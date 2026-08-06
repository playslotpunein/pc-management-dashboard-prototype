"""Pydantic models — the wire contract.

These are the types the architecture calls for sharing with the cloud backend, so that
"a session" means the same thing on both sides of the sync. Keep them free of
SQLAlchemy imports: the cloud has the same schemas without the same storage.

Money crosses the wire as integer paise, in fields named ``*_paise``. A JSON number that
looks like ``340.00`` invites a client to parse it as a float and re-introduce exactly
the drift the server avoided. The rupee value is included alongside as a preformatted
string for display, never for arithmetic.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from playslot.enums import (
    AlertKind,
    PaymentMethod,
    SessionSource,
    SessionStatus,
    UnitState,
    UnitType,
)
from playslot.money import format_rupees


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=False)


# ------------------------------------------------------------------------ units


class UnitCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64)
    type: UnitType
    zone: str = ""
    relay_address: str | None = None
    notes: str = ""


class UnitRead(ApiModel):
    id: str
    name: str
    type: UnitType
    zone: str
    state: UnitState
    current_session_id: str | None
    relay_address: str | None
    notes: str


class UnitLive(UnitRead):
    """A unit plus everything the dashboard card needs, computed server-side.

    The dashboard is a view: it renders these numbers, it does not derive them. Putting
    the countdown in browser state instead means closing the tab loses the floor.
    """

    remaining_seconds: int | None = None
    grace_remaining_seconds: int | None = None
    running_total_paise: int | None = None
    running_total: str | None = None
    customer_ref: str | None = None
    session_started_at: datetime | None = None


# --------------------------------------------------------------------- sessions


class SessionStart(ApiModel):
    unit_id: str
    source: SessionSource = SessionSource.WALK_IN
    customer_ref: str = ""

    #: Zero means open-ended: billed for time used, never expires, never locks.
    duration_minutes: int = Field(default=0, ge=0, le=24 * 60)

    extra_controllers: int = Field(default=0, ge=0, le=4)
    actor: str = "counter"


class SessionExtend(ApiModel):
    minutes: int = Field(gt=0, le=12 * 60)
    actor: str = "counter"


class SessionEnd(ApiModel):
    payment_method: PaymentMethod = PaymentMethod.CASH
    actor: str = "counter"


class SessionRead(ApiModel):
    id: str
    unit_id: str
    source: SessionSource
    customer_ref: str
    status: SessionStatus
    start_time: datetime | None
    end_time: datetime | None
    duration_minutes: int
    rate_snapshot_paise: int
    extensions: list[dict]


# ------------------------------------------------------------------------ bills


class BillLineRead(ApiModel):
    kind: str
    description: str
    minutes: int | None
    amount_paise: int

    @property
    def amount(self) -> str:
        return format_rupees(self.amount_paise)


class BillRead(ApiModel):
    lines: list[BillLineRead]
    total_paise: int
    total: str
    actual_minutes: int
    booked_minutes: int
    overtime_minutes: int

    @classmethod
    def of(cls, bill) -> BillRead:
        return cls(
            lines=[
                BillLineRead(
                    kind=line.kind,
                    description=line.description,
                    minutes=line.minutes,
                    amount_paise=line.amount_paise,
                )
                for line in bill.lines
            ],
            total_paise=bill.total_paise,
            total=format_rupees(bill.total_paise),
            actual_minutes=bill.actual_minutes,
            booked_minutes=bill.booked_minutes,
            overtime_minutes=bill.overtime_minutes,
        )


class SaleRead(ApiModel):
    id: str
    session_id: str
    source: SessionSource
    amount_paise: int
    payment_method: PaymentMethod
    settled_at: datetime
    lines: list[dict]


# ------------------------------------------------------------------------ sales


class TypeRollupRead(ApiModel):
    unit_type: UnitType
    closed_paise: int
    live_paise: int
    total_paise: int
    closed_sessions: int
    live_sessions: int


class RollupRead(ApiModel):
    since: datetime
    until: datetime
    closed_paise: int
    live_paise: int
    total_paise: int
    total: str
    by_type: list[TypeRollupRead]
    by_payment_method: dict[str, int]


# ----------------------------------------------------------------------- alerts


class AlertRead(ApiModel):
    kind: AlertKind
    unit_id: str
    session_id: str
    message: str
    triggers_lock: bool


# --------------------------------------------------------------------- pricing


class PricingCreate(ApiModel):
    """A price change inserts a new row; it never updates an existing one.

    ``effective_from`` may be in the future, which is how a venue schedules a change
    without it taking hold early.
    """

    unit_type: UnitType
    hourly_rate_paise: int = Field(ge=0)
    overtime_rate_paise_per_minute: int = Field(default=0, ge=0)
    controller_surcharge_paise_per_hour: int = Field(default=0, ge=0)
    effective_from: datetime | None = None


class PricingRead(ApiModel):
    id: str
    unit_type: UnitType
    hourly_rate_paise: int
    overtime_rate_paise_per_minute: int
    controller_surcharge_paise_per_hour: int
    effective_from: datetime
