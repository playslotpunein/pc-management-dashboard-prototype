"""Alert fan-out to whoever is watching.

The alert engine raises its events regardless of who is listening — that separation is
the point, and it is why the grace timeout still locks a unit at 2am with every browser
closed. This module is only the *delivery* channel for the manager-facing half.

Two things follow from that framing:

**Delivery is best-effort.** A dashboard that is not connected misses the toast, and
nothing is retried. The consequences of an alert (the lock, the state change, the
activity-log row) have already happened server-side; the toast is a courtesy on top.
Persisting a queue of undelivered toasts would be storing something nobody will read.

**A slow subscriber must not hold up the floor.** Each subscriber gets a bounded queue,
and when it fills, the *oldest* alert is dropped rather than the newest. A manager whose
laptop just woke from sleep wants "unit 5 locked" now, not the four warnings that led to
it — and an unbounded queue behind a wedged browser tab is a memory leak that takes the
control server down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict
from datetime import datetime
from typing import Any

from playslot.engine.alerts import Alert

log = logging.getLogger(__name__)

#: Per-subscriber backlog. Roughly a minute of a busy floor; past that the oldest go.
QUEUE_SIZE = 32

#: Alerts replayed to a dashboard that has just connected, so a manager opening the tab
#: sees what fired moments ago rather than an empty screen with a locked unit on it.
REPLAY_SIZE = 12


class AlertBroker:
    """Fans alerts out to connected dashboards, with a short replay buffer."""

    def __init__(self, *, queue_size: int = QUEUE_SIZE, replay_size: int = REPLAY_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=replay_size)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    async def publish(self, alerts: list[Alert], *, at: datetime) -> None:
        """Hand alerts to every connected dashboard. Never raises."""
        for alert in alerts:
            payload = {
                **asdict(alert),
                "kind": alert.kind.value,
                "at": at.isoformat(),
            }

            self._recent.append(payload)

            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    # Drop the oldest to make room. A dashboard behind on a busy evening
                    # should catch up on the newest state, not replay the backlog.
                    try:
                        queue.get_nowait()
                        queue.put_nowait(payload)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        log.debug("Dropped an alert for a wedged subscriber")

    async def sink(self, alerts: list[Alert], at: datetime) -> None:
        """Signature the session engine calls on each tick."""
        await self.publish(alerts, at=at)


def sse(payload: dict[str, Any], *, event: str = "alert") -> str:
    """Format one server-sent event.

    The blank line at the end is the record separator; without it the browser buffers
    the message indefinitely and the toast never appears.
    """
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def sse_comment(text: str = "keep-alive") -> str:
    """A comment frame. Keeps idle connections open through proxies that reap them."""
    return f": {text}\n\n"
