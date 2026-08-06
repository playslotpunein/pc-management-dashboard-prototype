"""The agent WebSocket.

Holds a persistent connection from every agent, signature-verified per device, in the
same process as the session engine — so nothing sits between a state change and the lock
command that follows from it.

## The one message type

The server sends exactly one kind of message: ``state``. It carries whether the unit
should be locked *and* when the current session ends.

That second field is what makes Zone 5 work. The agent caches the end time, so when it
loses the control server it can wait sixty seconds, look at its cache and answer one
question: is there paid time remaining? Yes — stay unlocked and keep counting down
locally. No — lock. Without a pushed end time the agent has nothing to fail safe *on*,
and a network blip either strands a paying customer behind a lock screen or hands out
free play.

A single idempotent message also means reconnection needs no special handling. The agent
reconnects, gets the current state, and is correct — no replay of missed commands, no
ordering to reason about.

## Delivery is best-effort, on purpose

If an agent is not connected, the command is logged and dropped. The architecture is
explicit that a dead link is the agent's problem to survive: it falls into the fail-safe
branch on its own. Queueing commands for a disconnected agent would deliver a stale lock
to a machine whose customer has since paid for another hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from playslot.clock import Clock, ensure_utc
from playslot.db import unit_of_work
from playslot.enums import SessionStatus, UnitState
from playslot.models import ActivityLog, Agent, Session, Unit
from playslot.security import ReplayGuard, SignatureError, build_envelope, verify

log = logging.getLogger(__name__)

#: An agent heartbeats every 5s; three missed beats and we treat the link as dead.
CONNECTION_TIMEOUT_SECONDS = 20


@dataclass
class Connection:
    unit_id: str
    socket: WebSocket
    device_token: str


class AgentHub:
    """Registry of live agent connections, and the engine's command sink."""

    def __init__(
        self,
        factory: sessionmaker,
        *,
        venue_id: str,
        clock: Clock | None = None,
    ) -> None:
        self._factory = factory
        self._venue_id = venue_id
        self._clock = clock or Clock()

        self._connections: dict[str, Connection] = {}
        self._guard = ReplayGuard()
        self._lock = asyncio.Lock()

    @property
    def connected_units(self) -> list[str]:
        return sorted(self._connections)

    def is_connected(self, unit_id: str) -> bool:
        return unit_id in self._connections

    # ------------------------------------------------------------------ sending

    async def send_state(self, unit_id: str, *, locked: bool) -> bool:
        """Push the authoritative state to one agent. Returns whether it was delivered."""
        connection = self._connections.get(unit_id)

        if connection is None:
            # Expected, not exceptional: the PC may be off, or rebooting. The agent
            # fails safe on its own and re-syncs when it reconnects.
            log.info("No agent connected for unit %s; state not pushed", unit_id)
            return False

        body = self._state_body(unit_id, locked=locked)

        envelope = build_envelope(
            connection.device_token,
            unit_id=unit_id,
            message_type="state",
            body=body,
        )

        try:
            await connection.socket.send_json(envelope)
            return True
        except Exception:
            log.warning("Failed to push state to unit %s; dropping connection", unit_id)
            await self._drop(unit_id)
            return False

    def _state_body(self, unit_id: str, *, locked: bool) -> dict[str, Any]:
        """What the agent needs to act now *and* to survive losing us."""
        with unit_of_work(self._factory) as db:
            unit = db.get(Unit, unit_id)

            body: dict[str, Any] = {
                "locked": locked,
                "unit_state": unit.state.value if unit else UnitState.AVAILABLE.value,
                "session_end_utc": None,
                "grace_end_utc": None,
            }

            if unit is None or unit.current_session_id is None:
                return body

            session = db.get(Session, unit.current_session_id)

            if session is None or session.status is not SessionStatus.ACTIVE:
                return body

            if session.start_time is None:
                return body

            booked = session.duration_minutes + sum(
                extension["minutes"] for extension in session.extensions
            )

            # An open-ended walk-in has no end time. Sending null is correct and the
            # agent must read it as "no deadline", not as "expired" — otherwise the
            # fail-safe would lock a customer who is still paying by the minute.
            if booked <= 0:
                return body

            end = ensure_utc(session.start_time) + timedelta(minutes=booked)

            body["session_end_utc"] = end.isoformat()
            body["grace_end_utc"] = (
                end + timedelta(minutes=session.grace_minutes)
            ).isoformat()

            return body

    async def command_sink(self, unit_id: str, lock: bool) -> None:
        """Wired to the session engine. Signature matches ``CommandSink``."""
        await self.send_state(unit_id, locked=lock)

    # ---------------------------------------------------------------- connection

    async def serve(self, socket: WebSocket, unit_id: str) -> None:
        """Handle one agent connection for its lifetime."""
        await socket.accept()

        agent = self._load_agent(unit_id)

        if agent is None:
            # Closed without explanation. An unenrolled unit id is exactly what probing
            # looks like, and a helpful error would confirm which ids exist.
            log.warning("Rejected connection for unenrolled unit %s", unit_id)
            await socket.close(code=4401)
            return

        token = agent.device_token

        # The first message must be a valid signed hello. Registering the connection
        # before that would let an unauthenticated socket receive state pushes.
        try:
            envelope = await asyncio.wait_for(socket.receive_json(), timeout=10)
            body = verify(token, envelope, guard=self._guard)
        except (TimeoutError, WebSocketDisconnect, ValueError, SignatureError) as exc:
            self._record_failure(unit_id, str(exc))
            with contextlib.suppress(Exception):
                await socket.close(code=4403)
            return

        if envelope.get("type") != "hello":
            self._record_failure(unit_id, "first message was not hello")
            with contextlib.suppress(Exception):
                await socket.close(code=4403)
            return

        await self._register(Connection(unit_id, socket, token))
        self._record_connect(unit_id, body.get("agent_version", ""))

        # Immediately push current state, so a reconnecting agent converges without
        # waiting for the next transition.
        await self.send_state(unit_id, locked=self._current_lock_state(unit_id))

        try:
            while True:
                raw = await socket.receive_json()

                try:
                    payload = verify(token, raw, guard=self._guard)
                except SignatureError as exc:
                    # Logged to the activity feed so attempts are visible, per the
                    # architecture. Not disconnected: a single bad frame is more likely
                    # clock skew than an attack, and dropping the link would lock out a
                    # legitimate unit.
                    self._record_failure(unit_id, str(exc))
                    continue

                await self._handle(unit_id, str(raw.get("type", "")), payload)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("Agent connection for %s failed", unit_id)
        finally:
            await self._drop(unit_id)

    async def _handle(self, unit_id: str, message_type: str, body: dict[str, Any]) -> None:
        if message_type == "heartbeat":
            self._record_heartbeat(unit_id, body)
            return

        if message_type == "hello":
            return

        log.info("Ignoring unknown message type %r from unit %s", message_type, unit_id)

    async def _register(self, connection: Connection) -> None:
        async with self._lock:
            existing = self._connections.get(connection.unit_id)

            if existing is not None:
                # One agent per unit. A second connection is a restarted agent whose old
                # socket has not timed out yet; the newest wins.
                with contextlib.suppress(Exception):
                    await existing.socket.close(code=4409)

            self._connections[connection.unit_id] = connection

    async def _drop(self, unit_id: str) -> None:
        async with self._lock:
            self._connections.pop(unit_id, None)

    # -------------------------------------------------------------- persistence

    def _load_agent(self, unit_id: str) -> Agent | None:
        with unit_of_work(self._factory) as db:
            return db.scalars(
                select(Agent).where(
                    Agent.venue_id == self._venue_id, Agent.unit_id == unit_id
                )
            ).first()

    def _current_lock_state(self, unit_id: str) -> bool:
        with unit_of_work(self._factory) as db:
            unit = db.get(Unit, unit_id)
            return unit is not None and unit.state is UnitState.LOCKED

    def _record_heartbeat(self, unit_id: str, body: dict[str, Any]) -> None:
        with unit_of_work(self._factory) as db:
            agent = db.scalars(
                select(Agent).where(Agent.unit_id == unit_id)
            ).first()

            if agent is None:
                return

            agent.last_heartbeat = self._clock.now()
            agent.failed_verifications = 0

            if version := body.get("agent_version"):
                agent.agent_version = str(version)

    def _record_connect(self, unit_id: str, agent_version: str) -> None:
        with unit_of_work(self._factory) as db:
            db.add(
                ActivityLog(
                    venue_id=self._venue_id,
                    timestamp=self._clock.now(),
                    event="agent.connected",
                    unit_id=unit_id,
                    actor=f"agent:{unit_id}",
                    detail=f"version {agent_version or 'unknown'}",
                )
            )

    def _record_failure(self, unit_id: str, reason: str) -> None:
        """Failed verifications go to the activity feed so attempts are visible."""
        log.warning("Signature check failed for unit %s: %s", unit_id, reason)

        with unit_of_work(self._factory) as db:
            agent = db.scalars(
                select(Agent).where(Agent.unit_id == unit_id)
            ).first()

            if agent is not None:
                # A climbing count on one unit is an attack in progress rather than a
                # flaky clock.
                agent.failed_verifications += 1

            db.add(
                ActivityLog(
                    venue_id=self._venue_id,
                    timestamp=self._clock.now(),
                    event="agent.verification_failed",
                    unit_id=unit_id,
                    actor=f"agent:{unit_id}",
                    detail=reason,
                )
            )
