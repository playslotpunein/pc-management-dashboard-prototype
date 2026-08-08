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
    """One PC, priced."""
    client.post(
        "/pricing",
        json={"unit_type": "pc", "hourly_rate_paise": rupees(120)},
    )

    response = client.post(
        "/units", json={"name": "Nova", "type": "pc", "zone": "Battle Zone"}
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

        assert sale["unit_name"] == "Nova"
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
