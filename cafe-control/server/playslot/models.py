"""The eight tables. Everything else is derived.

These SQLAlchemy models are the single definition the architecture calls for: the same
classes drive local SQLite at the venue and Supabase Postgres in the cloud, and both
migrations are generated from here. Defining the schema twice is how the two drift
within a month.

Three decisions worth knowing before reading the tables:

**Primary keys are UUID strings, not autoincrement integers.** A venue that generates
``session 41`` locally and syncs it up would collide with every other venue's session 41.
UUIDs let a row be created offline, on any venue, and still be globally unique when the
outbox eventually flushes — which is what makes local-first work at all.

**Every money column is BigInteger paise.** Never a float, never a NUMERIC that a driver
might hand back as a float. See :mod:`playslot.money`.

**Every timestamp column is :class:`UtcDateTime`.** SQLite has no native timezone-aware
type and silently returns naive datetimes, which would then be rejected by the engine's
clock handling. The type decorator below guarantees the round-trip.

Each table also carries ``venue_id``. It is unused in standalone mode, but it is the
column Supabase row-level security scopes on, so a manager can only ever read their own
venue. Adding it later would mean a migration across every table at exactly the point
the system has real data in it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from playslot.enums import (
    DEFAULT_ENFORCEMENT,
    EnforcementMode,
    PaymentMethod,
    PaymentStatus,
    SessionSource,
    SessionStatus,
    UnitState,
    UnitType,
)


class UtcDateTime(TypeDecorator):
    """A datetime column that is always timezone-aware UTC in Python.

    Postgres stores the offset; SQLite does not, and hands back a naive datetime that
    would then be rejected by :func:`playslot.clock.ensure_utc`. This normalises on the
    way in and re-attaches UTC on the way out, so both databases behave identically.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None:
            raise ValueError(
                f"Refusing to store naive datetime {value!r}. Use clock.now()."
            )

        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class EnumValue(TypeDecorator):
    """Stores a StrEnum by its *value* and reads it back as the enum member.

    Declaring the column as a plain String would store the value correctly but hand
    back a bare ``str`` on read, so ``unit.state is UnitState.ACTIVE`` would silently be
    False everywhere and the state machine would compare strings to enums.

    SQLAlchemy's own Enum type stores member *names* ("ACTIVE") rather than values
    ("active") unless coaxed, and emits a native enum on Postgres that then needs a
    migration to add a value to. A VARCHAR holding the lowercase value is readable in a
    plain SELECT and identical on both databases.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type, length: int = 32) -> None:
        self._enum = enum_class
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None

        # Coerces a bare string too, so seed data and API payloads are validated here
        # rather than failing later as an unknown state.
        return self._enum(value).value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None

        return self._enum(value)


def new_id() -> str:
    return str(uuid.uuid4())


def _enforcement_for(context: Any) -> EnforcementMode:
    """Default a unit's enforcement from its type, at insert time.

    Derived here rather than left to the API so that *every* path which creates a unit —
    a route, a seed script, a test — gets it right. A flat default of SOFTWARE would
    quietly arm lock commands against a pool table, and the mistake would only surface as
    "no agent connected" logged once a second forever.
    """
    unit_type = context.get_current_parameters().get("type")

    try:
        return DEFAULT_ENFORCEMENT[UnitType(unit_type)]
    except (ValueError, KeyError):
        # An unknown type cannot be enforced by something we do not understand.
        return EnforcementMode.MANUAL


class Base(DeclarativeBase):
    pass


class Unit(Base):
    """A bookable machine: a PC, a PS5 station or a sim rig."""

    __tablename__ = "units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[UnitType] = mapped_column(EnumValue(UnitType))
    zone: Mapped[str] = mapped_column(String(64), default="")

    #: The state machine's single source of truth for this unit.
    state: Mapped[UnitState] = mapped_column(
        EnumValue(UnitState), default=UnitState.AVAILABLE
    )

    #: Denormalised pointer to the running session, so the dashboard's unit grid does
    #: not need a correlated subquery per card on every poll.
    current_session_id: Mapped[str | None] = mapped_column(String(36), default=None)

    #: How this unit's time limit is actually enforced. Defaults from the type — a PC is
    #: locked by its agent; a console, a pool table, or a PC without its agent yet is
    #: handled by a person — but stored per unit so a venue can override it.
    enforcement: Mapped[EnforcementMode] = mapped_column(
        EnumValue(EnforcementMode), default=lambda context: _enforcement_for(context)
    )

    notes: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC)
    )

    sessions: Mapped[list[Session]] = relationship(back_populates="unit")

    __table_args__ = (Index("ix_units_venue_state", "venue_id", "state"),)

    @property
    def is_enforced(self) -> bool:
        """Whether anything can actually hold this unit shut."""
        return self.enforcement is not EnforcementMode.MANUAL

    @staticmethod
    def default_enforcement(unit_type: UnitType) -> EnforcementMode:
        return DEFAULT_ENFORCEMENT.get(unit_type, EnforcementMode.MANUAL)


class Session(Base):
    """One customer's time on one unit."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    unit_id: Mapped[str] = mapped_column(ForeignKey("units.id"), index=True)

    #: The standalone/cloud switch. A venue running without PlaySlot produces only
    #: WALK_IN rows; enabling bookings later adds APP rows beside them, and nothing
    #: downstream changes.
    source: Mapped[SessionSource] = mapped_column(
        EnumValue(SessionSource), default=SessionSource.WALK_IN
    )

    #: Phone number, booking reference, or a name written at the counter. Deliberately
    #: free-form: a walk-in has no account.
    customer_ref: Mapped[str] = mapped_column(String(128), default="")

    status: Mapped[SessionStatus] = mapped_column(
        EnumValue(SessionStatus), default=SessionStatus.ACTIVE
    )

    #: When the customer actually sat down. Null while SCHEDULED.
    start_time: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    end_time: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    #: For an APP booking, the slot the player reserved. Drives the no-show timeout.
    scheduled_start: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    #: Booked minutes. Zero means an open-ended walk-in, billed for time used.
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)

    #: Extensions live here as JSON rather than in an eighth table, matching the doc's
    #: seven-table model. They are always read and written as a whole list with the
    #: session, never queried across sessions, so a child table would buy nothing.
    #: Shape: [{"minutes": int, "granted_at": iso8601, "rate_snapshot_paise": int}]
    extensions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    #: Snacks and drinks rung up against this tab, same JSON-on-the-session reasoning as
    #: extensions. Each line snapshots the item's price at the moment it was sold, so a
    #: price change never rewrites an open tab, and carries the name so a bill is still
    #: readable after the item is renamed or archived.
    #: Shape: [{"line_id": str, "item_id": str, "name": str, "qty": int,
    #:          "unit_price_paise": int, "added_at": iso8601}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    #: THE auditability rule. Captured when the session starts and never updated. A
    #: pricing change at 6pm must not alter a bill for someone who started at 5pm.
    rate_snapshot_paise: Mapped[int] = mapped_column(BigInteger)

    #: Snapshotted alongside the rate for the same reason.
    overtime_rate_paise_per_minute: Mapped[int] = mapped_column(BigInteger, default=0)
    controller_surcharge_paise_per_hour: Mapped[int] = mapped_column(BigInteger, default=0)
    extra_controllers: Mapped[int] = mapped_column(Integer, default=0)

    #: Free minutes after expiry before the lock engages. Snapshotted so that changing
    #: venue policy mid-session cannot shorten someone's grace.
    grace_minutes: Mapped[int] = mapped_column(Integer, default=5)

    #: Whether this unit's grace ending actually shuts it down, taken from the unit's
    #: enforcement when the session started. Snapshotted for the same reason as the rate:
    #: a manager switching a unit to MANUAL at 9pm must not retroactively start charging
    #: overtime for the hour it spent locked beforehand.
    locks_at_grace_end: Mapped[bool] = mapped_column(Boolean, default=False)

    unit: Mapped[Unit] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_sessions_venue_status", "venue_id", "status"),
        Index("ix_sessions_unit_status", "unit_id", "status"),
    )


