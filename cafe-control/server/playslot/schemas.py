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
    EnforcementMode,
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

    #: Left null, this follows the type. Set it to override — a PC awaiting agent
    #: installation runs as MANUAL and still gets timing, billing and alerts.
    enforcement: EnforcementMode | None = None

    notes: str = ""


class UnitRead(ApiModel):
    id: str
    name: str
    type: UnitType
    zone: str
    state: UnitState
    enforcement: EnforcementMode
    current_session_id: str | None
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

    #: Elapsed but deliberately not charged, because the unit locked and the customer
    #: could not use it. The counter needs this on screen: without it the bill looks
    #: short against the clock, and the manager has no answer for why.
    unbilled_minutes: int = 0

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
            unbilled_minutes=bill.unbilled_minutes,
        )


class SaleRead(ApiModel):
    id: str
    session_id: str
    source: SessionSource
    amount_paise: int
    payment_method: PaymentMethod
    settled_at: datetime
    lines: list[dict]

    #: Joined in for the shift report. A row reading "session 3f9a-…, ₹120" is no use to
    #: a manager reconciling cash at closing time; they need the unit and the customer.
    unit_name: str = ""
    customer_ref: str = ""
    amount: str = ""


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

    #: Preformatted for display, so the shift report can be read without the client
    #: doing money arithmetic on it.
    closed: str = ""
    live: str = ""


class SalesSummaryRead(ApiModel):
    """All three windows in one response.

    Sent together rather than as three endpoints because the panel polls every second and
    needs all of them on screen at once; splitting it would triple the query load to show
    the same numbers.
    """

    today: RollupRead
    week: RollupRead
    month: RollupRead


# ----------------------------------------------------------------------- alerts


class AlertRead(ApiModel):
    kind: AlertKind
    unit_id: str
    session_id: str
    message: str
    triggers_lock: bool


# --------------------------------------------------------------------- pricing


class InventoryCreate(ApiModel):
    name: str = Field(min_length=1, max_length=80)
    unit_price_paise: int = Field(ge=0)
    category: str = Field(default="", max_length=40)
    stock_qty: int = Field(default=0, ge=0)
    low_stock_threshold: int = Field(default=0, ge=0)


class InventoryUpdate(ApiModel):
    """Every field optional — a PATCH. Stock is deliberately not here; it moves only
    through a sale or a restock so it always has a reason."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    unit_price_paise: int | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=40)
    low_stock_threshold: int | None = Field(default=None, ge=0)
    archived: bool | None = None


class InventoryRead(ApiModel):
    id: str
    name: str
    category: str
    unit_price_paise: int
    stock_qty: int
    low_stock_threshold: int
    archived: bool

    #: Preformatted for the shelf list; the client does no money arithmetic.
    unit_price: str = ""

    #: True at or below the threshold — the dashboard flags these and a sale reaching one
    #: raises the alert.
    is_low: bool = False


class SessionItemAdd(ApiModel):
    item_id: str
    qty: int = Field(default=1, gt=0, le=99)


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

    #: True for the row a new session would snapshot right now. Future-dated rows are
    #: scheduled rather than live, and the difference has to be obvious on screen — a
    #: manager who cannot tell which rate is in force will price a session wrong.
    is_current: bool = False
