"""State machine tests.

The architecture's rule is "one state per unit at all times, no skipping". These prove
the skipping is actually impossible rather than merely discouraged.
"""

from __future__ import annotations

import pytest

from playslot.engine.lifecycle import (
    Countdown,
    IllegalTransition,
    can_transition,
    derive_state,
    legal_targets,
    transition,
)
from playslot.enums import OCCUPIED_STATES, UNLOCKED_STATES, UnitState

WARNING = 300


class TestTransitionTable:
    def test_grace_cannot_be_skipped(self):
        """The rule that protects the customer.

        ACTIVE and WARNING cannot reach LOCKED directly. The only way in is through
        OVERTIME, which is the five-minute grace. Without this a unit could cut someone
        off mid-match the instant their timer hit zero.
        """
        assert not can_transition(UnitState.ACTIVE, UnitState.LOCKED)
        assert not can_transition(UnitState.WARNING, UnitState.LOCKED)
        assert can_transition(UnitState.OVERTIME, UnitState.LOCKED)

    def test_a_lock_is_always_recoverable(self):
        # The manager extends and the unit returns straight to ACTIVE.
        assert can_transition(UnitState.LOCKED, UnitState.ACTIVE)

    def test_maintenance_cannot_strand_a_customer(self):
        # Reachable only from an idle unit, so toggling it mid-session is impossible.
        for occupied in OCCUPIED_STATES:
            assert not can_transition(occupied, UnitState.MAINTENANCE)

        assert can_transition(UnitState.AVAILABLE, UnitState.MAINTENANCE)

    def test_a_maintenance_unit_cannot_be_sold(self):
        assert legal_targets(UnitState.MAINTENANCE) == frozenset({UnitState.AVAILABLE})

    def test_illegal_transition_names_what_was_allowed(self):
        with pytest.raises(IllegalTransition) as caught:
            transition(UnitState.ACTIVE, UnitState.LOCKED, reason="timer")

        message = str(caught.value)

        assert "active" in message
        assert "locked" in message
        assert "overtime" in message  # tells you the legal route

    def test_transitioning_to_the_same_state_is_a_no_op(self):
        assert transition(UnitState.ACTIVE, UnitState.ACTIVE, reason="tick") is UnitState.ACTIVE

    def test_every_state_has_an_entry(self):
        # A missing entry would raise KeyError at the worst possible moment.
        for state in UnitState:
            assert isinstance(legal_targets(state), frozenset)


class TestDerivedState:
    @staticmethod
    def countdown(remaining: int, grace: int = 300) -> Countdown:
        return Countdown(remaining_seconds=remaining, grace_remaining_seconds=grace)

    def test_plenty_of_time_is_active(self):
        assert (
            derive_state(
                self.countdown(3600), warning_seconds=WARNING, current=UnitState.ACTIVE
            )
            is UnitState.ACTIVE
        )

    def test_warning_fires_at_exactly_300_seconds(self):
        """The doc says exactly 300, not approximately five minutes."""
        assert (
            derive_state(
                self.countdown(301), warning_seconds=WARNING, current=UnitState.ACTIVE
            )
            is UnitState.ACTIVE
        )
        assert (
            derive_state(
                self.countdown(300), warning_seconds=WARNING, current=UnitState.ACTIVE
            )
            is UnitState.WARNING
        )

    def test_zero_remaining_is_overtime_not_locked(self):
        state = derive_state(
            self.countdown(0, grace=300), warning_seconds=WARNING, current=UnitState.WARNING
        )

        assert state is UnitState.OVERTIME

    def test_lock_only_once_grace_is_consumed(self):
        assert (
            derive_state(
                self.countdown(-60, grace=1),
                warning_seconds=WARNING,
                current=UnitState.OVERTIME,
            )
            is UnitState.OVERTIME
        )
        assert (
            derive_state(
                self.countdown(-300, grace=0),
                warning_seconds=WARNING,
                current=UnitState.OVERTIME,
            )
            is UnitState.LOCKED
        )

    def test_the_clock_has_no_opinion_about_idle_units(self):
        for idle in (UnitState.AVAILABLE, UnitState.SCHEDULED, UnitState.MAINTENANCE):
            assert (
                derive_state(self.countdown(-9999, 0), warning_seconds=WARNING, current=idle)
                is idle
            )


class TestUnlockedStates:
    def test_overtime_is_an_unlocked_state(self):
        """Deliberate, and worth a test so nobody 'fixes' it later.

        A customer in overtime is still playing. The grace period only means anything
        if the machine still works during it.
        """
        assert UnitState.OVERTIME in UNLOCKED_STATES
        assert UnitState.LOCKED not in UNLOCKED_STATES