class Sale(Base):
    """The money side of a closed session."""

    __tablename__ = "sales"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    source: Mapped[SessionSource] = mapped_column(EnumValue(SessionSource))

    amount_paise: Mapped[int] = mapped_column(BigInteger)

    #: cash / upi / card at the counter, or paid_online for an app booking. In
    #: standalone mode only the first three ever occur — there is no gateway involved.
    payment_method: Mapped[PaymentMethod] = mapped_column(EnumValue(PaymentMethod))
    payment_status: Mapped[PaymentStatus] = mapped_column(
        EnumValue(PaymentStatus), default=PaymentStatus.PAID
    )

    #: The itemised breakdown as billed, stored rather than recomputed. Pricing may have
    #: changed by the time anyone asks why the total was what it was.
    #: Shape: [{"kind": str, "description": str, "minutes": int|None, "amount_paise": int}]
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    settled_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (Index("ix_sales_venue_settled", "venue_id", "settled_at"),)


class Pricing(Base):
    """Rates by unit type, versioned by ``effective_from``. Never mutate history.

    A price change inserts a new row. Updating an existing one would silently rewrite
    what past sessions were charged, and the sales figures would stop reconciling
    against the sessions that produced them.
    """

    __tablename__ = "pricing"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    unit_type: Mapped[UnitType] = mapped_column(EnumValue(UnitType), index=True)

    hourly_rate_paise: Mapped[int] = mapped_column(BigInteger)
    overtime_rate_paise_per_minute: Mapped[int] = mapped_column(BigInteger, default=0)
    controller_surcharge_paise_per_hour: Mapped[int] = mapped_column(BigInteger, default=0)

    effective_from: Mapped[datetime] = mapped_column(UtcDateTime, index=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_pricing_lookup", "venue_id", "unit_type", "effective_from"),
    )


