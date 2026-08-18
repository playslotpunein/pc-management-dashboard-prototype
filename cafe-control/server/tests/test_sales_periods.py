"""Week and month boundaries for the shift report.

These are pure functions of the clock, so they can be pinned exactly — which matters,
because every one of them is wrong in a way nobody notices until the day it matters. A
month boundary is exercised twelve times a year and a manager only spots it when the
first of the month reports a suspiciously empty till.

The rule under all of them: a café's day rolls over at 6am, not midnight, so the *week*
and the *month* roll over with it. Sunday night's takings belong to that week even when
the clock has gone past midnight into Monday.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from playslot.engine.sales import (
    business_day_start,
    month_start,
    period_starts,
    week_start,
)

SIX_AM = time(6, 0)


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestWeekStart:
    def test_midweek_lands_on_monday_morning(self):
        # Thursday 13 August 2026, mid-evening.
        assert week_start(at(2026, 8, 13, 20), day_starts_at=SIX_AM) == at(2026, 8, 10, 6)

    def test_monday_afternoon_is_already_in_its_own_week(self):
        assert week_start(at(2026, 8, 10, 14), day_starts_at=SIX_AM) == at(2026, 8, 10, 6)

    def test_monday_before_six_still_belongs_to_the_week_that_is_ending(self):
        """The 2am problem.

        At 2am on Monday the venue is working Sunday night. Rolling the week over at
        midnight would file that night's takings under the new week and leave the old
        one's report missing its busiest hours.
        """
        assert week_start(at(2026, 8, 10, 2), day_starts_at=SIX_AM) == at(2026, 8, 3, 6)

    def test_sunday_night_is_the_end_of_its_week_not_the_start_of_the_next(self):
        assert week_start(at(2026, 8, 16, 23), day_starts_at=SIX_AM) == at(2026, 8, 10, 6)


class TestMonthStart:
    def test_midmonth_lands_on_the_first(self):
        assert month_start(at(2026, 8, 13, 20), day_starts_at=SIX_AM) == at(2026, 8, 1, 6)

    def test_the_first_after_six_starts_the_new_month(self):
        assert month_start(at(2026, 9, 1, 10), day_starts_at=SIX_AM) == at(2026, 9, 1, 6)

    def test_the_first_before_six_still_belongs_to_the_month_that_is_ending(self):
        """2am on the 1st is the closing night of the month before."""
        assert month_start(at(2026, 9, 1, 2), day_starts_at=SIX_AM) == at(2026, 8, 1, 6)

    def test_the_last_night_of_the_year_rolls_into_january_correctly(self):
        assert month_start(at(2027, 1, 1, 3), day_starts_at=SIX_AM) == at(2026, 12, 1, 6)
        assert month_start(at(2027, 1, 1, 7), day_starts_at=SIX_AM) == at(2027, 1, 1, 6)

    @pytest.mark.parametrize("month,day", [(2, 28), (4, 30), (12, 31)])
    def test_short_and_long_months_alike(self, month, day):
        assert month_start(at(2026, month, day, 20), day_starts_at=SIX_AM) == at(
            2026, month, 1, 6
        )


class TestHowThePeriodsRelate:
    """Today sits inside both others. The week and the month do *not* nest."""

    @pytest.mark.parametrize(
        "moment",
        [
            at(2026, 8, 13, 20),
            at(2026, 8, 10, 2),
            at(2026, 9, 1, 2),
            at(2026, 9, 1, 7),
            at(2026, 8, 1, 5),
            at(2027, 1, 1, 3),
            at(2026, 9, 3, 20),
        ],
    )
    def test_today_starts_inside_both_of_the_others(self, moment):
        starts = period_starts(moment, day_starts_at=SIX_AM)

        assert starts["today"] >= starts["week"]
        assert starts["today"] >= starts["month"]

    def test_today_always_matches_the_business_day(self):
        moment = at(2026, 8, 13, 20)

        assert period_starts(moment, day_starts_at=SIX_AM)["today"] == business_day_start(
            moment, day_starts_at=SIX_AM
        )

    def test_a_week_that_straddles_a_month_keeps_both_starts(self):
        """Thursday 3 September 2026: the week began in August, the month did not."""
        starts = period_starts(at(2026, 9, 3, 20), day_starts_at=SIX_AM)

        assert starts["week"] == at(2026, 8, 31, 6)
        assert starts["month"] == at(2026, 9, 1, 6)

        # And here the week reaches back further than the month, so a sale on 31 August
        # is in this week but not this month. Nesting is not guaranteed between those
        # two — only that today sits inside both.
        assert starts["week"] < starts["month"] <= starts["today"]


class TestRollupOverAStraddlingWeek:
    """The bug the boundary functions above cannot catch on their own.

    ``rollup_periods`` reads the sales table once and fills all three windows. Anchoring
    that read to the month start looks obviously right and is wrong for the six days a
    year when a week began in the previous month: those days' sales are inside the week
    and outside the query, so the week silently under-reports.
    """

    @staticmethod
    def seed(factory, clock, moments):
        from playslot.db import unit_of_work
        from playslot.enums import PaymentMethod, SessionSource, UnitType
        from playslot.models import Pricing, Sale, Session, Unit
        from playslot.money import rupees

        with unit_of_work(factory) as db:
            db.add_all(
                [
                    Unit(id="u1", venue_id="v", name="PC 1", type=UnitType.PC),
                    Pricing(
                        venue_id="v",
                        unit_type=UnitType.PC,
                        hourly_rate_paise=rupees(120),
                        effective_from=at(2026, 1, 1, 0),
                    ),
                ]
            )

            for index, settled in enumerate(moments):
                db.add(
                    Session(
                        id=f"s{index}",
                        venue_id="v",
                        unit_id="u1",
                        rate_snapshot_paise=rupees(120),
                        duration_minutes=60,
                    )
                )
                db.flush()
                db.add(
                    Sale(
                        id=f"sale{index}",
                        venue_id="v",
                        session_id=f"s{index}",
                        source=SessionSource.WALK_IN,
                        amount_paise=rupees(100),
                        payment_method=PaymentMethod.CASH,
                        settled_at=settled,
                    )
                )

    def test_a_sale_from_the_tail_of_last_month_counts_in_this_week(self, factory):
        from playslot.engine.sales import rollup_periods
        from playslot.db import unit_of_work
        from playslot.money import rupees

        # Thursday 3 September 2026. Its week began Monday 31 August.
        now = at(2026, 9, 3, 20)

        self.seed(
            factory,
            None,
            [
                at(2026, 8, 31, 20),  # Monday night — in the week, not in the month
                at(2026, 9, 2, 20),  # in both
                at(2026, 9, 3, 19),  # today, and in both
            ],
        )

        class NoLiveBills:
            total_paise = 0

        with unit_of_work(factory) as db:
            periods = rollup_periods(
                db,
                venue_id="v",
                now=now,
                day_starts_at=SIX_AM,
                live_bill=lambda _: NoLiveBills(),
            )

        assert periods["today"].closed_paise == rupees(100)
        assert periods["month"].closed_paise == rupees(200)

        # The one that breaks when the query is anchored to the month start.
        assert periods["week"].closed_paise == rupees(300)
