"""The unit state machine.

The architecture's rule is "one state per unit at all times, no skipping". That is only
true if something enforces it, so every transition goes through :func:`transition` and
an illegal one raises rather than being written to the database.

The reason this is worth a module of its own: an unenforced state machine degrades
quietly. A unit that skips OVERTIME and jumps from ACTIVE to LOCKED cuts a paying
customer off mid-match, and the only trace is a support call the next day. Failing loudly
at the transition puts the bug in the log instead of at the counter.
"""

from __future__ import annotations

from dataclasses import dataclass

from playslot.enums import UnitState

#: Legal transitions, as ``from -> {to}``.
#:
#: Read this table as the operational contract:
#:   * Every occupied state can reach LOCKED only through OVERTIME — grace is not
#:     optional and cannot be skipped.
#:   * LOCKED returns to ACTIVE on an extension. A lock is recoverable, never terminal.
#:   * MAINTENANCE is reachable only from an idle unit, so toggling it can never strand
#:     a customer mid-session.
_LEGAL: dict[UnitState, frozenset[UnitState]] = {
    UnitState.AVAILABLE: frozenset(
        {UnitState.SCHEDULED, UnitState.ACTIVE, UnitState.MAINTENANCE}
    ),
    # A no-show releases the hold back to AVAILABLE.
    UnitState.SCHEDULED: frozenset({UnitState.ACTIVE, UnitState.AVAILABLE}),
    UnitState.ACTIVE: frozenset(
        {UnitState.WARNING, UnitState.OVERTIME, UnitState.AVAILABLE}
    ),
    # WARNING can fall straight back to ACTIVE when an extension pushes the end time
    # beyond the warning threshold again.
    UnitState.WARNING: frozenset(
        {UnitState.ACTIVE, UnitState.OVERTIME, UnitState.AVAILABLE}
    ),
    UnitState.OVERTIME: frozenset(
        {UnitState.ACTIVE, UnitState.WARNING, UnitState.LOCKED, UnitState.AVAILABLE}
    ),
    # An extension unlocks the machine again; ending the session frees the unit.
    UnitState.LOCKED: frozenset(
        {UnitState.ACTIVE, UnitState.WARNING, UnitState.AVAILABLE}
    ),
    UnitState.MAINTENANCE: frozenset({UnitState.AVAILABLE}),
}


class IllegalTransition(ValueError):
    """Raised when a transition would break the state machine."""

    def __init__(self, current: UnitState, requested: UnitState, reason: str) -> None:
        self.current = current
        self.requested = requested

        super().__init__(
            f"Cannot move unit from {current.value} to {requested.value}: {reason}. "
            f"Legal from {current.value}: "
            f"{', '.join(sorted(s.value for s in _LEGAL[current])) or 'none'}."
        )


def legal_targets(current: UnitState) -> frozenset[UnitState]:
    """The states reachable from ``current``."""
    return _LEGAL[current]


def can_transition(current: UnitState, requested: UnitState) -> bool:
    return requested in _LEGAL[current]


def transition(current: UnitState, requested: UnitState, *, reason: str) -> UnitState:
    """Validate a transition and return the new state.

    ``reason`` is required rather than optional because it ends up in the activity log,
    and a state change with no recorded cause is the kind of thing that makes a busy
    evening impossible to reconstruct afterwards.
    """
    if current is requested:
        return current

    if not can_transition(current, requested):
        raise IllegalTransition(current, requested, reason)

    return requested


def path_to(current: UnitState, target: UnitState) -> tuple[UnitState, ...]:
    """The shortest legal sequence of states from ``current`` to ``target``.

    Exists because the engine can miss ticks. If the control server is restarted, or is
    down for twenty minutes, a unit can still be ACTIVE while the clock says its grace
    expired long ago — and ACTIVE to LOCKED is deliberately illegal, so a direct jump
    would leave that unit stuck and unlockable.

    Walking the path is the honest fix rather than relaxing the rule. The customer did
    get their grace period; it elapsed in real time while nothing was watching. Replaying
    the intermediate states preserves the invariant *and* writes the full sequence to the
    activity log, which is what makes an outage reconstructable afterwards.

    Returns the states to move through, excluding ``current`` and including ``target``.
    """
    if current is target:
        return ()

    # Breadth-first, so the result is always the shortest legal route rather than
    # whichever order the transition table happens to be written in.
    queue: list[tuple[UnitState, tuple[UnitState, ...]]] = [(current, ())]
    seen = {current}

    while queue:
        state, route = queue.pop(0)

        for nxt in _LEGAL[state]:
            if nxt in seen:
                continue

            extended = (*route, nxt)

            if nxt is target:
                return extended

            seen.add(nxt)
            queue.append((nxt, extended))

    raise IllegalTransition(current, target, "no legal route exists")


@dataclass(frozen=True, slots=True)
class Countdown:
    """A session's position in time, derived fresh from stored timestamps."""

    #: Seconds until the paid time runs out. Negative once it has.
    remaining_seconds: int

    #: Seconds of grace left after expiry. Zero once grace is consumed.
    grace_remaining_seconds: int

    #: False for an open-ended walk-in, which is billed for time used and never expires.
    #: Such a session carries a sentinel remaining time so the comparisons above stay
    #: total, and callers must read this flag rather than that number — an API that
    #: forwarded the sentinel would show the manager a countdown eleven thousand days
    #: long.
    has_deadline: bool = True

    @property
    def expired(self) -> bool:
        return self.remaining_seconds <= 0

    @property
    def grace_consumed(self) -> bool:
        return self.expired and self.grace_remaining_seconds <= 0


def derive_state(
    countdown: Countdown,
    *,
    warning_seconds: int,
    current: UnitState,
) -> UnitState:
    """Work out which state an occupied unit should be in, purely from the clock.

    This is the half of the state machine that runs on a timer. It is a pure function of
    the countdown so it can be tested exhaustively, and so a restarted control server
    recomputes exactly the same answer from the same stored timestamps.

    Units that are not running a session (AVAILABLE, SCHEDULED, MAINTENANCE) are returned
    untouched — the clock has no opinion about them.
    """
    if current in (UnitState.AVAILABLE, UnitState.SCHEDULED, UnitState.MAINTENANCE):
        return current

    if countdown.grace_consumed:
        return UnitState.LOCKED

    if countdown.expired:
        return UnitState.OVERTIME

    if countdown.remaining_seconds <= warning_seconds:
        return UnitState.WARNING

    return UnitState.ACTIVE
