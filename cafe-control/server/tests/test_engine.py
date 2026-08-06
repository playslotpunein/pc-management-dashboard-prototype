"""Session engine tests, against a real database.

These walk a session through its whole life on a frozen clock: start, warn, expire,
grace, lock, extend, close. Between them they cover every rule the architecture calls
out as load-bearing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from playslot.db import unit_of_work
from playslot.engine.session_engine import SessionEngine, SessionEngineError, UnitBusy
from playslot.enums import (
    AlertKind,
    PaymentMethod,
    SessionSource,
    SessionStatus,
    UnitState,
)
from playslot.models import ActivityLog, Pricing, Sale, Session, SyncOutbox, Unit
from playslot.money import rupees

from .conftest import VENUE

PC = "unit-pc-01"
PS5 = "unit-ps5-01"


def unit_state(factory, unit_id: str) -> UnitState:
    with unit_of_work(factory) as db:
        return db.get(Unit, unit_id).state


class TestStartingSessions:
    async def test_starting_moves_the_unit_to_active(self, engine, seeded):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        assert unit_state(seeded, PC) is UnitState.ACTIVE

        with unit_of_work(seeded) as db:
            unit = db.get(Unit, PC)
            assert unit.current_session_id == session_id

    async def test_the_rate_is_snapshotted_at_start(self, engine, seeded):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with unit_of_work(seeded) as db:
            session = db.get(Session, session_id)
            assert session.rate_snapshot_paise == rupees(120)

    async def test_a_busy_unit_cannot_take_a_second_session(self, engine):
        engine.start_session(unit_id=PC, duration_minutes=60)

        with pytest.raises(UnitBusy, match="active"):
            engine.start_session(unit_id=PC, duration_minutes=60)

    async def test_starting_without_pricing_fails_loudly(self, factory, clock):
        """Better than billing everyone zero and finding out at closing time."""
        with unit_of_work(factory) as db:
            db.add(Unit(id="u1", venue_id=VENUE, name="Orphan", type="pc"))

        engine = SessionEngine(factory, venue_id=VENUE, clock=clock)

        with pytest.raises(SessionEngineError, match="No pricing"):
            engine.start_session(unit_id="u1", duration_minutes=60)

    async def test_walk_in_is_the_default_source(self, engine, seeded):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with unit_of_work(seeded) as db:
            assert db.get(Session, session_id).source is SessionSource.WALK_IN


class TestRateSnapshotAgainstLivePricing:
    async def test_a_price_rise_does_not_touch_a_running_session(
        self, engine, seeded, clock
    ):
        """The 5pm-session-billed-at-6pm-prices scenario, end to end."""
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        # 6pm: the manager raises PC pricing.
        clock.advance(minutes=30)

        with unit_of_work(seeded) as db:
            db.add(
                Pricing(
                    venue_id=VENUE,
                    unit_type="pc",
                    hourly_rate_paise=rupees(220),
                    effective_from=clock.now(),
                )
            )

        clock.advance(minutes=30)
        sale = engine.end_session(session_id=session_id)

        assert sale.amount_paise == rupees(120)

    async def test_a_new_session_picks_up_the_new_price(self, engine, seeded, clock):
        clock.advance(minutes=30)

        with unit_of_work(seeded) as db:
            db.add(
                Pricing(
                    venue_id=VENUE,
                    unit_type="pc",
                    hourly_rate_paise=rupees(220),
                    effective_from=clock.now(),
                )
            )

        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with unit_of_work(seeded) as db:
            assert db.get(Session, session_id).rate_snapshot_paise == rupees(220)

    async def test_future_dated_pricing_does_not_take_hold_early(
        self, engine, seeded, clock
    ):
        with unit_of_work(seeded) as db:
            db.add(
                Pricing(
                    venue_id=VENUE,
                    unit_type="pc",
                    hourly_rate_paise=rupees(999),
                    effective_from=clock.now() + timedelta(days=1),
                )
            )

        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with unit_of_work(seeded) as db:
            assert db.get(Session, session_id).rate_snapshot_paise == rupees(120)


class TestTheCountdown:
    async def test_full_walk_through_active_warning_overtime_locked(
        self, engine, seeded, clock, commands
    ):
        """The whole lifecycle on one unit, no skipping."""
        engine.start_session(unit_id=PC, duration_minutes=60)

        # 54 minutes in: 6 minutes left, still active.
        clock.advance(minutes=54)
        await engine.tick()
        assert unit_state(seeded, PC) is UnitState.ACTIVE

        # 55:01 — under 300 seconds. Amber.
        clock.advance(minutes=1, seconds=1)
        result = await engine.tick()
        assert unit_state(seeded, PC) is UnitState.WARNING
        assert any(a.kind is AlertKind.FIVE_MINUTE_WARNING for a in result.alerts)

        # The unit stays unlocked through the warning.
        assert commands == []

        # Timer hits zero: overtime, grace running, still unlocked.
        clock.advance(minutes=5)
        result = await engine.tick()
        assert unit_state(seeded, PC) is UnitState.OVERTIME
        assert any(a.kind is AlertKind.EXPIRED for a in result.alerts)
        assert commands == []

        # Grace consumed: now, and only now, it locks.
        clock.advance(minutes=5, seconds=1)
        result = await engine.tick()
        assert unit_state(seeded, PC) is UnitState.LOCKED
        assert any(a.kind is AlertKind.GRACE_TIMEOUT for a in result.alerts)
        assert commands == [(PC, True)]

    async def test_alerts_do_not_repeat_every_tick(self, engine, clock):
        engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=56)

        first = await engine.tick()
        second = await engine.tick()
        third = await engine.tick()

        assert len(first.alerts) == 1
        assert second.alerts == ()
        assert third.alerts == ()

    async def test_an_open_ended_walk_in_never_expires(self, engine, seeded, clock):
        engine.start_session(unit_id=PC, duration_minutes=0)

        clock.advance(hours=6)
        await engine.tick()

        assert unit_state(seeded, PC) is UnitState.ACTIVE

    async def test_state_survives_a_restart(self, seeded, clock, commands):
        """Epoch timers, not tick counters.

        A brand-new engine over the same database derives the same state, because
        nothing that mattered was held in memory.
        """
        first = SessionEngine(seeded, venue_id=VENUE, clock=clock)
        first.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=66)

        # The original engine never ticked. A fresh one still gets it right.
        async def sink(unit_id: str, lock: bool) -> None:
            commands.append((unit_id, lock))

        restarted = SessionEngine(seeded, venue_id=VENUE, clock=clock, command_sink=sink)
        await restarted.tick()

        assert unit_state(seeded, PC) is UnitState.LOCKED
        assert commands == [(PC, True)]


class TestExtending:
    async def test_extending_a_locked_unit_returns_it_to_active(
        self, engine, seeded, clock, commands
    ):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=66)
        await engine.tick()
        assert unit_state(seeded, PC) is UnitState.LOCKED

        engine.extend_session(session_id=session_id, minutes=30)

        assert unit_state(seeded, PC) is UnitState.ACTIVE

        # And the agent is told to unlock.
        await engine.tick()
        assert (PC, False) in commands

    async def test_extending_re_arms_the_warning(self, engine, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=56)
        first = await engine.tick()
        assert any(a.kind is AlertKind.FIVE_MINUTE_WARNING for a in first.alerts)

        engine.extend_session(session_id=session_id, minutes=30)

        # The new deadline gets its own warning rather than being suppressed as a repeat.
        clock.advance(minutes=30)
        second = await engine.tick()
        assert any(a.kind is AlertKind.FIVE_MINUTE_WARNING for a in second.alerts)

    async def test_an_extension_bills_at_the_session_rate_not_the_current_one(
        self, engine, seeded, clock
    ):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=30)

        with unit_of_work(seeded) as db:
            db.add(
                Pricing(
                    venue_id=VENUE,
                    unit_type="pc",
                    hourly_rate_paise=rupees(220),
                    effective_from=clock.now(),
                )
            )

        engine.extend_session(session_id=session_id, minutes=30)
        clock.advance(minutes=60)

        sale = engine.end_session(session_id=session_id)

        # 60 min + 30 min extension, both at ₹120/hr.
        assert sale.amount_paise == rupees(180)

    async def test_a_zero_minute_extension_is_rejected(self, engine):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        with pytest.raises(SessionEngineError, match="positive"):
            engine.extend_session(session_id=session_id, minutes=0)


class TestEndingAndBilling:
    async def test_ending_frees_the_unit_and_writes_a_sale(self, engine, seeded, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=60)

        sale = engine.end_session(session_id=session_id, payment_method=PaymentMethod.UPI)

        assert unit_state(seeded, PC) is UnitState.AVAILABLE
        assert sale.amount_paise == rupees(120)
        assert sale.payment_method is PaymentMethod.UPI

        with unit_of_work(seeded) as db:
            assert db.get(Unit, PC).current_session_id is None
            assert db.get(Session, session_id).status is SessionStatus.CLOSED

    async def test_the_breakdown_is_stored_not_recomputed(self, engine, seeded, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=90)

        sale = engine.end_session(session_id=session_id)

        with unit_of_work(seeded) as db:
            stored = db.get(Sale, sale.id)

            assert stored.lines
            assert sum(line["amount_paise"] for line in stored.lines) == stored.amount_paise

    async def test_ps5_surcharge_reaches_the_bill(self, engine, clock):
        session_id = engine.start_session(
            unit_id=PS5, duration_minutes=60, extra_controllers=2
        )
        clock.advance(minutes=60)

        sale = engine.end_session(session_id=session_id)

        # ₹180 base + (₹40 × 2 controllers).
        assert sale.amount_paise == rupees(260)

    async def test_a_session_cannot_be_billed_twice(self, engine, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=60)

        engine.end_session(session_id=session_id)

        with pytest.raises(SessionEngineError, match="already closed"):
            engine.end_session(session_id=session_id)

    async def test_preview_shows_what_is_owed_right_now(self, engine, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=30)
        preview = engine.preview_bill(session_id)

        # Booked an hour, so an hour is owed even half way through.
        assert preview.total_paise == rupees(120)
        assert preview.actual_minutes == 30


class TestNoShow:
    async def test_an_unclaimed_booking_is_released_and_flagged(
        self, engine, seeded, clock
    ):
        with unit_of_work(seeded) as db:
            session = Session(
                id="sess-booked",
                venue_id=VENUE,
                unit_id=PC,
                source=SessionSource.APP,
                status=SessionStatus.SCHEDULED,
                scheduled_start=clock.now(),
                duration_minutes=60,
                rate_snapshot_paise=rupees(120),
            )

            db.add(session)

            unit = db.get(Unit, PC)
            unit.state = UnitState.SCHEDULED
            unit.current_session_id = "sess-booked"

        clock.advance(minutes=16)
        result = await engine.tick()

        assert unit_state(seeded, PC) is UnitState.AVAILABLE
        assert any(a.kind is AlertKind.NO_SHOW for a in result.alerts)

        with unit_of_work(seeded) as db:
            assert db.get(Session, "sess-booked").status is SessionStatus.NO_SHOW

            queued = db.query(SyncOutbox).filter_by(event_type="session.no_show").one()
            assert queued.payload["refund_decision_required"] is True

    async def test_a_booking_is_held_until_the_timeout(self, engine, seeded, clock):
        with unit_of_work(seeded) as db:
            db.add(
                Session(
                    id="sess-booked",
                    venue_id=VENUE,
                    unit_id=PC,
                    source=SessionSource.APP,
                    status=SessionStatus.SCHEDULED,
                    scheduled_start=clock.now(),
                    duration_minutes=60,
                    rate_snapshot_paise=rupees(120),
                )
            )

            unit = db.get(Unit, PC)
            unit.state = UnitState.SCHEDULED
            unit.current_session_id = "sess-booked"

        clock.advance(minutes=14)
        await engine.tick()

        assert unit_state(seeded, PC) is UnitState.SCHEDULED


class TestAuditTrail:
    async def test_every_transition_is_logged_with_a_cause(self, engine, seeded, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=66)
        await engine.tick()
        engine.end_session(session_id=session_id)

        with unit_of_work(seeded) as db:
            events = [row.event for row in db.query(ActivityLog).all()]

            assert "unit.active" in events
            assert "unit.overtime" in events
            assert "unit.locked" in events
            assert "session.billed" in events

            # Nothing unattributed.
            assert all(row.actor for row in db.query(ActivityLog).all())

    async def test_the_sale_is_logged_with_its_amount(self, engine, seeded, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=60)
        engine.end_session(session_id=session_id)

        with unit_of_work(seeded) as db:
            billed = db.query(ActivityLog).filter_by(event="session.billed").one()
            assert billed.amount_paise == rupees(120)


class TestSyncOutbox:
    async def test_events_queue_even_in_standalone_mode(self, engine, seeded, clock):
        session_id = engine.start_session(unit_id=PC, duration_minutes=60)
        clock.advance(minutes=60)
        engine.end_session(session_id=session_id)

        with unit_of_work(seeded) as db:
            queued = {row.event_type for row in db.query(SyncOutbox).all()}

            assert "session.started" in queued
            assert "sale.created" in queued

    async def test_every_queued_event_has_a_unique_idempotency_key(
        self, engine, seeded, clock
    ):
        for _ in range(3):
            session_id = engine.start_session(unit_id=PC, duration_minutes=30)
            clock.advance(minutes=30)
            engine.end_session(session_id=session_id)

        with unit_of_work(seeded) as db:
            rows = db.query(SyncOutbox).all()
            keys = [row.event_id for row in rows]

            # Without this a retry mid-flush duplicates sales and inflates revenue.
            assert len(keys) == len(set(keys))
            assert all(row.synced_at is None for row in rows)
