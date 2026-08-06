"""Agent WebSocket tests, over a real socket.

These drive the whole loop: enrol, connect, authenticate, receive state, and confirm a
lock decision made by the session engine actually reaches the agent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from playslot import main
from playslot.clock import FrozenClock
from playslot.db import create_all, create_db_engine, session_factory, unit_of_work
from playslot.engine.session_engine import SessionEngine
from playslot.models import ActivityLog, Agent
from playslot.money import rupees
from playslot.security import build_envelope, sign
from playslot.ws import AgentHub

from .conftest import VENUE


@pytest.fixture
def wired(clock: FrozenClock):
    """App with a hub wired as the engine's command sink, on a frozen clock."""
    db_engine = create_db_engine("sqlite://")
    create_all(db_engine)
    factory = session_factory(db_engine)

    hub = AgentHub(factory, venue_id=VENUE, clock=clock)
    engine = SessionEngine(
        factory, venue_id=VENUE, clock=clock, command_sink=hub.command_sink
    )

    main.settings.venue_id = VENUE
    main.factory_holder["factory"] = factory
    main.engine_holder["engine"] = engine
    main.hub_holder["hub"] = hub

    client = TestClient(main.app)

    client.post("/pricing", json={"unit_type": "pc", "hourly_rate_paise": rupees(120)})
    unit_id = client.post("/units", json={"name": "Nova", "type": "pc"}).json()["id"]
    token = client.post("/agents/enroll", params={"unit_id": unit_id}).json()["device_token"]

    return {
        "client": client,
        "engine": engine,
        "hub": hub,
        "factory": factory,
        "unit_id": unit_id,
        "token": token,
    }


def hello(token: str, unit_id: str) -> dict:
    return build_envelope(
        token, unit_id=unit_id, message_type="hello", body={"agent_version": "1.0.0"}
    )


class TestEnrolment:
    def test_enrolment_returns_a_token_once(self, wired):
        assert len(wired["token"]) == 64

    def test_re_enrolling_rotates_the_secret(self, wired):
        """The revocation path for a stolen token."""
        second = wired["client"].post(
            "/agents/enroll", params={"unit_id": wired["unit_id"]}
        ).json()["device_token"]

        assert second != wired["token"]

    def test_the_token_is_never_listed(self, wired):
        listed = wired["client"].get("/agents").json()

        assert listed[0]["unit_id"] == wired["unit_id"]
        assert "device_token" not in listed[0]

    def test_enrolling_an_unknown_unit_is_404(self, wired):
        response = wired["client"].post("/agents/enroll", params={"unit_id": "nope"})

        assert response.status_code == 404


