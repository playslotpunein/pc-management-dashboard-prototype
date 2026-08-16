"""The billing engine.

The architecture's formula:

    Bill = rate snapshot ÷ 60 × actual minutes, plus controller surcharge, plus each
    extension charged incrementally, plus per-minute overtime once grace is consumed.

Everything here is a pure function over explicit inputs. No database, no clock, no
config lookups. That is deliberate: billing is the one part of the system where being
wrong is not a bug report but an argument at the counter, and pure functions can be
tested exhaustively.

Three rules the code enforces, each of which exists because the alternative is worse:

**The rate is snapshotted, never looked up.** ``rate_snapshot_paise`` is passed in from
the session row, captured when the session started. A manager raising PS5 pricing at 6pm
must not retroactively change the bill of someone who started at 5pm — that makes revenue
unauditable and produces disputes nobody can settle.

**Grace is free.** The five minutes after expiry cost nothing. They exist to give the
manager time to walk over and offer an extension, and charging for them would turn a
courtesy into a complaint.

**Every amount is an itemised line.** The total is the sum of the lines and nothing else,
so "why is this ₹340?" is always answerable from the stored bill rather than by
re-deriving it later against pricing that may since have changed.

Interpretation note: the doc's "actual minutes" is read as *billed* minutes, where a
session is never billed below the time it booked. A customer who books an hour and leaves
at forty minutes pays for the hour; a walk-in who never set a duration books zero and pays
purely for time used. Overstaying past grace adds overtime on top. This is the standard
café model, and it is stated here because it is a revenue decision rather than a
technical one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from playslot.clock import ensure_utc
from playslot.money import Paise, prorate_hourly


@dataclass(frozen=True, slots=True)
class Extension:
    """Time added to a running session, billed as its own line."""

    minutes: int
    granted_at: datetime

    #: Captured per extension. Normally equal to the session's snapshot, but stored
    #: separately so a deliberate goodwill discount on one extension stays visible in
    #: the breakdown instead of being averaged into the total.
    rate_snapshot_paise: Paise


@dataclass(frozen=True, slots=True)
class BillLine:
    kind: str
    description: str
    amount_paise: Paise
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class Bill:
    lines: tuple[BillLine, ...]
    total_paise: Paise

    #: Wall-clock minutes from start to end, rounded up. Reported for the receipt and
    #: for reconciliation; not necessarily what was charged.
    actual_minutes: int

    #: Paid-for minutes: the booked duration plus every extension.
    booked_minutes: int

    #: Minutes beyond booked time *and* beyond grace. These are the billed overtime.
    overtime_minutes: int

    #: Minutes that elapsed but are deliberately not charged, because the unit locked
    #: when its grace ran out and the customer could not use it. Non-zero only on a
    #: unit something can actually lock; a pool table has no such minutes, which is why
    #: its overrun is billed instead.
    unbilled_minutes: int = 0

    def line(self, kind: str) -> BillLine | None:
        """Fetch a single line by kind, for tests and for the receipt renderer."""
        return next((line for line in self.lines if line.kind == kind), None)


def elapsed_minutes(started_at: datetime, ended_at: datetime) -> int:
    """Wall-clock minutes between two instants, rounded up.

    Rounded up because a part-minute of play is a minute of the unit being unavailable
    to anyone else. Both arguments must be timezone-aware; a naive datetime here would
    silently shift the bill by the local UTC offset.
    """
    start = ensure_utc(started_at)
    end = ensure_utc(ended_at)

    seconds = (end - start).total_seconds()

    if seconds <= 0:
        return 0

    return math.ceil(seconds / 60)


def compute_bill(
    *,
    rate_snapshot_paise: Paise,
    duration_minutes: int,
    started_at: datetime,
    ended_at: datetime,
    extensions: Sequence[Extension] = (),
    grace_minutes: int = 5,
    overtime_rate_paise_per_minute: Paise = 0,
    controller_surcharge_paise_per_hour: Paise = 0,
    extra_controllers: int = 0,
    locks_at_grace_end: bool = False,
) -> Bill:
    """Produce the itemised bill for a session.

    Args:
        rate_snapshot_paise: Hourly rate captured when the session started. Never the
            current pricing row.
        duration_minutes: Booked duration. Zero for an open-ended walk-in.
        extensions: Each is charged incrementally as its own line.
        grace_minutes: Free minutes after expiry before overtime begins.
        overtime_rate_paise_per_minute: Penalty rate per minute once grace is consumed.
            Zero does not mean free — it means overtime is charged at the session's own
            hourly rate, prorated. Free time is what ``grace_minutes`` is for.
        controller_surcharge_paise_per_hour: Per extra controller, where the unit takes
            them.
        extra_controllers: Controllers beyond the one included in the base rate.
        locks_at_grace_end: True where something actually shuts the unit down when grace
            runs out — an agent on a PC. Such a unit never accrues billable overtime,
            because the minute overtime would start is the minute the machine locks. The
            session stays open until the counter closes it, and those minutes are
            reported as ``unbilled_minutes`` rather than charged.
    """
    actual = elapsed_minutes(started_at, ended_at)
    extension_minutes = sum(extension.minutes for extension in extensions)
    booked = duration_minutes + extension_minutes

    lines: list[BillLine] = []

    # Base: the booked duration at the snapshot rate.
    if duration_minutes > 0:
        lines.append(
            BillLine(
                kind="base",
                description=f"{duration_minutes} min at the session rate",
                minutes=duration_minutes,
                amount_paise=prorate_hourly(rate_snapshot_paise, duration_minutes),
            )
        )

    # An open-ended walk-in books nothing up front, so the time it used is the base.
    # Without this it would be billed entirely as overtime, which is both wrong and
    # unexplainable to the customer.
    open_ended_minutes = 0

    if booked == 0 and actual > 0:
        open_ended_minutes = actual

        lines.append(
            BillLine(
                kind="base",
                description=f"{actual} min at the session rate (open-ended)",
                minutes=actual,
                amount_paise=prorate_hourly(rate_snapshot_paise, actual),
            )
        )

    # Extensions, itemised. Charged incrementally so each one is individually visible
    # rather than folded into a single larger base line.
    for index, extension in enumerate(extensions, start=1):
        lines.append(
            BillLine(
                kind="extension",
                description=f"Extension {index}: {extension.minutes} min",
                minutes=extension.minutes,
                amount_paise=prorate_hourly(
                    extension.rate_snapshot_paise, extension.minutes
                ),
            )
        )

    # Controller surcharge, prorated over the time actually charged for.
    charged_minutes = booked or open_ended_minutes

    if extra_controllers > 0 and controller_surcharge_paise_per_hour > 0:
        surcharge = prorate_hourly(
            controller_surcharge_paise_per_hour * extra_controllers, charged_minutes
        )

        lines.append(
            BillLine(
                kind="controller_surcharge",
                description=(
                    f"{extra_controllers} extra controller"
                    f"{'s' if extra_controllers > 1 else ''}"
                ),
                minutes=charged_minutes,
                amount_paise=surcharge,
            )
        )

    # Overtime: only the minutes past both the booked time and the grace period. An
    # open-ended session has no expiry, so it can never accrue overtime.
    overtime = 0

    if booked > 0:
        overtime = max(0, actual - booked - grace_minutes)

    # A unit that locks cannot run into billable overtime, because the moment overtime
    # would begin is the moment the agent locks the machine. Everything after that is
    # time the customer was shut out of, and charging for it bills them for a screen
    # they could not use — the session stays open only until someone at the counter
    # closes it, which might be an hour later on a busy evening.
    #
    # This is why the distinction is the unit's, not the type's. A pool table has
    # nothing to lock, so its overrun is real play and is charged; a PC's is not.
    unbilled = 0

    if locks_at_grace_end and overtime > 0:
        unbilled = overtime
        overtime = 0

    if overtime > 0:
        if overtime_rate_paise_per_minute > 0:
            lines.append(
                BillLine(
                    kind="overtime",
                    description=f"{overtime} min past the {grace_minutes} min grace",
                    minutes=overtime,
                    amount_paise=overtime_rate_paise_per_minute * overtime,
                )
            )
        else:
            # No penalty rate set, so overtime is charged at what the customer is
            # already paying. It is emphatically not free.
            #
            # It used to be: with no overtime rate this line was skipped entirely, and a
            # customer who booked an hour and played two paid for one. The pricing form
            # defaults that field to zero, so this was not an unusual setup — it was the
            # normal one, quietly giving away every overrun on the floor.
            #
            # The rate is the last one the customer actually bought at: if they extended
            # after a price rise, the extension's rate is the one in force when the
            # overtime began, and billing the older base rate would undercharge.
            rate = extensions[-1].rate_snapshot_paise if extensions else rate_snapshot_paise

            lines.append(
                BillLine(
                    kind="overtime",
                    description=(
                        f"{overtime} min past the {grace_minutes} min grace, "
                        f"at the session rate"
                    ),
                    minutes=overtime,
                    amount_paise=prorate_hourly(rate, overtime),
                )
            )

    return Bill(
        lines=tuple(lines),
        total_paise=sum(line.amount_paise for line in lines),
        actual_minutes=actual,
        booked_minutes=booked,
        overtime_minutes=overtime,
        unbilled_minutes=unbilled,
    )
