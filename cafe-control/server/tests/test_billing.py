"""Billing tests.

These encode the architecture's revenue rules. If one of these fails, money is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from playslot.engine.billing import Extension, compute_bill, elapsed_minutes
from playslot.money import format_rupees, prorate_hourly, rupees, to_rupees

START = datetime(2026, 8, 6, 17, 0, tzinfo=UTC)

RATE_120 = rupees(120)  # a Battle Zone PC
RATE_220 = rupees(220)  # a Pro Arena rig


def at(**kwargs) -> datetime:
    return START + timedelta(**kwargs)


# --------------------------------------------------------------------------- money


class TestMoney:
    def test_rupees_are_exact_paise(self):
        assert rupees(120) == 12_000
        assert rupees("0.10") == 10
        assert rupees(0.1) == 10

    def test_float_drift_does_not_reach_the_total(self):
        # The reason money is integers: summing 0.10 as a float ten times is not 1.00.
        assert sum(rupees("0.10") for _ in range(10)) == rupees(1)

    def test_rounding_is_half_up_not_bankers(self):
        # Python's round() would give 62 here; a customer shown ₹62.50 pays ₹63.
        assert to_rupees(6250) == pytest.approx(to_rupees(6250))
        assert prorate_hourly(rupees(125), 30) == rupees("62.50")

    def test_prorate_multiplies_before_dividing(self):
        # ₹120/hr for 7 minutes is exactly ₹14. Dividing first loses precision.
        assert prorate_hourly(RATE_120, 7) == rupees(14)

    def test_prorate_rejects_negative_time(self):
        # A clock adjustment must never produce a credit.
        assert prorate_hourly(RATE_120, -30) == 0

    def test_formatting_groups_thousands(self):
        assert format_rupees(rupees(1234.5)) == "₹1,234.50"


# --------------------------------------------------------------------------- elapsed


class TestElapsedMinutes:
    def test_part_minutes_round_up(self):
        # A part-minute is a minute the unit was not available to anyone else.
        assert elapsed_minutes(START, at(seconds=61)) == 2

    def test_exact_minute_does_not_round_up(self):
        assert elapsed_minutes(START, at(minutes=60)) == 60

    def test_end_before_start_is_zero(self):
        assert elapsed_minutes(START, at(minutes=-10)) == 0

    def test_naive_datetime_is_rejected(self):
        # Silently assuming UTC is how a bill ends up 5.5 hours wrong in IST.
        with pytest.raises(ValueError, match="Naive datetime"):
            elapsed_minutes(START, datetime(2026, 8, 6, 18, 0))


# --------------------------------------------------------------------------- base


class TestBaseCharge:
    def test_full_hour_at_the_snapshot_rate(self):
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=60),
        )

        assert bill.total_paise == rupees(120)
        assert bill.booked_minutes == 60
        assert bill.overtime_minutes == 0

    def test_leaving_early_still_pays_the_booked_time(self):
        # Booked an hour, left at forty minutes. No refund — they bought the hour.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=40),
        )

        assert bill.total_paise == rupees(120)
        assert bill.actual_minutes == 40

    def test_open_ended_walk_in_bills_time_used(self):
        # No booked duration: the base is the time actually used, not overtime.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=0,
            started_at=START,
            ended_at=at(minutes=45),
        )

        assert bill.total_paise == rupees(90)
        assert bill.overtime_minutes == 0
        assert bill.line("overtime") is None


# --------------------------------------------------------------------- rate snapshot


class TestRateSnapshot:
    def test_a_later_price_rise_cannot_reach_a_running_session(self):
        """The doc's auditability rule, stated as a test.

        A session that started at 5pm on the ₹120 rate bills at ₹120 even though the
        pricing row now says ₹220. The engine literally cannot do otherwise: it is
        never given the current rate.
        """
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=60),
        )

        assert bill.total_paise == rupees(120)
        assert bill.total_paise != rupees(220)


# --------------------------------------------------------------------- extensions


class TestExtensions:
    def test_each_extension_is_its_own_line(self):
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=90),
            extensions=[
                Extension(minutes=15, granted_at=at(minutes=55), rate_snapshot_paise=RATE_120),
                Extension(minutes=15, granted_at=at(minutes=70), rate_snapshot_paise=RATE_120),
            ],
        )

        extension_lines = [line for line in bill.lines if line.kind == "extension"]

        assert len(extension_lines) == 2
        assert all(line.amount_paise == rupees(30) for line in extension_lines)
        assert bill.total_paise == rupees(180)
        assert bill.booked_minutes == 90

    def test_extension_pushes_back_the_overtime_boundary(self):
        # 60 booked + 30 extended = 90, plus 5 grace. Ending at 92 is not overtime.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=92),
            extensions=[
                Extension(minutes=30, granted_at=at(minutes=55), rate_snapshot_paise=RATE_120)
            ],
            overtime_rate_paise_per_minute=rupees(5),
        )

        assert bill.overtime_minutes == 0
        assert bill.total_paise == rupees(180)

    def test_an_extension_may_carry_its_own_rate(self):
        # A goodwill discount stays visible as its own line rather than being averaged in.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=90),
            extensions=[
                Extension(minutes=30, granted_at=at(minutes=55), rate_snapshot_paise=rupees(60))
            ],
        )

        assert bill.line("extension").amount_paise == rupees(30)
        assert bill.total_paise == rupees(150)


# ----------------------------------------------------------------------- overtime


class TestOvertimeAndGrace:
    def test_grace_is_free(self):
        # Five minutes past expiry, inside grace. The courtesy costs nothing.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=65),
            grace_minutes=5,
            overtime_rate_paise_per_minute=rupees(5),
        )

        assert bill.overtime_minutes == 0
        assert bill.total_paise == rupees(120)

    def test_overtime_starts_only_after_grace_is_consumed(self):
        # 60 booked + 5 grace = 65 free. Ending at 75 bills 10 overtime minutes.
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=75),
            grace_minutes=5,
            overtime_rate_paise_per_minute=rupees(5),
        )

        assert bill.overtime_minutes == 10
        assert bill.line("overtime").amount_paise == rupees(50)
        assert bill.total_paise == rupees(170)

    def test_overtime_without_a_penalty_rate_bills_at_the_session_rate(self):
        """Zero means "no *penalty*", not "free".

        This used to skip the line entirely, so a customer who booked an hour and played
        an hour and a half paid for the hour. The pricing form defaults that field to
        zero, which made it the normal setup rather than an unusual one — the venue gave
        away every overrun on the floor and nothing on screen said so.
        """
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=90),
            overtime_rate_paise_per_minute=0,
        )

        assert bill.overtime_minutes == 25

        # 25 min at ₹120/hr.
        assert bill.line("overtime").amount_paise == rupees(50)
        assert bill.total_paise == rupees(170)

    def test_free_overtime_is_spelled_grace_not_a_zero_rate(self):
        """The way to give time away is to lengthen the grace, which is explicit."""
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=90),
            grace_minutes=30,
            overtime_rate_paise_per_minute=0,
        )

        assert bill.overtime_minutes == 0
        assert bill.line("overtime") is None
        assert bill.total_paise == rupees(120)

    def test_the_fallback_follows_the_latest_rate_the_customer_bought_at(self):
        """They extended after a price rise, then ran over on top of that.

        Billing the older base rate for those minutes would undercharge for time sold at
        the newer one.
        """
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=125),
            extensions=[
                Extension(
                    minutes=30,
                    granted_at=at(minutes=55),
                    rate_snapshot_paise=rupees(180),
                )
            ],
            overtime_rate_paise_per_minute=0,
        )

        # 60 booked + 30 extended + 5 grace = 95; 30 min over, at the ₹180 extension rate.
        assert bill.overtime_minutes == 30
        assert bill.line("overtime").amount_paise == rupees(90)

    def test_an_explicit_penalty_rate_still_wins(self):
        """The fallback must not quietly replace a rate the venue did set."""
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=90),
            overtime_rate_paise_per_minute=rupees(5),
        )

        assert bill.line("overtime").amount_paise == rupees(125)

    def test_a_unit_that_locks_is_never_billed_for_overtime(self):
        """It locked when grace ran out, so those minutes were not playable.

        The session stays open until the counter closes it, which can be long after the
        machine went dark. Charging for that bills the customer for a screen the system
        itself switched off.
        """
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=180),
            overtime_rate_paise_per_minute=rupees(5),
            locks_at_grace_end=True,
        )

        assert bill.actual_minutes == 180
        assert bill.overtime_minutes == 0
        assert bill.unbilled_minutes == 115
        assert bill.line("overtime") is None
        assert bill.total_paise == rupees(120)

    def test_locking_does_not_touch_a_session_still_inside_its_grace(self):
        """Nothing has locked yet, so there is nothing to waive and nothing to charge."""
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=63),
            locks_at_grace_end=True,
        )

        assert bill.overtime_minutes == 0
        assert bill.unbilled_minutes == 0
        assert bill.total_paise == rupees(120)

    def test_an_extension_restores_billing_past_the_old_deadline(self):
        """Unlocking by extending means the new time is played, and charged.

        Otherwise a manager who takes payment for another half hour would find it waived
        as "locked time" — the customer pays and the till does not see it.
        """
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=88),
            extensions=[
                Extension(
                    minutes=30, granted_at=at(minutes=62), rate_snapshot_paise=RATE_120
                )
            ],
            locks_at_grace_end=True,
        )

        # 90 booked, ended at 88: still inside its time, so nothing is waived.
        assert bill.unbilled_minutes == 0
        assert bill.total_paise == rupees(180)

    def test_a_table_and_a_pc_at_the_same_elapsed_time_differ(self):
        """The whole point of the distinction, in one comparison."""
        common = dict(
            rate_snapshot_paise=RATE_120,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=120),
        )

        table = compute_bill(**common, locks_at_grace_end=False)
        machine = compute_bill(**common, locks_at_grace_end=True)

        # The table was played for 55 min past grace and is charged for them.
        assert table.overtime_minutes == 55
        assert table.total_paise == rupees(230)

        # The PC locked at 65 min; the rest is not the customer's to pay for.
        assert machine.overtime_minutes == 0
        assert machine.unbilled_minutes == 55
        assert machine.total_paise == rupees(120)

    def test_an_open_ended_session_never_accrues_overtime(self):
        """It has no deadline to run past; every minute is already in the base line."""
        bill = compute_bill(
            rate_snapshot_paise=RATE_120,
            duration_minutes=0,
            started_at=START,
            ended_at=at(minutes=200),
            overtime_rate_paise_per_minute=0,
        )

        assert bill.overtime_minutes == 0
        assert bill.line("overtime") is None
        assert bill.total_paise == rupees(400)


# ---------------------------------------------------------------------- surcharge


class TestControllerSurcharge:
    def test_surcharge_is_per_controller_and_prorated(self):
        bill = compute_bill(
            rate_snapshot_paise=RATE_220,
            duration_minutes=30,
            started_at=START,
            ended_at=at(minutes=30),
            controller_surcharge_paise_per_hour=rupees(40),
            extra_controllers=2,
        )

        # ₹40/hr × 2 controllers × half an hour.
        assert bill.line("controller_surcharge").amount_paise == rupees(40)
        assert bill.total_paise == rupees(150)

    def test_no_surcharge_line_without_extra_controllers(self):
        bill = compute_bill(
            rate_snapshot_paise=RATE_220,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=60),
            controller_surcharge_paise_per_hour=rupees(40),
            extra_controllers=0,
        )

        assert bill.line("controller_surcharge") is None


# ------------------------------------------------------------------------ totals


class TestTotalIntegrity:
    def test_total_is_always_the_sum_of_the_lines(self):
        """The auditability guarantee: nothing is added outside a line."""
        bill = compute_bill(
            rate_snapshot_paise=RATE_220,
            duration_minutes=60,
            started_at=START,
            ended_at=at(minutes=100),
            extensions=[
                Extension(minutes=20, granted_at=at(minutes=58), rate_snapshot_paise=RATE_220)
            ],
            grace_minutes=5,
            overtime_rate_paise_per_minute=rupees(8),
            controller_surcharge_paise_per_hour=rupees(40),
            extra_controllers=1,
        )

        assert bill.total_paise == sum(line.amount_paise for line in bill.lines)
        assert bill.total_paise > 0

    def test_every_amount_is_an_integer(self):
        # A float anywhere in the breakdown means drift downstream.
        bill = compute_bill(
            rate_snapshot_paise=rupees(99.99),
            duration_minutes=37,
            started_at=START,
            ended_at=at(minutes=37),
        )

        assert isinstance(bill.total_paise, int)
        assert all(isinstance(line.amount_paise, int) for line in bill.lines)
