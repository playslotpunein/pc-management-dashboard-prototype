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
    EnforcementMode,
    PaymentMethod,
    SessionSource,
    SessionStatus,
    UnitState,
    UnitType,
)
from playslot.models import ActivityLog, Pricing, Sale, Session, SyncOutbox, Unit
from playslot.money import rupees

from .conftest import VENUE

PC = "unit-pc-01"
PS5 = "unit-ps5-01"
POOL = "unit-pool-01"


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

        # The unit stays unlocked through the warning. A state push at session start is
        # expected and carries the end time that seeds the agent's fail-safe cache, so
        # the invariant is that nothing has asked for a LOCK — not that nothing was sent.
        assert not any(lock for _, lock in commands)

        # Timer hits zero: overtime, grace running, still unlocked.
        clock.advance(minutes=5)
        result = await engine.tick()
        assert unit_state(seeded, PC) is UnitState.OVERTIME
        assert any(a.kind is AlertKind.EXPIRED for a in result.alerts)
        assert not any(lock for _, lock in commands)

        # Grace consumed: now, and only now, it locks.
        clock.advance(minutes=5, seconds=1)
        result = await engine.tick()
        assert unit_state(seeded, PC) is UnitState.LOCKED
        assert any(a.kind is AlertKind.GRACE_TIMEOUT for a in result.alerts)
        assert commands[-1] == (PC, True)

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