class Agent(Base):
    """A client agent enrolled against a unit. The token drives the HMAC check."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    unit_id: Mapped[str] = mapped_column(ForeignKey("units.id"), unique=True, index=True)

    #: Per-device secret issued at enrolment. Every inbound message is verified against
    #: it — without that, anyone on the café wifi can send an unlock for unit 5.
    device_token: Mapped[str] = mapped_column(String(128))

    agent_version: Mapped[str] = mapped_column(String(32), default="")
    last_heartbeat: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    #: Cleared on a successful verify. A climbing count is an attack in progress.
    failed_verifications: Mapped[int] = mapped_column(Integer, default=0)

    enrolled_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC)
    )


class SyncOutbox(Base):
    """Events waiting to go up to the cloud.

    ``event_id`` is the idempotency key. Without it, a flaky connection that retries
    mid-flush writes the same sale twice and inflates revenue — which is why the
    architecture calls this not optional.

    In standalone mode nothing drains this table, and that is fine: it costs a few
    kilobytes a day and means enabling cloud sync later is a configuration change rather
    than a backfill.
    """

    __tablename__ = "sync_outbox"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC), index=True
    )
    synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None, index=True)


class ActivityLog(Base):
    """Append-only audit trail. Never updated, never deleted.

    This is what makes a disputed evening reconstructable: who started what, which unit
    locked when, what was charged and by whom.
    """

    __tablename__ = "activity_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    timestamp: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC), index=True
    )

    event: Mapped[str] = mapped_column(String(64), index=True)
    unit_id: Mapped[str | None] = mapped_column(String(36), default=None, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), default=None)

    amount_paise: Mapped[int | None] = mapped_column(BigInteger, default=None)

    #: Who caused it: a manager's name, "system" for a timer-driven transition, or the
    #: agent's unit id. A transition with no actor is unattributable after the fact.
    actor: Mapped[str] = mapped_column(String(64), default="system")

    detail: Mapped[str] = mapped_column(Text, default="")

    #: Guards against the audit trail being edited in place. Set at the database level
    #: too when this reaches Postgres; here it documents the intent and lets a test
    #: assert on it.
    is_append_only: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_activity_venue_time", "venue_id", "timestamp"),)


class InventoryItem(Base):
    """A sellable stock item — a can of Coke, a packet of chips.

    Separate from Pricing, which prices *time* by unit type and keeps a history so old
    bills stay explainable. Inventory prices in place: a bill snapshots the price onto the
    session line at the moment of sale (see Session.items), so this row can change freely
    without a second historical table.
    """

    __tablename__ = "inventory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    venue_id: Mapped[str] = mapped_column(String(36), index=True)

    name: Mapped[str] = mapped_column(String(80))
    #: Free-text grouping for the dashboard — "Drinks", "Snacks". Never billed on.
    category: Mapped[str] = mapped_column(String(40), default="")

    unit_price_paise: Mapped[int] = mapped_column(BigInteger)

    #: On hand right now. Decremented when sold, incremented on restock. Never negative:
    #: the engine refuses a sale it cannot cover rather than letting this go below zero.
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)

    #: At or below this, the item is "low" and a sale that reaches it raises an alert.
    #: Zero means only running out (reaching 0) is worth flagging.
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0)

    #: Hidden from the counter without deleting it, so the sales it already appears on
    #: keep their line. A deleted row would orphan those.
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_inventory_venue", "venue_id", "archived"),)

    @property
    def is_low(self) -> bool:
        return self.stock_qty <= self.low_stock_threshold