class TestConnection:
    def test_a_valid_hello_receives_current_state(self, wired):
        client, unit_id, token = wired["client"], wired["unit_id"], wired["token"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            message = socket.receive_json()

        assert message["type"] == "state"
        assert message["body"]["locked"] is False
        assert message["body"]["unit_state"] == "available"

    def test_server_messages_are_signed_too(self, wired):
        """An unsigned server->agent channel is the same hole from the other end."""
        from playslot.security import verify

        client, unit_id, token = wired["client"], wired["unit_id"], wired["token"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            message = socket.receive_json()

        assert verify(token, message)["locked"] is False

    def test_a_bad_signature_never_registers_the_connection(self, wired):
        client, unit_id, hub = wired["client"], wired["unit_id"], wired["hub"]

        forged = hello("f" * 64, unit_id)

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(forged)

            with pytest.raises(Exception):
                socket.receive_json()

        assert not hub.is_connected(unit_id)

    def test_an_unenrolled_unit_is_closed(self, wired):
        client = wired["client"]

        with client.websocket_connect("/agent/unit-does-not-exist") as socket:
            with pytest.raises(Exception):
                socket.receive_json()

    def test_a_failed_verification_reaches_the_activity_feed(self, wired):
        """The architecture asks for attempts to be visible, not merely rejected."""
        client, unit_id, factory = wired["client"], wired["unit_id"], wired["factory"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello("f" * 64, unit_id))

            with pytest.raises(Exception):
                socket.receive_json()

        with unit_of_work(factory) as db:
            failures = (
                db.query(ActivityLog).filter_by(event="agent.verification_failed").all()
            )

            assert len(failures) == 1
            assert failures[0].unit_id == unit_id

        with unit_of_work(factory) as db:
            assert db.query(Agent).filter_by(unit_id=unit_id).one().failed_verifications == 1

    def test_a_non_hello_first_message_is_refused(self, wired):
        client, unit_id, token, hub = (
            wired["client"], wired["unit_id"], wired["token"], wired["hub"]
        )

        premature = build_envelope(
            token, unit_id=unit_id, message_type="heartbeat", body={}
        )

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(premature)

            with pytest.raises(Exception):
                socket.receive_json()

        assert not hub.is_connected(unit_id)


class TestHeartbeat:
    def test_a_heartbeat_updates_the_agent_row(self, wired):
        client, unit_id, token, factory = (
            wired["client"], wired["unit_id"], wired["token"], wired["factory"]
        )

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            socket.receive_json()

            socket.send_json(
                build_envelope(
                    token,
                    unit_id=unit_id,
                    message_type="heartbeat",
                    body={"agent_version": "1.2.3", "state": "active"},
                )
            )

            # Round-trip a second message so the first is certainly processed.
            socket.send_json(
                build_envelope(token, unit_id=unit_id, message_type="hello", body={})
            )

        with unit_of_work(factory) as db:
            agent = db.query(Agent).filter_by(unit_id=unit_id).one()

            assert agent.last_heartbeat is not None
            assert agent.agent_version == "1.2.3"


class TestLockDelivery:
    async def test_a_grace_timeout_reaches_the_agent(self, wired, clock):
        """The whole point: an engine decision arrives at the machine.

        Session expires, grace runs out, the engine locks the unit — and the agent
        receives a signed state message telling it to lock.
        """
        client, unit_id, token, engine = (
            wired["client"], wired["unit_id"], wired["token"], wired["engine"]
        )

        session_id = client.post(
            "/sessions", json={"unit_id": unit_id, "duration_minutes": 60}
        ).json()["id"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            first = socket.receive_json()

            # Freshly started: unlocked, with an end time cached for the fail-safe.
            assert first["body"]["locked"] is False
            assert first["body"]["session_end_utc"] is not None

            clock.advance(minutes=66)
            await engine.tick()

            locked = socket.receive_json()

        assert locked["body"]["locked"] is True
        assert locked["body"]["unit_state"] == "locked"

    async def test_an_extension_unlocks_the_agent(self, wired, clock):
        """Customer pays for more time; the machine must actually come back."""
        client, unit_id, token, engine = (
            wired["client"], wired["unit_id"], wired["token"], wired["engine"]
        )

        session_id = client.post(
            "/sessions", json={"unit_id": unit_id, "duration_minutes": 60}
        ).json()["id"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            socket.receive_json()

            clock.advance(minutes=66)
            await engine.tick()
            assert socket.receive_json()["body"]["locked"] is True

            client.post(f"/sessions/{session_id}/extend", json={"minutes": 30})
            await engine.tick()

            released = socket.receive_json()

        assert released["body"]["locked"] is False

    async def test_a_disconnected_agent_does_not_break_the_tick(self, wired, clock):
        """A dead link is the agent's problem to survive, not the engine's to retry."""
        client, unit_id, engine = wired["client"], wired["unit_id"], wired["engine"]

        client.post("/sessions", json={"unit_id": unit_id, "duration_minutes": 60})

        clock.advance(minutes=66)
        result = await engine.tick()

        # The engine still transitioned the unit; delivery simply failed.
        assert result.lock_commands == (unit_id,)


class TestFailSafePayload:
    def test_state_carries_the_end_time_for_the_agents_cache(self, wired, clock):
        """Zone 5 depends on this: without an end time there is nothing to fail safe on."""
        client, unit_id, token = wired["client"], wired["unit_id"], wired["token"]

        client.post("/sessions", json={"unit_id": unit_id, "duration_minutes": 60})

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            body = socket.receive_json()["body"]

        assert body["session_end_utc"].startswith("2026-08-06T18:00")
        assert body["grace_end_utc"].startswith("2026-08-06T18:05")

    def test_an_open_ended_session_has_no_end_time(self, wired):
        """Null must read as 'no deadline', never as 'expired'.

        A walk-in paying by the minute would otherwise be locked out by their own
        agent the moment the network hiccuped.
        """
        client, unit_id, token = wired["client"], wired["unit_id"], wired["token"]

        client.post("/sessions", json={"unit_id": unit_id, "duration_minutes": 0})

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            body = socket.receive_json()["body"]

        assert body["locked"] is False
        assert body["session_end_utc"] is None


class TestCacheSeeding:
    async def test_starting_a_session_pushes_the_end_time(self, wired, clock):
        """The agent's fail-safe is useless without this.

        Starting a session changes no lock state, so an engine that only pushed on lock
        transitions would leave the agent's cache reading "no session" — and it would
        never lock, however long the customer stayed after the link dropped.
        """
        client, unit_id, token, engine = (
            wired["client"], wired["unit_id"], wired["token"], wired["engine"]
        )

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))

            idle = socket.receive_json()["body"]
            assert idle["session_end_utc"] is None

            client.post("/sessions", json={"unit_id": unit_id, "duration_minutes": 60})
            await engine.tick()

            seeded = socket.receive_json()["body"]

        assert seeded["locked"] is False
        assert seeded["session_end_utc"].startswith("2026-08-06T18:00")

    async def test_ending_a_session_clears_the_cached_end_time(self, wired, clock):
        """Otherwise the next customer is failed safe against the last one's deadline."""
        client, unit_id, token, engine = (
            wired["client"], wired["unit_id"], wired["token"], wired["engine"]
        )

        session_id = client.post(
            "/sessions", json={"unit_id": unit_id, "duration_minutes": 60}
        ).json()["id"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            assert socket.receive_json()["body"]["session_end_utc"] is not None

            clock.advance(minutes=30)
            client.post(f"/sessions/{session_id}/end", json={})
            await engine.tick()

            cleared = socket.receive_json()["body"]

        assert cleared["locked"] is False
        assert cleared["session_end_utc"] is None

    async def test_extending_pushes_the_new_deadline(self, wired, clock):
        client, unit_id, token, engine = (
            wired["client"], wired["unit_id"], wired["token"], wired["engine"]
        )

        session_id = client.post(
            "/sessions", json={"unit_id": unit_id, "duration_minutes": 60}
        ).json()["id"]

        with client.websocket_connect(f"/agent/{unit_id}") as socket:
            socket.send_json(hello(token, unit_id))
            first = socket.receive_json()["body"]
            assert first["session_end_utc"].startswith("2026-08-06T18:00")

            client.post(f"/sessions/{session_id}/extend", json={"minutes": 30})
            await engine.tick()

            extended = socket.receive_json()["body"]

        # Stale here would have the agent lock someone who just paid for more time.
        assert extended["session_end_utc"].startswith("2026-08-06T18:30")
