"""FastAPI application.

The control server. Serves the manager dashboard's data and runs the session engine as a
background task in the same process, so no cross-process hop sits between a state change
and a lock command.

Note what is *not* here: no timer arithmetic, no billing, no state decisions. Those live
in the engine. These routes translate HTTP to engine calls and back, which is why the
dashboard can be replaced without touching a rule.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from sqlalchemy import select

from playslot.clock import Clock
from playslot.config import settings
from playslot.db import create_all, create_db_engine, session_factory, unit_of_work
from playslot.engine import sales as sales_engine
from playslot.engine.session_engine import (
    SessionEngine,
    SessionEngineError,
    SessionNotFound,
    UnitBusy,
    UnitNotFound,
)
from playslot.enums import SessionStatus, UnitState
from playslot.models import Agent, Pricing, Sale, Session, Unit
from playslot.security import new_device_token
from playslot.ws import AgentHub
from playslot.money import format_rupees
from playslot.schemas import (
    BillRead,
    PricingCreate,
    PricingRead,
    RollupRead,
    SaleRead,
    SessionEnd,
    SessionExtend,
    SessionRead,
    SessionStart,
    TypeRollupRead,
    UnitCreate,
    UnitLive,
    UnitRead,
)

engine_holder: dict[str, SessionEngine] = {}
factory_holder: dict[str, object] = {}
hub_holder: dict[str, AgentHub] = {}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_engine = create_db_engine(settings.database_url, echo=settings.echo_sql)
    create_all(db_engine)

    factory = session_factory(db_engine)
    factory_holder["factory"] = factory

    clock = Clock()

    # The hub is the engine's command sink: a lock decision goes straight out over the
    # agent's socket without a cross-process hop.
    hub = AgentHub(factory, venue_id=settings.venue_id, clock=clock)
    hub_holder["hub"] = hub

    engine = SessionEngine(
        factory,
        venue_id=settings.venue_id,
        clock=clock,
        command_sink=hub.command_sink,
        warning_seconds=settings.warning_seconds,
        no_show_timeout_minutes=settings.no_show_timeout_minutes,
    )

    engine_holder["engine"] = engine

    # The engine runs here, in the API process, not in the browser. Close every tab and
    # the floor keeps ticking.
    engine.start(interval_seconds=settings.tick_seconds)

    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(
    title="PlaySlot Control Server",
    version="0.1.0",
    summary="Venue-side session engine, billing, alerts and sales.",
    lifespan=lifespan,
)


def get_engine() -> SessionEngine:
    engine = engine_holder.get("engine")

    if engine is None:
        raise HTTPException(503, "Session engine is not running")

    return engine


def get_factory():
    return factory_holder["factory"]


def get_hub() -> AgentHub:
    hub = hub_holder.get("hub")

    if hub is None:
        raise HTTPException(503, "Agent hub is not running")

    return hub


EngineDep = Annotated[SessionEngine, Depends(get_engine)]
FactoryDep = Annotated[object, Depends(get_factory)]
HubDep = Annotated[AgentHub, Depends(get_hub)]


@app.exception_handler(UnitBusy)
async def _unit_busy(_request, exc: UnitBusy):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc)})


# ------------------------------------------------------------------------ health


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok", "venue": settings.venue_id}


# ------------------------------------------------------------------------- units


@app.get("/units", response_model=list[UnitLive], tags=["units"])
async def list_units(engine: EngineDep, factory: FactoryDep) -> list[UnitLive]:
    """Every unit with its live countdown and running total.

    Everything a card needs is computed here. The dashboard renders these values and
    derives nothing of its own.
    """
    out: list[UnitLive] = []

    with unit_of_work(factory) as db:
        for unit in db.scalars(
            select(Unit).where(Unit.venue_id == settings.venue_id).order_by(Unit.name)
        ).all():
            live = UnitLive.model_validate(unit)

            if unit.current_session_id:
                session = db.get(Session, unit.current_session_id)

                if session is not None and session.status is SessionStatus.ACTIVE:
                    countdown = engine.countdown_for(session)
                    bill = engine.preview_bill(session.id)

                    live.remaining_seconds = countdown.remaining_seconds
                    live.grace_remaining_seconds = countdown.grace_remaining_seconds
                    live.running_total_paise = bill.total_paise
                    live.running_total = format_rupees(bill.total_paise)
                    live.customer_ref = session.customer_ref
                    live.session_started_at = session.start_time

            out.append(live)

    return out


@app.post("/units", response_model=UnitRead, status_code=201, tags=["units"])
async def create_unit(payload: UnitCreate, factory: FactoryDep) -> UnitRead:
    with unit_of_work(factory) as db:
        unit = Unit(venue_id=settings.venue_id, **payload.model_dump())
        db.add(unit)
        db.flush()

        return UnitRead.model_validate(unit)


@app.post("/units/{unit_id}/maintenance", response_model=UnitRead, tags=["units"])
async def set_maintenance(unit_id: str, on: bool, factory: FactoryDep) -> UnitRead:
    """Toggle maintenance. Only legal on an idle unit, so it cannot strand a customer."""
    from playslot.engine.lifecycle import IllegalTransition, transition

    with unit_of_work(factory) as db:
        unit = db.get(Unit, unit_id)

        if unit is None:
            raise HTTPException(404, f"No unit {unit_id}")

        target = UnitState.MAINTENANCE if on else UnitState.AVAILABLE

        try:
            unit.state = transition(unit.state, target, reason="manager toggle")
        except IllegalTransition as exc:
            raise HTTPException(409, str(exc)) from exc

        db.flush()
        return UnitRead.model_validate(unit)


# ---------------------------------------------------------------------- sessions


@app.post("/sessions", response_model=SessionRead, status_code=201, tags=["sessions"])
async def start_session(
    payload: SessionStart, engine: EngineDep, factory: FactoryDep
) -> SessionRead:
    try:
        session_id = engine.start_session(**payload.model_dump())
    except UnitNotFound as exc:
        raise HTTPException(404, f"No unit {exc}") from exc
    except UnitBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionEngineError as exc:
        raise HTTPException(400, str(exc)) from exc

    with unit_of_work(factory) as db:
        return SessionRead.model_validate(db.get(Session, session_id))


@app.post("/sessions/{session_id}/extend", response_model=SessionRead, tags=["sessions"])
async def extend_session(
    session_id: str, payload: SessionExtend, engine: EngineDep, factory: FactoryDep
) -> SessionRead:
    try:
        engine.extend_session(session_id=session_id, **payload.model_dump())
    except SessionNotFound as exc:
        raise HTTPException(404, f"No session {exc}") from exc
    except SessionEngineError as exc:
        raise HTTPException(400, str(exc)) from exc

    with unit_of_work(factory) as db:
        return SessionRead.model_validate(db.get(Session, session_id))


@app.get("/sessions/{session_id}/bill", response_model=BillRead, tags=["sessions"])
async def preview_bill(session_id: str, engine: EngineDep) -> BillRead:
    """What is owed right now, without ending the session."""
    try:
        return BillRead.of(engine.preview_bill(session_id))
    except SessionNotFound as exc:
        raise HTTPException(404, f"No session {exc}") from exc


@app.post("/sessions/{session_id}/end", response_model=SaleRead, tags=["sessions"])
async def end_session(
    session_id: str, payload: SessionEnd, engine: EngineDep
) -> SaleRead:
    try:
        sale = engine.end_session(session_id=session_id, **payload.model_dump())
    except SessionNotFound as exc:
        raise HTTPException(404, f"No session {exc}") from exc
    except SessionEngineError as exc:
        raise HTTPException(409, str(exc)) from exc

    return SaleRead.model_validate(sale)


# ------------------------------------------------------------------------- sales


@app.get("/sales/today", response_model=RollupRead, tags=["sales"])
async def sales_today(engine: EngineDep, factory: FactoryDep) -> RollupRead:
    """Closed sales plus what is still owed on the floor, by unit type."""
    now = engine.now()
    since = sales_engine.business_day_start(
        now, day_starts_at=time(settings.business_day_starts_hour, 0)
    )

    with unit_of_work(factory) as db:
        result = sales_engine.rollup(
            db,
            venue_id=settings.venue_id,
            since=since,
            until=now,
            live_bill=engine.preview_bill,
        )

    return RollupRead(
        since=result.since,
        until=result.until,
        closed_paise=result.closed_paise,
        live_paise=result.live_paise,
        total_paise=result.total_paise,
        total=format_rupees(result.total_paise),
        by_type=[
            TypeRollupRead(
                unit_type=row.unit_type,
                closed_paise=row.closed_paise,
                live_paise=row.live_paise,
                total_paise=row.total_paise,
                closed_sessions=row.closed_sessions,
                live_sessions=row.live_sessions,
            )
            for row in result.by_type.values()
        ],
        by_payment_method={
            method.value: amount for method, amount in result.by_payment_method.items()
        },
    )


@app.get("/sales", response_model=list[SaleRead], tags=["sales"])
async def list_sales(
    factory: FactoryDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[SaleRead]:
    with unit_of_work(factory) as db:
        rows = db.scalars(
            select(Sale)
            .where(Sale.venue_id == settings.venue_id)
            .order_by(Sale.settled_at.desc())
            .limit(limit)
        ).all()

        return [SaleRead.model_validate(row) for row in rows]


# ----------------------------------------------------------------------- pricing


@app.post("/pricing", response_model=PricingRead, status_code=201, tags=["pricing"])
async def set_pricing(
    payload: PricingCreate, engine: EngineDep, factory: FactoryDep
) -> PricingRead:
    """Insert a new pricing row. Never updates an existing one.

    Open sessions are unaffected: they hold the rate captured when they started.
    """
    with unit_of_work(factory) as db:
        row = Pricing(
            venue_id=settings.venue_id,
            unit_type=payload.unit_type,
            hourly_rate_paise=payload.hourly_rate_paise,
            overtime_rate_paise_per_minute=payload.overtime_rate_paise_per_minute,
            controller_surcharge_paise_per_hour=(
                payload.controller_surcharge_paise_per_hour
            ),
            effective_from=payload.effective_from or engine.now(),
        )

        db.add(row)
        db.flush()

        return PricingRead.model_validate(row)


@app.get("/pricing", response_model=list[PricingRead], tags=["pricing"])
async def list_pricing(factory: FactoryDep) -> list[PricingRead]:
    with unit_of_work(factory) as db:
        rows = db.scalars(
            select(Pricing)
            .where(Pricing.venue_id == settings.venue_id)
            .order_by(Pricing.unit_type, Pricing.effective_from.desc())
        ).all()

        return [PricingRead.model_validate(row) for row in rows]


# ------------------------------------------------------------------------ agents


@app.post("/agents/enroll", status_code=201, tags=["agents"])
async def enroll_agent(unit_id: str, factory: FactoryDep) -> dict[str, str]:
    """Issue a device secret for a unit.

    Returned in the clear exactly once, at enrolment, because the agent has to be
    configured with it. Re-enrolling a unit rotates the secret and immediately
    invalidates the old one, which is the revocation path for a stolen token.
    """
    with unit_of_work(factory) as db:
        unit = db.get(Unit, unit_id)

        if unit is None:
            raise HTTPException(404, f"No unit {unit_id}")

        token = new_device_token()

        agent = db.scalars(select(Agent).where(Agent.unit_id == unit_id)).first()

        if agent is None:
            agent = Agent(venue_id=settings.venue_id, unit_id=unit_id, device_token=token)
            db.add(agent)
        else:
            agent.device_token = token
            agent.failed_verifications = 0

        db.flush()

        return {"unit_id": unit_id, "device_token": token}


@app.get("/agents", tags=["agents"])
async def list_agents(hub: HubDep, factory: FactoryDep) -> list[dict]:
    """Enrolment and liveness per unit. The device token is never returned."""
    with unit_of_work(factory) as db:
        rows = db.scalars(
            select(Agent).where(Agent.venue_id == settings.venue_id)
        ).all()

        return [
            {
                "unit_id": row.unit_id,
                "agent_version": row.agent_version,
                "last_heartbeat": row.last_heartbeat,
                "failed_verifications": row.failed_verifications,
                "connected": hub.is_connected(row.unit_id),
            }
            for row in rows
        ]


@app.websocket("/agent/{unit_id}")
async def agent_socket(websocket: WebSocket, unit_id: str) -> None:
    """The persistent agent link.

    Authentication is the first message, not a query parameter or a header — a token in
    a URL ends up in access logs and browser history. The agent sends a signed ``hello``
    and nothing is registered until it verifies.
    """
    hub = hub_holder.get("hub")

    if hub is None:
        await websocket.close(code=1013)
        return

    await hub.serve(websocket, unit_id)
