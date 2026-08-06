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