class TestTablesNothingCanLock:
    """Pool and snooker: timed and billed like everything else, held by nobody.

    A table has no agent, so the engine's last resort is unavailable. It runs the same
    clock, the same rates and the same sales, and where a PC would lock it nags the
    manager instead.
    """

    async def test_a_table_past_grace_stays_in_overtime(
        self, engine, pool_table, clock
    ):
        """It must not show LOCKED.

        There is no lock. Four people are still playing on it, and a padlock on the card
        would tell the manager the floor is under control when it is not.
        """
        engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        await engine.tick()

        assert unit_state(pool_table, POOL) is UnitState.OVERTIME

        # An hour past grace, and still not locked.
        clock.advance(hours=1)
        await engine.tick()

        assert unit_state(pool_table, POOL) is UnitState.OVERTIME

    async def test_no_lock_command_is_ever_dispatched(
        self, engine, pool_table, clock, commands
    ):
        """Nothing is listening. Sending anyway would log a failure once a second."""
        engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=90)
        await engine.tick()

        assert not any(unit_id == POOL and lock for unit_id, lock in commands)

    async def test_grace_expiry_raises_overdue_rather_than_a_lock(
        self, engine, pool_table, clock
    ):
        engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        result = await engine.tick()

        kinds = {alert.kind for alert in result.alerts}

        assert AlertKind.OVERDUE in kinds
        assert AlertKind.GRACE_TIMEOUT not in kinds
        assert not any(alert.triggers_lock for alert in result.alerts)

    async def test_the_overdue_reminder_repeats_every_five_minutes(
        self, engine, pool_table, clock
    ):
        """The nag is the enforcement, so unlike every other alert this one recurs."""
        engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        first = await engine.tick()

        clock.advance(minutes=5)
        second = await engine.tick()

        clock.advance(minutes=5)
        third = await engine.tick()

        for result in (first, second, third):
            assert [alert.kind for alert in result.alerts] == [AlertKind.OVERDUE]

        # It counts up, and counts from the end of the paid hour rather than from the
        # end of grace — the minutes shown are the minutes the manager has to charge for.
        assert "5 min over" in first.alerts[0].message
        assert "10 min over" in second.alerts[0].message
        assert "15 min over" in third.alerts[0].message

    async def test_the_reminder_does_not_repeat_on_every_tick(
        self, engine, pool_table, clock
    ):
        """The engine ticks once a second between reminders.

        Sixty toasts a minute is not an alert, it is a reason to stop reading them.
        """
        engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        await engine.tick()

        for _ in range(30):
            clock.advance(seconds=1)

            assert (await engine.tick()).alerts == ()

    async def test_a_table_bills_and_sells_exactly_like_a_pc(
        self, engine, pool_table, clock
    ):
        """The half of this that needed no new code, asserted so it stays that way."""
        session_id = engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=60)
        sale = engine.end_session(session_id=session_id, payment_method=PaymentMethod.UPI)

        assert sale.amount_paise == rupees(200)
        assert unit_state(pool_table, POOL) is UnitState.AVAILABLE

        with unit_of_work(pool_table) as db:
            assert db.get(Sale, sale.id) is not None

    async def test_a_pc_on_the_same_floor_still_locks(
        self, engine, pool_table, clock, commands
    ):
        """Enforcement is a property of the unit, not a mode the whole venue is in."""
        engine.start_session(unit_id=POOL, duration_minutes=60)
        engine.start_session(unit_id=PC, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        await engine.tick()

        assert unit_state(pool_table, POOL) is UnitState.OVERTIME
        assert unit_state(pool_table, PC) is UnitState.LOCKED
        assert (PC, True) in commands

    async def test_paying_up_clears_the_reminder(self, engine, pool_table, clock):
        """The customer settles and buys another hour; the nagging has to stop."""
        session_id = engine.start_session(unit_id=POOL, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        await engine.tick()

        engine.extend_session(session_id=session_id, minutes=60)
        await engine.tick()

        assert unit_state(pool_table, POOL) is UnitState.ACTIVE
        assert (await engine.tick()).alerts == ()


class TestEnforcementFollowsTheUnitType:
    async def test_the_defaults_match_what_each_type_can_actually_do(self, pool_table):
        """Only a machine running an agent can be held shut. Everything else is a person.

        A console has no operating system to install an agent on, so it sits in the same
        bucket as a pool table: timed, billed, alerted, and dealt with by whoever is at
        the counter.
        """
        with unit_of_work(pool_table) as db:
            assert db.get(Unit, PC).enforcement is EnforcementMode.SOFTWARE
            assert db.get(Unit, PS5).enforcement is EnforcementMode.MANUAL
            assert db.get(Unit, POOL).enforcement is EnforcementMode.MANUAL

    async def test_only_a_software_unit_is_enforced(self, pool_table):
        with unit_of_work(pool_table) as db:
            assert db.get(Unit, PC).is_enforced
            assert not db.get(Unit, PS5).is_enforced
            assert not db.get(Unit, POOL).is_enforced

    async def test_a_console_gets_the_same_treatment_as_a_table(
        self, engine, pool_table, clock, commands
    ):
        """No lock command is dispatched at a PS5, and it never shows LOCKED."""
        engine.start_session(unit_id=PS5, duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        result = await engine.tick()

        assert unit_state(pool_table, PS5) is UnitState.OVERTIME
        assert not any(unit_id == PS5 and lock for unit_id, lock in commands)
        assert any(alert.kind is AlertKind.OVERDUE for alert in result.alerts)

    async def test_a_pc_can_be_set_manual_while_its_agent_is_missing(
        self, factory, clock, commands
    ):
        """The third case this buys: a machine on the floor with no agent installed.

        It still gets timing, billing and alerts. What it does not get is a lock command
        sent into nothing while the manager assumes the machine cut out.
        """
        with unit_of_work(factory) as db:
            db.add_all(
                [
                    Unit(
                        id="unit-new-pc",
                        venue_id=VENUE,
                        name="Nova 12",
                        type=UnitType.PC,
                        enforcement=EnforcementMode.MANUAL,
                    ),
                    Pricing(
                        venue_id=VENUE,
                        unit_type=UnitType.PC,
                        hourly_rate_paise=rupees(120),
                        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                    ),
                ]
            )

        async def sink(unit_id: str, lock: bool) -> None:
            commands.append((unit_id, lock))

        engine = SessionEngine(
            factory, venue_id=VENUE, clock=clock, command_sink=sink
        )
        engine.start_session(unit_id="unit-new-pc", duration_minutes=60)

        clock.advance(minutes=65, seconds=1)
        result = await engine.tick()

        assert unit_state(factory, "unit-new-pc") is UnitState.OVERTIME
        assert not any(lock for _, lock in commands)
        assert {alert.kind for alert in result.alerts} == {AlertKind.OVERDUE}


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
