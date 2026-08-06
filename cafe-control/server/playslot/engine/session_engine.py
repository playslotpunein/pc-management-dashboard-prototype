"""The session engine.

The heart of the system. Holds the state machine, runs the timers on an asyncio loop and
decides every transition. The dashboard is a view over this; it never computes a timer or
a bill of its own. If the manager closes the browser tab, every session here keeps running
and every unit stays correctly locked or unlocked.

Timers are epoch-timestamp based, not tick counters. ``remaining_seconds`` is always
``end_time - now``, recomputed from stored values. Restart the control server mid-evening
and it picks every session up exactly where it was, because nothing important was held in
memory.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from playslot.clock import Clock, ensure_utc
from playslot.db import unit_of_work
from playslot.engine import lifecycle
from playslot.engine.alerts import Alert, AlertLedger, evaluate, no_show
from playslot.engine.billing import Extension, compute_bill
from playslot.enums import (
    AlertKind,
    PaymentMethod,
    PaymentStatus,
    SessionSource,
    SessionStatus,
    UnitState,
)
from playslot.models import ActivityLog, Pricing, Sale, Session, SyncOutbox, Unit

#: Seconds of remaining time at which the amber warning fires. The architecture is
#: explicit that this is exactly 300, not "about five minutes".
WARNING_SECONDS = 300

#: How long a scheduled booking is held before it is released as a no-show.
NO_SHOW_TIMEOUT_MINUTES = 15


class SessionEngineError(RuntimeError):
    pass


class UnitBusy(SessionEngineError):
    pass


class UnitNotFound(SessionEngineError):
    pass


class SessionNotFound(SessionEngineError):
    pass


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one pass of the loop changed. Returned so tests can assert on it."""

    transitions: tuple[tuple[str, UnitState, UnitState], ...] = ()
    alerts: tuple[Alert, ...] = ()
    lock_commands: tuple[str, ...] = ()
    unlock_commands: tuple[str, ...] = ()


#: Called with a unit id when the agent must lock or unlock. Wired to the WebSocket in
#: production; a list append in tests.
CommandSink = Callable[[str, bool], Awaitable[None]]


