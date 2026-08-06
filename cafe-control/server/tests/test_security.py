"""Signing and replay tests.

The threat these defend against is concrete: someone on the café wifi sending an unlock
for unit 5 and playing free.
"""

from __future__ import annotations

import time

import pytest

from playslot.security import (
    ReplayGuard,
    SignatureError,
    build_envelope,
    canonical_bytes,
    new_device_token,
    sign,
    verify,
)

SECRET = "a" * 64
UNIT = "unit-pc-01"


def envelope(**overrides):
    base = build_envelope(SECRET, unit_id=UNIT, message_type="heartbeat", body={"state": "active"})
    base.update(overrides)
    return base


class TestCanonicalForm:
    def test_key_order_does_not_change_the_bytes(self):
        """The C# agent must produce identical bytes, whatever its dictionary order."""
        first = canonical_bytes(
            unit_id=UNIT, message_type="t", timestamp=1, nonce="n",
            body={"a": 1, "b": 2},
        )
        second = canonical_bytes(
            unit_id=UNIT, message_type="t", timestamp=1, nonce="n",
            body={"b": 2, "a": 1},
        )

        assert first == second

    def test_body_json_has_no_incidental_whitespace(self):
        raw = canonical_bytes(
            unit_id=UNIT, message_type="t", timestamp=1, nonce="n", body={"a": 1, "b": 2}
        ).decode()

        assert raw.endswith('{"a":1,"b":2}')

    def test_fields_cannot_be_shifted_into_one_another(self):
        """Newline separators mean no field boundary can be forged by concatenation."""
        first = canonical_bytes(
            unit_id="ab", message_type="c", timestamp=1, nonce="n", body={}
        )
        second = canonical_bytes(
            unit_id="a", message_type="bc", timestamp=1, nonce="n", body={}
        )

        assert first != second


class TestVerification:
    def test_a_well_formed_message_verifies(self):
        assert verify(SECRET, envelope()) == {"state": "active"}

    def test_a_tampered_body_is_rejected(self):
        message = envelope()
        message["body"] = {"state": "unlocked"}

        with pytest.raises(SignatureError):
            verify(SECRET, message)

    def test_a_different_secret_is_rejected(self):
        with pytest.raises(SignatureError):
            verify(new_device_token(), envelope())

    def test_a_forged_unit_id_is_rejected(self):
        """The wifi attack, directly: sign for your own unit, aim at someone else's."""
        message = envelope()
        message["unit_id"] = "unit-pc-05"

        with pytest.raises(SignatureError):
            verify(SECRET, message)

    def test_the_message_type_is_signed(self):
        # Otherwise a captured heartbeat could be relabelled as a command.
        message = envelope()
        message["type"] = "state"

        with pytest.raises(SignatureError):
            verify(SECRET, message)

    @pytest.mark.parametrize("missing", ["unit_id", "type", "ts", "nonce", "body", "sig"])
    def test_a_missing_field_is_rejected(self, missing):
        message = envelope()
        del message[missing]

        with pytest.raises(SignatureError):
            verify(SECRET, message)

    def test_a_non_dict_body_is_rejected(self):
        message = envelope()
        message["body"] = "not-a-dict"

        with pytest.raises(SignatureError):
            verify(SECRET, message)

    def test_failures_do_not_say_which_check_failed(self):
        """Distinguishable errors are a probing oracle for anyone on the wifi."""
        reasons = set()

        for message in (
            {**envelope(), "sig": "0" * 64},
            {**envelope(), "unit_id": "other"},
            {**envelope(), "body": {"state": "x"}},
        ):
            with pytest.raises(SignatureError) as caught:
                verify(SECRET, message)

            reasons.add(str(caught.value))

        assert reasons == {"bad signature"}


class TestReplay:
    def test_a_valid_message_replayed_is_rejected(self):
        """A captured, perfectly-signed unlock must not work twice."""
        guard = ReplayGuard()
        message = envelope()
        now = message["ts"]

        verify(SECRET, message, guard=guard, now=now)

        with pytest.raises(SignatureError, match="nonce reused"):
            verify(SECRET, message, guard=guard, now=now)

    def test_a_stale_message_is_rejected(self):
        guard = ReplayGuard(freshness_seconds=30)
        message = envelope()

        with pytest.raises(SignatureError, match="stale"):
            verify(SECRET, message, guard=guard, now=message["ts"] + 31)

    def test_a_future_dated_message_is_rejected(self):
        guard = ReplayGuard(freshness_seconds=30)
        message = envelope()

        with pytest.raises(SignatureError, match="stale"):
            verify(SECRET, message, guard=guard, now=message["ts"] - 31)

    def test_modest_clock_skew_is_tolerated(self):
        # A café PC that has never synced properly should still work.
        guard = ReplayGuard(freshness_seconds=30)
        message = envelope()

        assert verify(SECRET, message, guard=guard, now=message["ts"] + 20)

    def test_the_nonce_store_does_not_grow_without_bound(self):
        """A unit beating every 5s all day is ~9,000 nonces; they have to be pruned."""
        guard = ReplayGuard(freshness_seconds=30)
        start = int(time.time())

        for offset in range(0, 300, 5):
            message = build_envelope(
                SECRET, unit_id=UNIT, message_type="heartbeat", body={"i": offset}
            )
            message["ts"] = start + offset
            message["sig"] = sign(
                SECRET,
                unit_id=UNIT,
                message_type="heartbeat",
                timestamp=message["ts"],
                nonce=message["nonce"],
                body=message["body"],
            )

            verify(SECRET, message, guard=guard, now=start + offset)

        # Only the freshness window's worth is retained, not all 60.
        assert len(guard._seen) <= 10

    def test_nonces_are_scoped_per_device(self):
        # Two units legitimately colliding on a nonce must not block each other.
        guard = ReplayGuard()
        now = int(time.time())

        first = build_envelope(SECRET, unit_id="unit-a", message_type="heartbeat", body={})
        second = build_envelope(SECRET, unit_id="unit-b", message_type="heartbeat", body={})
        second["nonce"] = first["nonce"]
        second["ts"] = first["ts"]
        second["sig"] = sign(
            SECRET,
            unit_id="unit-b",
            message_type="heartbeat",
            timestamp=second["ts"],
            nonce=second["nonce"],
            body=second["body"],
        )

        verify(SECRET, first, guard=guard, now=now)
        verify(SECRET, second, guard=guard, now=now)


class TestTokens:
    def test_tokens_are_long_and_unique(self):
        tokens = {new_device_token() for _ in range(100)}

        assert len(tokens) == 100
        assert all(len(token) == 64 for token in tokens)
