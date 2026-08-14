"""The alert engine.

Watches remaining seconds across every unit and raises three distinct events, plus the
no-show. Kept separate from the API and the dashboard on purpose: alerts must fire when
nobody is looking at the screen, and the grace-timeout event is what actually triggers
the lock command. Fold this into the UI and closing the browser tab stops the floor
locking.

Each alert fires exactly once per session. The engine records which kinds have already
been raised, so a tick every second does not produce sixty identical toasts a minute.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from playslot.enums import AlertKind
from playslot.engine.lifecycle import Countdown

#: How often the overdue reminder repeats on a unit nothing can lock. On those the nag is
#: the enforcement — a manager who misses it once has given the table away for free.
OVERDUE_REMINDER_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    unit_id: str
    session_id: str

    #: Ready to show on a card or a toast without further formatting.
    message: str

    #: True for the one alert that has a side effect beyond the UI: grace timeout is
    #: what sends the lock command to the agent.
    triggers_lock: bool = False


@dataclass
class AlertLedger:
    """Remembers which alerts have already fired, per session.

    Held in memory rather than persisted: on restart the engine recomputes state from
    the stored timestamps anyway, and re-raising a five-minute warning after a crash is
    a great deal better than missing a grace timeout.
    """

    _raised: dict[str, set[AlertKind]] = field(default_factory=dict)
    _stages: dict[tuple[str, AlertKind], int] = field(default_factory=dict)

    def already_raised(self, session_id: str, kind: AlertKind) -> bool:
        return kind in self._raised.get(session_id, set())

    def mark(self, session_id: str, kind: AlertKind) -> None:
        self._raised.setdefault(session_id, set()).add(kind)

    def clear(self, session_id: str, kind: AlertKind) -> None:
        """Allow one kind to fire again, without forgetting the rest of the session."""
        self._raised.get(session_id, set()).discard(kind)

    def reached_stage(self, session_id: str, kind: AlertKind, stage: int) -> bool:
        """True the first time this kind reaches ``stage`` on this session.

        For the repeating overdue reminder. Comparing the elapsed time against a window
        instead — "fire if we are within a minute of a five-minute boundary" — would
        re-fire on every tick inside that window, which at a one-second tick is sixty
        toasts a minute. The stage is a counter, so each block can only be entered once.
        """
        key = (session_id, kind)

        if self._stages.get(key) == stage:
            return False

        self._stages[key] = stage

        return True

    def forget(self, session_id: str) -> None:
        """Drop a closed session, and re-arm one that was extended.

        An extension pushes the end time back, so the warning and expiry alerts must be
        allowed to fire again on the new deadline.
        """
        self._raised.pop(session_id, None)

        for key in [key for key in self._stages if key[0] == session_id]:
            del self._stages[key]


def evaluate(
    *,
    unit_id: str,
    session_id: str,
    unit_name: str,
    countdown: Countdown,
    warning_seconds: int,
    ledger: AlertLedger,
    enforced: bool = True,
) -> list[Alert]:
    """Return the alerts that should fire for one unit on this tick.

    Only newly-crossed thresholds are returned; the ledger is updated as a side effect
    so the caller can simply dispatch whatever comes back.
    """
    alerts: list[Alert] = []

    def raise_once(kind: AlertKind, message: str, *, triggers_lock: bool = False) -> None:
        if ledger.already_raised(session_id, kind):
            return

        ledger.mark(session_id, kind)
        alerts.append(
            Alert(
                kind=kind,
                unit_id=unit_id,
                session_id=session_id,
                message=message,
                triggers_lock=triggers_lock,
            )
        )

    if countdown.grace_consumed:
        # Every earlier threshold is implied. Marking them prevents a burst of stale
        # alerts if the control server was down across the whole expiry window.
        ledger.mark(session_id, AlertKind.FIVE_MINUTE_WARNING)
        ledger.mark(session_id, AlertKind.EXPIRED)

        if enforced:
            raise_once(
                AlertKind.GRACE_TIMEOUT,
                f"{unit_name}: grace expired — locking",
                triggers_lock=True,
            )

            return alerts

        # Nothing here can be locked, so the reminder repeats until the manager deals
        # with it. Firing once and going quiet would let a table run an hour over
        # unnoticed on a busy evening, which is the whole failure this guards against.
        over_by = abs(countdown.remaining_seconds)

        if ledger.reached_stage(
            session_id, AlertKind.OVERDUE, over_by // OVERDUE_REMINDER_SECONDS
        ):
            ledger.clear(session_id, AlertKind.OVERDUE)

        raise_once(
            AlertKind.OVERDUE,
            f"{unit_name}: {over_by // 60} min over — still running, no lock possible",
        )

        return alerts

    if countdown.expired:
        ledger.mark(session_id, AlertKind.FIVE_MINUTE_WARNING)

        grace_left = max(0, countdown.grace_remaining_seconds // 60)

        raise_once(
            AlertKind.EXPIRED,
            f"{unit_name}: time up — {grace_left} min grace remaining",
        )

        return alerts

    if countdown.remaining_seconds <= warning_seconds:
        minutes_left = max(1, countdown.remaining_seconds // 60)

        raise_once(
            AlertKind.FIVE_MINUTE_WARNING,
            f"{unit_name}: {minutes_left} min remaining",
        )

    return alerts


def no_show(*, unit_id: str, session_id: str, unit_name: str) -> Alert:
    """Raised when a scheduled booking is never claimed.

    Releases the hold back to AVAILABLE and flags a refund decision for whoever settles
    with the cloud. Not part of :func:`evaluate` because it is driven by the scheduled
    start time rather than by a running countdown.
    """
    return Alert(
        kind=AlertKind.NO_SHOW,
        unit_id=unit_id,
        session_id=session_id,
        message=f"{unit_name}: no-show — released, refund decision needed",
    )


def pending_lock_commands(alerts: Iterable[Alert]) -> list[str]:
    """Unit ids that must be sent a lock command as a result of these alerts."""
    return [alert.unit_id for alert in alerts if alert.triggers_lock]
