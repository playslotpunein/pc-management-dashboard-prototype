"""Time handling.

Two rules hold everywhere in the engine.

**Everything is timezone-aware UTC.** A naive datetime that means IST in one place and
UTC in another produces a bill that is five and a half hours wrong, and the error is
invisible until someone disputes it at the counter.

**State is derived from stored timestamps, never from counting ticks.** A session's
remaining time is ``end_time - now``, computed fresh each time. That is what the
architecture calls epoch-timestamp timers, and it is what lets the control server be
restarted mid-session — after a crash, a deploy, or a power cut — and pick up every
running session exactly where it was. A tick counter held in memory loses the floor.

The Clock indirection exists so tests can advance time deliberately instead of
sleeping. A test suite that sleeps for a five-minute warning is a test suite nobody
runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    """Real wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock(Clock):
    """A clock that only moves when told to. For tests.

    Do not use this in production code paths — it exists so that a test can express
    "five minutes and one second later" without taking five minutes and one second.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> datetime:
        self._now += timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = _require_aware(moment)


def ensure_utc(moment: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC, rejecting naive input.

    Naive datetimes are rejected rather than assumed to be UTC. Assuming is how a
    timezone bug survives to production: it works on the developer's machine, which is
    already UTC, and fails on the café PC in IST.
    """
    return _require_aware(moment).astimezone(UTC)


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"Naive datetime {moment!r}. Every timestamp in the engine must be "
            "timezone-aware; use clock.now() or datetime(..., tzinfo=UTC)."
        )

    return moment
