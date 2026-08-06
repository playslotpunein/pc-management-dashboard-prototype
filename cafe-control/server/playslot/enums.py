"""Domain vocabulary.

These are str enums so they persist as readable values in SQLite and Postgres. A status
column you can read with a plain SELECT is worth more during a 9pm incident at the
counter than the two bytes an integer code would save.
"""

from __future__ import annotations

from enum import StrEnum


class UnitType(StrEnum):
    PC = "pc"
    PS5 = "ps5"
    SIM = "sim"


class UnitState(StrEnum):
    """The per-unit state machine. Exactly one of these holds at any moment.

    Transitions are enforced in :mod:`playslot.engine.lifecycle`; this enum only names
    the states. The comments are the operational meaning, which matters because two of
    them are counter-intuitive on purpose.
    """

    #: Idle and bookable. Pushed up to the cloud as open inventory.
    AVAILABLE = "available"

    #: A booking has synced down but the player has not arrived. Held, not bookable.
    #: Needs a no-show timeout that releases it and flags a refund decision.
    SCHEDULED = "scheduled"

    #: Running. Unit unlocked, timer counting down.
    ACTIVE = "active"

    #: 300 seconds remaining. Informational only — the unit stays UNLOCKED so the
    #: manager can walk over and offer an extension.
    WARNING = "warning"

    #: Timer hit zero, grace counting down. The unit is STILL UNLOCKED, deliberately.
    #: Cutting someone off mid-match with no warning is how you lose a regular.
    OVERTIME = "overtime"

    #: Grace expired with no action. Input blocking engages. Fully recoverable: an
    #: extension returns the unit straight to ACTIVE.
    LOCKED = "locked"

    #: Manually toggled. Excluded from the availability push so the cloud cannot sell
    #: a broken machine.
    MAINTENANCE = "maintenance"


#: States in which a customer is actively using the unit and it must not be locked.
#: OVERTIME is deliberately included — see the note on the enum member.
UNLOCKED_STATES = frozenset(
    {UnitState.ACTIVE, UnitState.WARNING, UnitState.OVERTIME}
)

#: States that mean a session is in progress and the unit cannot be sold.
OCCUPIED_STATES = frozenset(UNLOCKED_STATES | {UnitState.LOCKED})


class SessionStatus(StrEnum):
    #: Booked, waiting for the player to arrive.
    SCHEDULED = "scheduled"

    #: Running, or over time. The unit state carries the finer detail.
    ACTIVE = "active"

    #: Ended and billed. Terminal.
    CLOSED = "closed"

    #: The player never arrived and the no-show timeout fired. Terminal, and flags a
    #: refund decision for whoever settles with the cloud.
    NO_SHOW = "no_show"

    #: Cancelled before it started. Terminal.
    CANCELLED = "cancelled"


TERMINAL_SESSION_STATUSES = frozenset(
    {SessionStatus.CLOSED, SessionStatus.NO_SHOW, SessionStatus.CANCELLED}
)


class SessionSource(StrEnum):
    """Where the session came from.

    This single field is the standalone/cloud switch the architecture is built around.
    A venue that bought only the software produces nothing but WALK_IN rows; turning
    PlaySlot bookings on later adds APP rows alongside them. Nothing downstream changes,
    which is what makes standalone mode a configuration rather than a migration.
    """

    WALK_IN = "walk_in"
    APP = "app"


class PaymentMethod(StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"

    #: Settled in the app before arrival. Only ever appears on APP sessions.
    PAID_ONLINE = "paid_online"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"


class AlertKind(StrEnum):
    """The three events the alert engine raises."""

    #: 300 seconds remaining.
    FIVE_MINUTE_WARNING = "five_minute_warning"

    #: Timer reached zero; grace has begun.
    EXPIRED = "expired"

    #: Grace consumed. This is the event that actually triggers the lock command.
    GRACE_TIMEOUT = "grace_timeout"

    #: A scheduled booking was never claimed.
    NO_SHOW = "no_show"
