"""Alert fan-out tests.

The engine raises alerts whether or not anyone is watching — that is asserted in
test_engine.py. These cover the delivery channel on top: does a connected dashboard get
them, and can a wedged one take the floor down.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from playslot.engine.alerts import Alert
from playslot.enums import AlertKind
from playslot.events import AlertBroker, sse, sse_comment

AT = datetime(2026, 8, 6, 18, 0, tzinfo=UTC)


def alert(kind: AlertKind = AlertKind.FIVE_MINUTE_WARNING, unit: str = "u1", **kwargs) -> Alert:
    return Alert(
        kind=kind,
        unit_id=unit,
        session_id=kwargs.get("session_id", "s1"),
        message=kwargs.get("message", "Nova: 5 min remaining"),
        triggers_lock=kwargs.get("triggers_lock", False),
    )


class TestFanOut:
    async def test_every_subscriber_receives_an_alert(self):
        """A counter screen and an office screen both need to see it."""
        broker = AlertBroker()
        first, second = broker.subscribe(), broker.subscribe()

        await broker.publish([alert()], at=AT)

        assert (await first.get())["message"] == "Nova: 5 min remaining"
        assert (await second.get())["message"] == "Nova: 5 min remaining"

    async def test_publishing_with_nobody_listening_is_fine(self):
        """The engine must not care whether a dashboard is open."""
        broker = AlertBroker()

        await broker.publish([alert()], at=AT)

        assert broker.subscriber_count == 0

    async def test_unsubscribing_stops_delivery(self):
        broker = AlertBroker()
        queue = broker.subscribe()
        broker.unsubscribe(queue)

        await broker.publish([alert()], at=AT)

        assert queue.empty()

    async def test_the_payload_carries_what_a_toast_needs(self):
        broker = AlertBroker()
        queue = broker.subscribe()

        await broker.publish(
            [alert(AlertKind.GRACE_TIMEOUT, message="Nova: grace expired", triggers_lock=True)],
            at=AT,
        )

        payload = await queue.get()

        assert payload["kind"] == "grace_timeout"
        assert payload["triggers_lock"] is True
        assert payload["unit_id"] == "u1"
        assert payload["at"] == AT.isoformat()


class TestBackpressure:
    async def test_a_wedged_subscriber_drops_the_oldest_not_the_newest(self):
        """A laptop waking from sleep wants "unit 5 locked", not the warnings before it.

        Just as importantly, the queue is bounded: an unbounded one behind a wedged tab
        is a memory leak that eventually takes the control server down with it.
        """
        broker = AlertBroker(queue_size=3)
        queue = broker.subscribe()

        for index in range(6):
            await broker.publish([alert(message=f"alert {index}")], at=AT)

        received = [queue.get_nowait()["message"] for _ in range(queue.qsize())]

        assert len(received) == 3
        assert received == ["alert 3", "alert 4", "alert 5"]

    async def test_one_wedged_subscriber_does_not_block_another(self):
        broker = AlertBroker(queue_size=2)
        wedged, healthy = broker.subscribe(), broker.subscribe()

        for index in range(5):
            await broker.publish([alert(message=f"alert {index}")], at=AT)

        # The healthy one is equally bounded, but the point is that publishing never
        # raised and never blocked on the full queue.
        assert wedged.qsize() == 2
        assert healthy.qsize() == 2


class TestReplay:
    async def test_a_dashboard_that_connects_late_sees_recent_alerts(self):
        """Opening the tab to a locked unit and no explanation is the failure here."""
        broker = AlertBroker()

        await broker.publish([alert(message="Nova: 5 min remaining")], at=AT)
        await broker.publish(
            [alert(AlertKind.GRACE_TIMEOUT, message="Nova: grace expired")], at=AT
        )

        replay = broker.recent()

        assert [item["message"] for item in replay] == [
            "Nova: 5 min remaining",
            "Nova: grace expired",
        ]

    async def test_the_replay_buffer_is_bounded(self):
        broker = AlertBroker(replay_size=3)

        for index in range(10):
            await broker.publish([alert(message=f"alert {index}")], at=AT)

        assert len(broker.recent()) == 3


class TestSseFormat:
    def test_an_event_ends_with_a_blank_line(self):
        """Without the terminator the browser buffers forever and no toast appears."""
        frame = sse({"message": "hello"})

        assert frame.endswith("\n\n")
        assert frame.startswith("event: alert\n")

    def test_the_payload_round_trips_as_json(self):
        frame = sse({"message": "Nova: 5 min", "kind": "five_minute_warning"})
        data = frame.split("data: ", 1)[1].strip()

        assert json.loads(data)["kind"] == "five_minute_warning"

    def test_a_comment_frame_is_a_valid_keep_alive(self):
        assert sse_comment().startswith(":")
        assert sse_comment().endswith("\n\n")


class TestEngineIntegration:
    async def test_the_engine_publishes_the_alerts_it_raises(self, seeded, clock):
        """End to end: a warning fires in the engine and lands on the broker."""
        from playslot.engine.session_engine import SessionEngine

        from .conftest import VENUE

        broker = AlertBroker()
        engine = SessionEngine(
            seeded, venue_id=VENUE, clock=clock, alert_sink=broker.sink
        )

        queue = broker.subscribe()

        engine.start_session(unit_id="unit-pc-01", duration_minutes=60)
        clock.advance(minutes=56)
        await engine.tick()

        payload = await asyncio.wait_for(queue.get(), timeout=2)

        assert payload["kind"] == "five_minute_warning"
        assert payload["unit_id"] == "unit-pc-01"

    async def test_a_failing_sink_never_stops_the_floor(self, seeded, clock):
        """The lock has already been committed; a broken toast must not undo it."""
        from playslot.engine.session_engine import SessionEngine

        from .conftest import VENUE

        async def exploding_sink(alerts, at):
            raise RuntimeError("dashboard delivery exploded")

        engine = SessionEngine(
            seeded, venue_id=VENUE, clock=clock, alert_sink=exploding_sink
        )

        engine.start_session(unit_id="unit-pc-01", duration_minutes=60)
        clock.advance(minutes=66)

        result = await engine.tick()

        # The tick completed and the unit still locked, despite the sink throwing.
        assert result.lock_commands == ("unit-pc-01",)
