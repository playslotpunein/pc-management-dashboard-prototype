"""Message signing for the agent link.

Every message in both directions is HMAC-SHA256 signed against a per-device secret
issued at enrolment. The architecture is blunt about why: without it, anyone on the café
wifi sends an unlock for unit 5 and plays free.

Signing runs **both ways**, not just agent to server. An unsigned server-to-agent channel
is the same hole viewed from the other end — a laptop on the same wifi could send an
agent a forged unlock and never touch the control server at all.

## The canonical form

The C# agent has to produce byte-identical input to this, so the rule is exact and has
no room for interpretation:

    unit_id \n message_type \n timestamp \n nonce \n body

where ``body`` is JSON with keys sorted and no whitespace
(``json.dumps(body, sort_keys=True, separators=(",", ":"))``), encoded UTF-8. The
signature is lowercase hex HMAC-SHA256 over that byte string.

The separator is a newline because it cannot appear in a UUID, a message type or a
number, so no field can be shifted into another — signing ``a|b`` and ``ab|`` to the same
string is a real class of bug and this rules it out.

## Replay

A signature alone does not stop someone re-sending a *valid* captured message. A recorded
"unlock" replayed an hour later would be perfectly signed. Two things prevent it:

* messages older than the freshness window are rejected outright, and
* each nonce is accepted once per device inside that window.

Both are needed. The window alone permits replay within it; nonces alone would require
remembering every nonce ever seen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

#: How far a message's timestamp may be from ours. Wide enough to absorb the clock skew
#: of a café PC that has never synced properly, narrow enough that a captured message is
#: useless within the minute.
DEFAULT_FRESHNESS_SECONDS = 30

#: Device secrets. 32 bytes of urandom, hex-encoded: long enough that guessing is not a
#: strategy, printable so it can be pasted into an agent's config during enrolment.
TOKEN_BYTES = 32


class SignatureError(Exception):
    """Raised when a message fails verification. Never leaks which check failed."""


def new_device_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def new_nonce() -> str:
    return secrets.token_hex(16)


def canonical_bytes(
    *, unit_id: str, message_type: str, timestamp: int, nonce: str, body: dict[str, Any]
) -> bytes:
    """The exact bytes that get signed. Must match the agent's implementation."""
    encoded_body = json.dumps(body, sort_keys=True, separators=(",", ":"))

    return "\n".join(
        [unit_id, message_type, str(timestamp), nonce, encoded_body]
    ).encode("utf-8")


def sign(
    secret: str,
    *,
    unit_id: str,
    message_type: str,
    timestamp: int,
    nonce: str,
    body: dict[str, Any],
) -> str:
    payload = canonical_bytes(
        unit_id=unit_id,
        message_type=message_type,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )

    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_envelope(
    secret: str, *, unit_id: str, message_type: str, body: dict[str, Any]
) -> dict[str, Any]:
    """A complete signed message, ready to send."""
    timestamp = int(time.time())
    nonce = new_nonce()

    return {
        "unit_id": unit_id,
        "type": message_type,
        "ts": timestamp,
        "nonce": nonce,
        "body": body,
        "sig": sign(
            secret,
            unit_id=unit_id,
            message_type=message_type,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        ),
    }


@dataclass
class ReplayGuard:
    """Rejects stale messages and nonces already used inside the window."""

    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS

    _seen: dict[tuple[str, str], int] = field(default_factory=dict)

    def check(self, unit_id: str, nonce: str, timestamp: int, *, now: int) -> None:
        """Raise :class:`SignatureError` if this message must not be accepted."""
        if abs(now - timestamp) > self.freshness_seconds:
            raise SignatureError("stale or future-dated message")

        key = (unit_id, nonce)

        if key in self._seen:
            raise SignatureError("nonce reused")

        self._seen[key] = timestamp
        self._prune(now)

    def _prune(self, now: int) -> None:
        """Drop nonces too old to be replayable.

        Bounded on purpose: a unit heartbeating every five seconds for a twelve-hour day
        is nearly nine thousand nonces, and without pruning this grows for as long as the
        process runs.
        """
        cutoff = now - self.freshness_seconds

        stale = [key for key, seen_at in self._seen.items() if seen_at < cutoff]

        for key in stale:
            del self._seen[key]


def verify(
    secret: str,
    envelope: dict[str, Any],
    *,
    guard: ReplayGuard | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify an envelope and return its body. Raises on any failure.

    The error messages are deliberately vague and identical in shape. Telling a caller
    *which* check failed — unknown unit versus bad signature versus replay — is a probing
    oracle, and this is reachable by anyone on the café wifi.
    """
    try:
        unit_id = str(envelope["unit_id"])
        message_type = str(envelope["type"])
        timestamp = int(envelope["ts"])
        nonce = str(envelope["nonce"])
        body = envelope["body"]
        presented = str(envelope["sig"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SignatureError("malformed envelope") from exc

    if not isinstance(body, dict):
        raise SignatureError("malformed envelope")

    expected = sign(
        secret,
        unit_id=unit_id,
        message_type=message_type,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )

    # Constant-time. A plain == leaks how many leading characters matched, which is
    # enough to reconstruct a signature one byte at a time given enough attempts.
    if not hmac.compare_digest(expected, presented):
        raise SignatureError("bad signature")

    if guard is not None:
        guard.check(unit_id, nonce, timestamp, now=now if now is not None else int(time.time()))

    return body
