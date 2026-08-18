"""API tests against the real app, over HTTP.

These exercise the routes end to end: real FastAPI, real SQLAlchemy, real SQLite. The
session engine's background loop is not started, so the clock does not move underneath
an assertion.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from playslot import main
from playslot.clock import FrozenClock
from playslot.db import create_all, create_db_engine, session_factory
from playslot.engine.session_engine import SessionEngine
from playslot.money import rupees

from .conftest import VENUE


@pytest.fixture
def client(clock: FrozenClock):
    db_engine = create_db_engine("sqlite://")
    create_all(db_engine)
    factory = session_factory(db_engine)

    main.settings.venue_id = VENUE
    main.factory_holder["factory"] = factory
    main.engine_holder["engine"] = SessionEngine(
        factory, venue_id=VENUE, clock=clock
    )

    # Deliberately NOT used as a context manager. Entering `with TestClient(...)` runs
    # the app's lifespan, which would build its own file-backed database and its own
    # engine on a real Clock — overwriting the in-memory database and frozen clock
    # injected above, and starting the background tick underneath every assertion.
    return TestClient(main.app)


@pytest.fixture
def stocked(client):
    """One PC, priced. It locks when grace runs out, so it never accrues overtime."""
    client.post(
        "/pricing",
        json={"unit_type": "pc", "hourly_rate_paise": rupees(120)},
    )

    response = client.post(
        "/units", json={"name": "PC 1", "type": "pc", "zone": "Battle Zone"}
    )

    return response.json()["id"]


@pytest.fixture
def stocked_table(client):
    """One pool table, priced, with no overtime penalty rate.

    The combination that actually bills overtime: nothing locks it, so an overrun is real
    play rather than a customer staring at a locked screen.
    """
    client.post(
        "/pricing",
        json={"unit_type": "pool", "hourly_rate_paise": rupees(120)},
    )

    response = client.post(
        "/units", json={"name": "Pool 1", "type": "pool", "zone": "Upstairs"}
    )

    return response.json()["id"]


class TestHealth:
    def test_health_reports_the_venue(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestUnits:
    def test_a_new_unit_starts_available(self, client, stocked):
        units = client.get("/units").json()

        assert len(units) == 1
        assert units[0]["state"] == "available"
        assert units[0]["remaining_seconds"] is None

    def test_maintenance_is_refused_on_a_busy_unit(self, client, stocked):
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})

        response = client.post(f"/units/{stocked}/maintenance", params={"on": True})

        assert response.status_code == 409
        assert "active" in response.json()["detail"]


class TestSessionsOverHttp:
    def test_start_returns_the_snapshotted_rate(self, client, stocked):
        response = client.post(
            "/sessions",
            json={"unit_id": stocked, "duration_minutes": 60, "customer_ref": "Rohan"},
        )

        assert response.status_code == 201
        assert response.json()["rate_snapshot_paise"] == rupees(120)

    def test_a_second_session_on_a_busy_unit_is_a_conflict(self, client, stocked):
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})

        response = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        )

        assert response.status_code == 409

    def test_the_unit_list_carries_the_live_countdown(self, client, stocked, clock):
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})

        clock.advance(minutes=30)
        unit = client.get("/units").json()[0]

        assert unit["state"] == "active"
        assert unit["remaining_seconds"] == pytest.approx(1800, abs=2)
        assert unit["running_total"] == "₹120.00"

    def test_bill_preview_does_not_end_the_session(self, client, stocked, clock):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=30)
        bill = client.get(f"/sessions/{session_id}/bill").json()

        assert bill["total"] == "₹120.00"
        assert client.get("/units").json()[0]["state"] == "active"

    def test_ending_an_overrun_table_charges_for_the_overrun(
        self, client, stocked_table, clock
    ):
        """The bug this covers: overtime silently costing nothing.

        ``stocked_table`` prices the unit without an overtime penalty, which is what the
        pricing form submits by default. That used to mean overtime was skipped entirely,
        so a customer who booked an hour and played an hour and a half paid for the hour
        and walked out — with the dashboard showing a tidy, wrong total at the counter.
        """
        session_id = client.post(
            "/sessions", json={"unit_id": stocked_table, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=95)

        bill = client.get(f"/sessions/{session_id}/bill").json()

        assert bill["overtime_minutes"] == 30

        overtime = [line for line in bill["lines"] if line["kind"] == "overtime"]

        assert overtime, "no overtime line on a table 30 minutes past grace"
        assert overtime[0]["amount_paise"] == rupees(60)

        sale = client.post(f"/sessions/{session_id}/end", json={}).json()

        # ₹120 for the booked hour, plus 30 min at the same ₹120/hr.
        assert sale["amount_paise"] == rupees(180)

    def test_a_locked_pc_is_not_charged_for_the_time_it_spent_locked(
        self, client, stocked, clock
    ):
        """The other half, and the reason overtime cannot simply always be charged.

        A PC locks the moment grace runs out, so from then on the customer is staring at
        a locked screen. The session stays open until someone at the counter closes it,
        which on a busy evening might be an hour — billing that hour charges them for a
        machine the system itself shut off.
        """
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=180)

        bill = client.get(f"/sessions/{session_id}/bill").json()

        assert bill["actual_minutes"] == 180
        assert bill["overtime_minutes"] == 0
        assert bill["unbilled_minutes"] == 115
        assert [line["kind"] for line in bill["lines"]] == ["base"]

        sale = client.post(f"/sessions/{session_id}/end", json={}).json()

        # The booked hour, and not one paisa for the two hours it sat locked.
        assert sale["amount_paise"] == rupees(120)

    def test_the_stored_sale_keeps_the_overtime_line(self, client, stocked_table, clock):
        """So the manager can still answer "why was it that much?" a week later."""
        session_id = client.post(
            "/sessions", json={"unit_id": stocked_table, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=95)
        client.post(f"/sessions/{session_id}/end", json={})

        sale = client.get("/sales").json()[0]
        kinds = [line["kind"] for line in sale["lines"]]

        assert "overtime" in kinds
        assert sum(line["amount_paise"] for line in sale["lines"]) == sale["amount_paise"]

    def test_the_preview_and_the_sale_agree(self, client, stocked_table, clock):
        """They are two calls into the same computation and must not diverge."""
        session_id = client.post(
            "/sessions", json={"unit_id": stocked_table, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=95)

        preview = client.get(f"/sessions/{session_id}/bill").json()
        sale = client.post(f"/sessions/{session_id}/end", json={}).json()

        assert preview["total_paise"] == sale["amount_paise"]

    def test_extend_then_end_bills_both(self, client, stocked, clock):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        client.post(f"/sessions/{session_id}/extend", json={"minutes": 30})
        clock.advance(minutes=90)

        sale = client.post(
            f"/sessions/{session_id}/end", json={"payment_method": "upi"}
        ).json()

        assert sale["amount_paise"] == rupees(180)
        assert sale["payment_method"] == "upi"
        assert client.get("/units").json()[0]["state"] == "available"

    def test_ending_twice_is_a_conflict(self, client, stocked, clock):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=60)
        client.post(f"/sessions/{session_id}/end", json={})

        response = client.post(f"/sessions/{session_id}/end", json={})

        assert response.status_code == 409

    def test_a_negative_extension_is_rejected_by_validation(self, client, stocked):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        response = client.post(f"/sessions/{session_id}/extend", json={"minutes": -10})

        assert response.status_code == 422


class TestSalesRollup:
    def test_live_sessions_are_included_in_the_rollup(self, client, stocked, clock):
        """The manager has to see what is owed on the floor, not just what is paid."""
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})
        clock.advance(minutes=30)

        rollup = client.get("/sales/today").json()

        assert rollup["closed_paise"] == 0
        assert rollup["live_paise"] == rupees(120)
        assert rollup["total_paise"] == rupees(120)

    def test_closed_and_live_are_reported_separately(self, client, stocked, clock):
        first = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=60)
        client.post(f"/sessions/{first}/end", json={"payment_method": "cash"})

        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 30})
        clock.advance(minutes=10)

        rollup = client.get("/sales/today").json()

        assert rollup["closed_paise"] == rupees(120)
        assert rollup["live_paise"] == rupees(60)
        assert rollup["by_payment_method"]["cash"] == rupees(120)

    def test_rollup_splits_by_unit_type(self, client, stocked, clock):
        client.post("/pricing", json={"unit_type": "ps5", "hourly_rate_paise": rupees(180)})
        ps5 = client.post("/units", json={"name": "Console 1", "type": "ps5"}).json()["id"]

        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})
        client.post("/sessions", json={"unit_id": ps5, "duration_minutes": 60})

        clock.advance(minutes=10)
        by_type = {row["unit_type"]: row for row in client.get("/sales/today").json()["by_type"]}

        assert by_type["pc"]["live_paise"] == rupees(120)
        assert by_type["ps5"]["live_paise"] == rupees(180)


class TestPricingHistory:
    def test_a_price_change_inserts_rather_than_mutates(self, client, stocked, clock):
        clock.advance(minutes=1)
        client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(220)})

        rows = client.get("/pricing").json()

        # Both rows survive; history is never rewritten.
        assert len(rows) == 2
        assert {row["hourly_rate_paise"] for row in rows} == {rupees(120), rupees(220)}

    def test_a_running_session_is_untouched_by_a_price_change(
        self, client, stocked, clock
    ):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=30)
        client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(220)})
        clock.advance(minutes=30)

        sale = client.post(f"/sessions/{session_id}/end", json={}).json()

        assert sale["amount_paise"] == rupees(120)


class TestOpenEndedNeverLeaksTheSentinel:
    def test_an_open_ended_session_reports_no_deadline(self, client, stocked, clock):
        """The engine uses a sentinel internally; the API must not forward it.

        An open-ended walk-in has no deadline. Sending the internal "forever" value
        would render a countdown roughly eleven thousand days long on the unit card.
        """
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 0})

        clock.advance(minutes=30)
        unit = client.get("/units").json()[0]

        assert unit["state"] == "active"
        assert unit["remaining_seconds"] is None
        assert unit["grace_remaining_seconds"] is None

        # Time used is still billed — no deadline is not the same as no charge.
        assert unit["running_total"] == "₹60.00"

    def test_a_booked_session_still_reports_its_countdown(self, client, stocked, clock):
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})

        clock.advance(minutes=30)
        unit = client.get("/units").json()[0]

        assert unit["remaining_seconds"] == pytest.approx(1800, abs=2)


class TestSalesListForTheShiftReport:
    def test_a_sale_carries_the_unit_and_customer(self, client, stocked, clock):
        """A row reading "session 3f9a-…, ₹120" is no use when reconciling the till."""
        session_id = client.post(
            "/sessions",
            json={"unit_id": stocked, "duration_minutes": 60, "customer_ref": "Rohan M."},
        ).json()["id"]

        clock.advance(minutes=60)
        client.post(f"/sessions/{session_id}/end", json={"payment_method": "upi"})

        sale = client.get("/sales").json()[0]

        assert sale["unit_name"] == "PC 1"
        assert sale["customer_ref"] == "Rohan M."
        assert sale["amount"] == "₹120.00"
        assert sale["payment_method"] == "upi"

    def test_the_list_is_scoped_to_the_business_day(self, client, stocked, clock):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        clock.advance(minutes=60)
        client.post(f"/sessions/{session_id}/end", json={})

        assert len(client.get("/sales").json()) == 1

        # Two days on, yesterday's takings must not pad today's shift report.
        clock.advance(hours=48)

        assert client.get("/sales").json() == []
        assert len(client.get("/sales", params={"all_days": True}).json()) == 1

    def test_the_stored_breakdown_comes_back_with_the_sale(self, client, stocked, clock):
        session_id = client.post(
            "/sessions", json={"unit_id": stocked, "duration_minutes": 60}
        ).json()["id"]

        client.post(f"/sessions/{session_id}/extend", json={"minutes": 30})
        clock.advance(minutes=90)
        client.post(f"/sessions/{session_id}/end", json={})

        sale = client.get("/sales").json()[0]
        kinds = [line["kind"] for line in sale["lines"]]

        assert "base" in kinds and "extension" in kinds
        assert sum(line["amount_paise"] for line in sale["lines"]) == sale["amount_paise"]


class TestPricingPanel:
    def test_the_live_row_is_flagged_per_unit_type(self, client, stocked, clock):
        client.post("/pricing", json={"unit_type": "ps5", "hourly_rate_paise": rupees(180)})

        clock.advance(minutes=5)
        client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(150)})

        rows = client.get("/pricing").json()
        current = {r["unit_type"]: r for r in rows if r["is_current"]}

        assert current["pc"]["hourly_rate_paise"] == rupees(150)
        assert current["ps5"]["hourly_rate_paise"] == rupees(180)

        # Exactly one live row per type, whatever the history depth.
        assert len([r for r in rows if r["is_current"]]) == 2

    def test_a_future_dated_row_is_not_marked_current(self, client, stocked, clock):
        """Scheduled is not live. Showing it as live would misprice a session."""
        future = (clock.now() + timedelta(days=1)).isoformat()

        client.post(
            "/pricing",
            json={"unit_type": "pc", "hourly_rate_paise": rupees(999), "effective_from": future},
        )

        rows = client.get("/pricing").json()
        current = next(r for r in rows if r["is_current"])

        assert current["hourly_rate_paise"] == rupees(120)
        assert any(not r["is_current"] and r["hourly_rate_paise"] == rupees(999) for r in rows)

    def test_history_is_returned_in_full(self, client, stocked, clock):
        clock.advance(minutes=1)
        client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(150)})
        clock.advance(minutes=1)
        client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(200)})

        rows = [r for r in client.get("/pricing").json() if r["unit_type"] == "pc"]

        # Three rows survive; a price change never rewrites what came before.
        assert len(rows) == 3
        assert rows[0]["is_current"] is True
        assert rows[0]["hourly_rate_paise"] == rupees(200)


class TestSalesPeriodsOverHttp:
    """Today, this week and this month. The clock fixture sits on Thursday 6 August 2026,
    whose week began Monday the 3rd and whose month began the 1st."""

    @staticmethod
    def settle(client, unit_id, when):
        """Close a session and backdate its sale, standing in for an earlier shift.

        Reaches for the app's own factory rather than the ``factory`` fixture: the client
        builds a separate in-memory database, and writing to the other one leaves the
        sale in place and the test quietly asserting nothing.
        """
        from playslot.db import unit_of_work
        from playslot.models import Sale

        session_id = client.post(
            "/sessions", json={"unit_id": unit_id, "duration_minutes": 60}
        ).json()["id"]

        sale_id = client.post(f"/sessions/{session_id}/end", json={}).json()["id"]

        with unit_of_work(main.factory_holder["factory"]) as db:
            db.get(Sale, sale_id).settled_at = when

        return sale_id

    def test_the_three_windows_widen(self, client, stocked, clock):
        from datetime import UTC, datetime

        # One today, one on Tuesday (this week), one on the 2nd (this month only).
        self.settle(client, stocked, datetime(2026, 8, 6, 15, tzinfo=UTC))
        self.settle(client, stocked, datetime(2026, 8, 4, 15, tzinfo=UTC))
        self.settle(client, stocked, datetime(2026, 8, 2, 15, tzinfo=UTC))

        summary = client.get("/sales/summary").json()

        assert summary["today"]["closed_paise"] == rupees(120)
        assert summary["week"]["closed_paise"] == rupees(240)
        assert summary["month"]["closed_paise"] == rupees(360)

    def test_each_window_reports_where_it_starts(self, client, stocked):
        summary = client.get("/sales/summary").json()

        assert summary["today"]["since"].startswith("2026-08-06T06:00")
        assert summary["week"]["since"].startswith("2026-08-03T06:00")
        assert summary["month"]["since"].startswith("2026-08-01T06:00")

    def test_owed_on_the_floor_is_the_same_in_all_three(self, client, stocked, clock):
        """It is a fact about right now, not an aggregate over a window."""
        client.post("/sessions", json={"unit_id": stocked, "duration_minutes": 60})
        clock.advance(minutes=30)

        summary = client.get("/sales/summary").json()
        live = {summary[period]["live_paise"] for period in ("today", "week", "month")}

        assert live == {rupees(120)}

    def test_the_list_follows_the_period(self, client, stocked):
        from datetime import UTC, datetime

        self.settle(client, stocked, datetime(2026, 8, 6, 15, tzinfo=UTC))
        self.settle(client, stocked, datetime(2026, 8, 4, 15, tzinfo=UTC))
        self.settle(client, stocked, datetime(2026, 8, 2, 15, tzinfo=UTC))

        assert len(client.get("/sales?period=today").json()) == 1
        assert len(client.get("/sales?period=week").json()) == 2
        assert len(client.get("/sales?period=month").json()) == 3

    def test_the_list_defaults_to_today(self, client, stocked):
        from datetime import UTC, datetime

        self.settle(client, stocked, datetime(2026, 8, 4, 15, tzinfo=UTC))

        assert client.get("/sales").json() == []

    def test_an_unknown_period_is_rejected_rather_than_silently_widened(self, client):
        """A typo must not quietly report the month as though it were today."""
        assert client.get("/sales?period=year").status_code == 422

    def test_summary_and_today_agree(self, client, stocked):
        from datetime import UTC, datetime

        self.settle(client, stocked, datetime(2026, 8, 6, 15, tzinfo=UTC))

        summary = client.get("/sales/summary").json()["today"]
        today = client.get("/sales/today").json()

        assert summary["closed_paise"] == today["closed_paise"]
        assert summary["total_paise"] == today["total_paise"]
