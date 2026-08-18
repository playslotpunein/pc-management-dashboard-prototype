"""Sales rollup.

Aggregates closed sales *and* live in-progress bills, split by unit type.

Including the live bills is the whole point. A manager looking at the sales panel needs
to know what is actually owed on the floor right now — the ₹2,400 sitting in front of
six machines mid-session — not only what has already been paid. A rollup of closed
sessions alone reconciles at midnight and is useless at 8pm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from playslot.clock import ensure_utc
from playslot.enums import PaymentMethod, SessionStatus, UnitType
from playslot.models import Sale, Session, Unit
from playslot.money import Paise


@dataclass
class TypeRollup:
    unit_type: UnitType
    closed_paise: Paise = 0
    live_paise: Paise = 0
    closed_sessions: int = 0
    live_sessions: int = 0

    @property
    def total_paise(self) -> Paise:
        return self.closed_paise + self.live_paise


@dataclass
class Rollup:
    since: datetime
    until: datetime

    by_type: dict[UnitType, TypeRollup] = field(default_factory=dict)
    by_payment_method: dict[PaymentMethod, Paise] = field(default_factory=dict)

    @property
    def closed_paise(self) -> Paise:
        return sum(row.closed_paise for row in self.by_type.values())

    @property
    def live_paise(self) -> Paise:
        """What is owed on the floor but not yet collected."""
        return sum(row.live_paise for row in self.by_type.values())

    @property
    def total_paise(self) -> Paise:
        return self.closed_paise + self.live_paise


def business_day_start(now: datetime, *, day_starts_at: time = time(6, 0)) -> datetime:
    """The start of the current business day.

    A café's day does not end at midnight — a session that starts at 11pm and closes at
    1am belongs to the evening it began. Rolling over at 6am keeps a late night on one
    shift report instead of splitting it across two.
    """
    now = ensure_utc(now)
    candidate = now.replace(
        hour=day_starts_at.hour, minute=day_starts_at.minute, second=0, microsecond=0
    )

    return candidate if now >= candidate else candidate - timedelta(days=1)


def week_start(now: datetime, *, day_starts_at: time = time(6, 0)) -> datetime:
    """The start of the current week, Monday.

    Anchored to the *business* day rather than the calendar one, so a Sunday night that
    runs past midnight counts against the week it belongs to rather than opening the new
    one at 00:01 with a session already in progress.
    """
    day = business_day_start(now, day_starts_at=day_starts_at)

    return day - timedelta(days=day.weekday())


def month_start(now: datetime, *, day_starts_at: time = time(6, 0)) -> datetime:
    """The start of the current month, the 1st.

    Same anchoring: at 2am on the 1st the venue is still working last month's closing
    night, and its takings belong to the month that is ending.
    """
    return business_day_start(now, day_starts_at=day_starts_at).replace(day=1)


#: The windows the sales tab reports on. Today sits inside both of the others, but the
#: week and the month do **not** nest: in a week that straddles a month end — Thursday
#: 3 September 2026, say, whose Monday was 31 August — the week reaches back further than
#: the month does. Anything reading these has to span the earliest of them, not the month.
PERIODS = ("today", "week", "month")


def period_starts(
    now: datetime, *, day_starts_at: time = time(6, 0)
) -> dict[str, datetime]:
    return {
        "today": business_day_start(now, day_starts_at=day_starts_at),
        "week": week_start(now, day_starts_at=day_starts_at),
        "month": month_start(now, day_starts_at=day_starts_at),
    }


def rollup(
    db: OrmSession,
    *,
    venue_id: str,
    since: datetime,
    until: datetime,
    live_bill: callable,
) -> Rollup:
    """Build the rollup for a window.

    ``live_bill`` is called with a session id and must return an object exposing
    ``total_paise``. It is injected rather than imported so this stays a pure query
    over whatever the engine considers owed right now.
    """
    result = Rollup(since=ensure_utc(since), until=ensure_utc(until))

    unit_types = {
        unit.id: unit.type
        for unit in db.scalars(select(Unit).where(Unit.venue_id == venue_id)).all()
    }

    def bucket(unit_type: UnitType) -> TypeRollup:
        return result.by_type.setdefault(unit_type, TypeRollup(unit_type=unit_type))

    # Closed and paid.
    sales = db.scalars(
        select(Sale).where(
            Sale.venue_id == venue_id,
            Sale.settled_at >= result.since,
            Sale.settled_at <= result.until,
        )
    ).all()

    for sale in sales:
        session = db.get(Session, sale.session_id)

        if session is None:
            continue

        row = bucket(unit_types.get(session.unit_id, UnitType.PC))
        row.closed_paise += sale.amount_paise
        row.closed_sessions += 1

        result.by_payment_method[sale.payment_method] = (
            result.by_payment_method.get(sale.payment_method, 0) + sale.amount_paise
        )

    # Still running: what the floor owes.
    live = db.scalars(
        select(Session).where(
            Session.venue_id == venue_id,
            Session.status == SessionStatus.ACTIVE,
        )
    ).all()

    for session in live:
        row = bucket(unit_types.get(session.unit_id, UnitType.PC))
        row.live_paise += live_bill(session.id).total_paise
        row.live_sessions += 1

    return result


def rollup_periods(
    db: OrmSession,
    *,
    venue_id: str,
    now: datetime,
    day_starts_at: time = time(6, 0),
    live_bill: callable,
) -> dict[str, Rollup]:
    """Today, this week and this month, from one pass over the sales table.

    Three separate :func:`rollup` calls would give the same answer, and the sales panel
    polls every second: that is three scans a second, the widest of them over a month of
    rows, plus the live bill for every running session recomputed three times over.

    One query over the earliest of the three starts serves all of them: a sale is added to
    every period whose start it falls after.

    The earliest, not the month's. In a week that straddles a month end the week begins
    *before* the month — Thursday 3 September 2026 belongs to a week that started on
    Monday 31 August — so a query anchored to the month start would silently drop the tail
    of that week and under-report it for six days a year.

    **The live figure is deliberately identical across the three.** "Owed on the floor"
    is a fact about right now, not an aggregate over a window; the ₹2,400 sitting in
    front of six machines is the same ₹2,400 whether the manager is looking at today or
    at the month. Scoping it to each window instead would drop a session that started
    yesterday out of "today" and make the floor look emptier than it is.
    """
    starts = period_starts(now, day_starts_at=day_starts_at)
    until = ensure_utc(now)

    results = {
        period: Rollup(since=starts[period], until=until) for period in PERIODS
    }

    unit_types = {
        unit.id: unit.type
        for unit in db.scalars(select(Unit).where(Unit.venue_id == venue_id)).all()
    }

    def bucket(result: Rollup, unit_type: UnitType) -> TypeRollup:
        return result.by_type.setdefault(unit_type, TypeRollup(unit_type=unit_type))

    sales = db.scalars(
        select(Sale).where(
            Sale.venue_id == venue_id,
            Sale.settled_at >= min(starts.values()),
            Sale.settled_at <= until,
        )
    ).all()

    for sale in sales:
        session = db.get(Session, sale.session_id)

        if session is None:
            continue

        unit_type = unit_types.get(session.unit_id, UnitType.PC)
        settled = ensure_utc(sale.settled_at)

        for period, result in results.items():
            if settled < starts[period]:
                continue

            row = bucket(result, unit_type)
            row.closed_paise += sale.amount_paise
            row.closed_sessions += 1

            result.by_payment_method[sale.payment_method] = (
                result.by_payment_method.get(sale.payment_method, 0) + sale.amount_paise
            )

    live = db.scalars(
        select(Session).where(
            Session.venue_id == venue_id,
            Session.status == SessionStatus.ACTIVE,
        )
    ).all()

    for session in live:
        unit_type = unit_types.get(session.unit_id, UnitType.PC)
        owed = live_bill(session.id).total_paise

        for result in results.values():
            row = bucket(result, unit_type)
            row.live_paise += owed
            row.live_sessions += 1

    return results