class SessionEngine:
    """Owns session lifecycle, billing and alerting for one venue."""

    def __init__(
        self,
        factory: sessionmaker[OrmSession],
        *,
        venue_id: str,
        clock: Clock | None = None,
        command_sink: CommandSink | None = None,
        warning_seconds: int = WARNING_SECONDS,
        no_show_timeout_minutes: int = NO_SHOW_TIMEOUT_MINUTES,
    ) -> None:
        self._factory = factory
        self._venue_id = venue_id
        self._clock = clock or Clock()
        self._command_sink = command_sink
        self._warning_seconds = warning_seconds
        self._no_show_timeout_minutes = no_show_timeout_minutes

        self._ledger = AlertLedger()
        self._task: asyncio.Task[None] | None = None

        #: Last lock command dispatched per unit. Commands are driven by the difference
        #: between desired and dispatched state rather than by observing a transition
        #: edge, because an edge is easy to miss: extending a locked session moves the
        #: unit to ACTIVE outside the tick, and the customer would sit in front of a
        #: locked machine they had just paid to unlock. Comparing state also means a
        #: restarted engine re-asserts every unit's lock rather than assuming the agents
        #: are still where it left them.
        self._lock_state: dict[str, bool] = {}

        #: Commands raised by the synchronous API (ending a locked session) for the next
        #: tick to dispatch, since those paths cannot await.
        self._pending: list[tuple[str, bool]] = []

    # ------------------------------------------------------------------ timing

    def now(self) -> datetime:
        """The engine's clock.

        Exposed so the API reads time from the same source the engine bills against.
        Two clocks in one process is how a sales rollup silently excludes a sale that
        was written moments earlier.
        """
        return self._clock.now()

    def countdown_for(self, session: Session) -> lifecycle.Countdown:
        """Where a session sits in time, derived from stored timestamps only."""
        if session.start_time is None:
            return lifecycle.Countdown(remaining_seconds=0, grace_remaining_seconds=0)

        booked = session.duration_minutes + sum(
            extension["minutes"] for extension in session.extensions
        )

        # An open-ended walk-in has no deadline, so it never expires, warns or locks.
        if booked <= 0:
            return lifecycle.Countdown(
                remaining_seconds=_FOREVER, grace_remaining_seconds=_FOREVER
            )

        end = ensure_utc(session.start_time) + timedelta(minutes=booked)
        remaining = int((end - self._clock.now()).total_seconds())

        grace_end = end + timedelta(minutes=session.grace_minutes)
        grace_remaining = int((grace_end - self._clock.now()).total_seconds())

        return lifecycle.Countdown(
            remaining_seconds=remaining,
            grace_remaining_seconds=max(0, grace_remaining),
        )

    # ------------------------------------------------------------- transitions

    def _move(
        self,
        db: OrmSession,
        unit: Unit,
        target: UnitState,
        *,
        reason: str,
        actor: str = "system",
        session_id: str | None = None,
    ) -> tuple[str, UnitState, UnitState] | None:
        """Apply a validated transition and log it. Returns the change, or None.

        Moves through every intermediate state rather than jumping, so a tick that was
        missed — a restart, or the server being down across an expiry — still produces a
        legal sequence and a complete audit trail.
        """
        origin = unit.state

        if origin is target:
            return None

        previous = origin

        for step in lifecycle.path_to(origin, target):
            unit.state = lifecycle.transition(previous, step, reason=reason)

            db.add(
                ActivityLog(
                    venue_id=self._venue_id,
                    timestamp=self._clock.now(),
                    event=f"unit.{step.value}",
                    unit_id=unit.id,
                    session_id=session_id or unit.current_session_id,
                    actor=actor,
                    detail=f"{previous.value} -> {step.value}: {reason}",
                )
            )

            previous = step

        return (unit.id, origin, target)

    # ------------------------------------------------------------ session start

    def start_session(
        self,
        *,
        unit_id: str,
        source: SessionSource = SessionSource.WALK_IN,
        customer_ref: str = "",
        duration_minutes: int = 0,
        extra_controllers: int = 0,
        actor: str = "counter",
    ) -> str:
        """Begin a session and return its id.

        The rate is read from pricing once, here, and copied onto the session. From this
        point the session bills at that rate no matter what pricing does.
        """
        with unit_of_work(self._factory) as db:
            unit = db.get(Unit, unit_id)

            if unit is None:
                raise UnitNotFound(unit_id)

            if unit.state not in (UnitState.AVAILABLE, UnitState.SCHEDULED):
                raise UnitBusy(
                    f"Unit {unit.name} is {unit.state.value}; it must be available "
                    "or scheduled to start a session."
                )

            pricing = self._current_pricing(db, unit)
            now = self._clock.now()

            session = Session(
                venue_id=self._venue_id,
                unit_id=unit.id,
                source=source,
                customer_ref=customer_ref,
                status=SessionStatus.ACTIVE,
                start_time=now,
                duration_minutes=duration_minutes,
                extensions=[],
                rate_snapshot_paise=pricing.hourly_rate_paise,
                overtime_rate_paise_per_minute=pricing.overtime_rate_paise_per_minute,
                controller_surcharge_paise_per_hour=(
                    pricing.controller_surcharge_paise_per_hour
                ),
                extra_controllers=extra_controllers,
            )

            db.add(session)
            db.flush()

            self._move(
                db,
                unit,
                UnitState.ACTIVE,
                reason=f"session started ({source.value})",
                actor=actor,
                session_id=session.id,
            )

            unit.current_session_id = session.id

            # Push fresh state to the agent even though the lock state has not changed.
            # The body carries the session end time, and the agent's fail-safe depends on
            # having it: without this its cache still reads "no session", so if the link
            # dropped mid-session it would never lock however long the customer stayed.
            self._pending.append((unit.id, False))

            self._enqueue(
                db,
                "session.started",
                {
                    "session_id": session.id,
                    "unit_id": unit.id,
                    "source": source.value,
                    "duration_minutes": duration_minutes,
                    "rate_snapshot_paise": session.rate_snapshot_paise,
                    "start_time": now.isoformat(),
                },
            )

            return session.id

    def _current_pricing(self, db: OrmSession, unit: Unit) -> Pricing:
        """The pricing row in effect right now for this unit's type.

        Latest ``effective_from`` that is not in the future. Future-dated rows are how a
        venue schedules a price change without it taking hold early.
        """
        pricing = db.scalars(
            select(Pricing)
            .where(
                Pricing.venue_id == self._venue_id,
                Pricing.unit_type == unit.type,
                Pricing.effective_from <= self._clock.now(),
            )
            .order_by(Pricing.effective_from.desc())
            .limit(1)
        ).first()

        if pricing is None:
            raise SessionEngineError(
                f"No pricing for unit type {unit.type.value}. Add a pricing row before "
                "starting sessions, or every bill would be zero."
            )

        return pricing

    # ------------------------------------------------------------------ extend

    def extend_session(
        self, *, session_id: str, minutes: int, actor: str = "counter"
    ) -> None:
        """Add time. Works from any occupied state, including LOCKED.

        Extending a locked unit returns it straight to ACTIVE — the lock is recoverable
        by design, and this is the path a manager takes when the customer pays for more
        time at the counter.
        """
        if minutes <= 0:
            raise SessionEngineError("An extension must add a positive number of minutes.")

        with unit_of_work(self._factory) as db:
            session = db.get(Session, session_id)

            if session is None:
                raise SessionNotFound(session_id)

            if session.status is not SessionStatus.ACTIVE:
                raise SessionEngineError(
                    f"Session is {session.status.value}; only an active session extends."
                )

            now = self._clock.now()

            # Reassigned rather than appended: SQLAlchemy does not track in-place
            # mutation of a JSON column, so appending would not be persisted.
            session.extensions = [
                *session.extensions,
                {
                    "minutes": minutes,
                    "granted_at": now.isoformat(),
                    # The session's snapshot, not current pricing. An extension at 6pm
                    # on a session started at 5pm bills at the 5pm rate.
                    "rate_snapshot_paise": session.rate_snapshot_paise,
                },
            ]

            unit = db.get(Unit, session.unit_id)

            if unit is None:
                raise UnitNotFound(session.unit_id)

            # Re-arm the alerts: the deadline moved, so the warning and expiry events
            # must be allowed to fire again against the new end time.
            self._ledger.forget(session_id)

            countdown = self.countdown_for(session)
            target = lifecycle.derive_state(
                countdown, warning_seconds=self._warning_seconds, current=unit.state
            )

            self._move(
                db,
                unit,
                target,
                reason=f"extended by {minutes} min",
                actor=actor,
                session_id=session_id,
            )

            # The deadline moved, so the agent's cached end time is now wrong. Left stale,
            # its fail-safe would measure against the old deadline and lock a customer who
            # has just paid for more time.
            self._pending.append((unit.id, target is UnitState.LOCKED))

            self._enqueue(
                db,
                "session.extended",
                {
                    "session_id": session_id,
                    "minutes": minutes,
                    "granted_at": now.isoformat(),
                },
            )

    # ------------------------------------------------------------------- close

    def end_session(
        self,
        *,
        session_id: str,
        payment_method: PaymentMethod = PaymentMethod.CASH,
        actor: str = "counter",
    ) -> Sale:
        """End a session, bill it, and free the unit.

        The bill is computed once here and its line breakdown stored on the sale.
        Recomputing it later would read pricing that may since have changed.
        """
        with unit_of_work(self._factory) as db:
            session = db.get(Session, session_id)

            if session is None:
                raise SessionNotFound(session_id)

            if session.status is not SessionStatus.ACTIVE:
                raise SessionEngineError(f"Session is already {session.status.value}.")

            now = self._clock.now()

            session.end_time = now
            session.status = SessionStatus.CLOSED

            bill = self._bill_for(session, ended_at=now)

            sale = Sale(
                venue_id=self._venue_id,
                session_id=session.id,
                source=session.source,
                amount_paise=bill.total_paise,
                payment_method=payment_method,
                # An app booking arrives already settled; a walk-in pays at the counter.
                payment_status=(
                    PaymentStatus.PAID
                    if payment_method is not PaymentMethod.PAID_ONLINE
                    else PaymentStatus.PAID
                ),
                lines=[
                    {
                        "kind": line.kind,
                        "description": line.description,
                        "minutes": line.minutes,
                        "amount_paise": line.amount_paise,
                    }
                    for line in bill.lines
                ],
                settled_at=now,
            )

            db.add(sale)

            unit = db.get(Unit, session.unit_id)

            if unit is not None:
                self._move(
                    db,
                    unit,
                    UnitState.AVAILABLE,
                    reason="session ended",
                    actor=actor,
                    session_id=session_id,
                )

                unit.current_session_id = None

            db.add(
                ActivityLog(
                    venue_id=self._venue_id,
                    timestamp=now,
                    event="session.billed",
                    unit_id=session.unit_id,
                    session_id=session.id,
                    amount_paise=bill.total_paise,
                    actor=actor,
                    detail=f"{bill.actual_minutes} min, {payment_method.value}",
                )
            )

            self._enqueue(
                db,
                "sale.created",
                {
                    "sale_id": sale.id,
                    "session_id": session.id,
                    "amount_paise": bill.total_paise,
                    "payment_method": payment_method.value,
                    "settled_at": now.isoformat(),
                },
            )

            # A session can be ended while the unit is locked — the customer settles up
            # and leaves. Once the session closes the tick stops looking at this unit,
            # so the unlock has to be queued here or the machine stays locked for
            # whoever sits down next.
            #
            # Pushed unconditionally, not only when locked, because the message also
            # clears the agent's cached end time. Leaving a stale one behind would have
            # the agent fail safe against a finished session and lock the next customer
            # against the previous one's deadline.
            self._pending.append((session.unit_id, False))

            self._lock_state.pop(session.unit_id, None)
            self._ledger.forget(session_id)

            db.flush()
            return sale

    def _bill_for(self, session: Session, *, ended_at: datetime):
        return compute_bill(
            rate_snapshot_paise=session.rate_snapshot_paise,
            duration_minutes=session.duration_minutes,
            started_at=session.start_time,
            ended_at=ended_at,
            extensions=[
                Extension(
                    minutes=raw["minutes"],
                    granted_at=datetime.fromisoformat(raw["granted_at"]),
                    rate_snapshot_paise=raw["rate_snapshot_paise"],
                )
                for raw in session.extensions
            ],
            grace_minutes=session.grace_minutes,
            overtime_rate_paise_per_minute=session.overtime_rate_paise_per_minute,
            controller_surcharge_paise_per_hour=(
                session.controller_surcharge_paise_per_hour
            ),
            extra_controllers=session.extra_controllers,
        )

    def preview_bill(self, session_id: str):
        """The bill as it stands right now, for a live session.

        Drives the running total on the dashboard and the sales rollup's in-progress
        figure. The manager cannot see what is owed on the floor without it.
        """
        with unit_of_work(self._factory) as db:
            session = db.get(Session, session_id)

            if session is None:
                raise SessionNotFound(session_id)

            return self._bill_for(session, ended_at=self._clock.now())

    # -------------------------------------------------------------------- tick

    async def tick(self) -> TickResult:
        """One pass: recompute every occupied unit, transition it, raise alerts.

        Everything in a pass is one transaction, so a unit's new state, its audit row
        and its outbox entry either all land or none do.
        """
        transitions: list[tuple[str, UnitState, UnitState]] = []
        alerts: list[Alert] = []
        lock_commands: list[str] = []
        unlock_commands: list[str] = []

        with unit_of_work(self._factory) as db:
            units = db.scalars(
                select(Unit).where(Unit.venue_id == self._venue_id)
            ).all()

            for unit in units:
                if unit.state is UnitState.SCHEDULED:
                    change = self._check_no_show(db, unit, alerts)

                    if change:
                        transitions.append(change)

                    continue

                if unit.state not in lifecycle._LEGAL or unit.current_session_id is None:
                    continue

                if unit.state in (UnitState.AVAILABLE, UnitState.MAINTENANCE):
                    continue

                session = db.get(Session, unit.current_session_id)

                if session is None or session.status is not SessionStatus.ACTIVE:
                    continue

                countdown = self.countdown_for(session)

                target = lifecycle.derive_state(
                    countdown,
                    warning_seconds=self._warning_seconds,
                    current=unit.state,
                )

                change = self._move(
                    db, unit, target, reason="timer", session_id=session.id
                )

                if change:
                    transitions.append(change)

                # Desired state, compared against what the agent was last told. The
                # lock command follows state rather than the alert, so a restarted
                # control server re-asserts a lock it never sent itself.
                should_lock = target is UnitState.LOCKED

                # An unknown unit is assumed unlocked, which is both the truth for a
                # freshly started session and the safe assumption after a restart: a
                # unit that should be locked differs from the default and gets its lock
                # command, while one that should be free stays quiet.
                if self._lock_state.get(unit.id, False) != should_lock:
                    self._lock_state[unit.id] = should_lock

                    if should_lock:
                        lock_commands.append(unit.id)
                    else:
                        unlock_commands.append(unit.id)

                alerts.extend(
                    evaluate(
                        unit_id=unit.id,
                        session_id=session.id,
                        unit_name=unit.name,
                        countdown=countdown,
                        warning_seconds=self._warning_seconds,
                        ledger=self._ledger,
                    )
                )

        # Drain anything the synchronous API queued since the last tick.
        while self._pending:
            unit_id, lock = self._pending.pop(0)
            (lock_commands if lock else unlock_commands).append(unit_id)

        await self._dispatch(lock_commands, unlock_commands)

        return TickResult(
            transitions=tuple(transitions),
            alerts=tuple(alerts),
            lock_commands=tuple(lock_commands),
            unlock_commands=tuple(unlock_commands),
        )

    def _check_no_show(
        self, db: OrmSession, unit: Unit, alerts: list[Alert]
    ) -> tuple[str, UnitState, UnitState] | None:
        """Release a booking the player never claimed, and flag the refund decision."""
        if unit.current_session_id is None:
            return None

        session = db.get(Session, unit.current_session_id)

        if session is None or session.scheduled_start is None:
            return None

        deadline = ensure_utc(session.scheduled_start) + timedelta(
            minutes=self._no_show_timeout_minutes
        )

        if self._clock.now() < deadline:
            return None

        session.status = SessionStatus.NO_SHOW

        alerts.append(
            no_show(unit_id=unit.id, session_id=session.id, unit_name=unit.name)
        )

        change = self._move(
            db,
            unit,
            UnitState.AVAILABLE,
            reason=f"no-show after {self._no_show_timeout_minutes} min",
            session_id=session.id,
        )

        unit.current_session_id = None

        self._enqueue(
            db,
            "session.no_show",
            {
                "session_id": session.id,
                "unit_id": unit.id,
                "refund_decision_required": True,
            },
        )

        return change

    async def _dispatch(self, lock: list[str], unlock: list[str]) -> None:
        if self._command_sink is None:
            return

        for unit_id in lock:
            await self._command_sink(unit_id, True)

        for unit_id in unlock:
            await self._command_sink(unit_id, False)

    # ------------------------------------------------------------------ outbox

    def _enqueue(self, db: OrmSession, event_type: str, payload: dict) -> None:
        """Queue an event for the cloud.

        Writes unconditionally, even in standalone mode where nothing drains it. The
        row's primary key is the idempotency key, so a retry mid-flush cannot write the
        same sale twice.
        """
        db.add(
            SyncOutbox(
                venue_id=self._venue_id,
                event_type=event_type,
                payload=payload,
                created_at=self._clock.now(),
            )
        )

    # ----------------------------------------------------------- loop lifecycle

    async def run(self, *, interval_seconds: float = 1.0) -> None:
        """Tick forever. Started as a background task by the FastAPI lifespan."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A single bad tick must never stop the loop; that would strand every
                # unit in whatever state it was last in.
                import logging

                logging.getLogger(__name__).exception("Session engine tick failed")

            await asyncio.sleep(interval_seconds)

    def start(self, *, interval_seconds: float = 1.0) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(interval_seconds=interval_seconds))

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await self._task

        self._task = None


#: Large enough that an open-ended session never warns, expires or locks, without
#: needing a nullable "no deadline" branch in every comparison.
_FOREVER = 10**9
