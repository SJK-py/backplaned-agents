"""`append_audit_event` caps payload size at 8 KiB.

The audit_log is hash-chained and append-only — every row's
`payload jsonb` field counts toward both the row size on disk AND
the SHA-256 input for the chain hash on every subsequent append.
A misbehaving caller writing a 5 MB blob would balloon the table
AND multiply the hash CPU cost for every future row in the chain.

The cap REPLACES the payload with a small marker on overflow
(rather than truncating in-place) so the JSON structure stays
valid and operators can see the original size. Truncation happens
BEFORE hashing so the chain stays consistent.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_maybe_truncate_returns_payload_unchanged_under_cap() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import _maybe_truncate_audit_payload

    payload = {"event": "user.created", "user_id": "usr_x", "level": "tier1"}
    out = _maybe_truncate_audit_payload(payload)
    assert out is payload  # identity preserved when under cap


def test_maybe_truncate_returns_empty_dict_for_none() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import _maybe_truncate_audit_payload

    assert _maybe_truncate_audit_payload(None) == {}
    assert _maybe_truncate_audit_payload({}) == {}


def test_maybe_truncate_replaces_oversized_payload_with_marker() -> None:
    """An 8 KiB+ payload gets replaced with a small marker carrying
    the original size and the cap. The original payload is NOT in
    the marker (that would defeat the cap)."""
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import (
        _AUDIT_PAYLOAD_MAX_BYTES,
        AUDIT_TRUNCATION_MARKER_KEY,
        _maybe_truncate_audit_payload,
    )

    huge_payload = {"blob": "x" * (_AUDIT_PAYLOAD_MAX_BYTES + 100)}
    out = _maybe_truncate_audit_payload(huge_payload)

    # R5: marker key namespaced so a legit caller's `_truncated`
    # field can no longer collide with the truncation sentinel.
    assert out[AUDIT_TRUNCATION_MARKER_KEY] is True
    assert out["max_bytes"] == _AUDIT_PAYLOAD_MAX_BYTES
    assert out["original_size_bytes"] > _AUDIT_PAYLOAD_MAX_BYTES
    # The original blob is NOT in the marker.
    assert "blob" not in out


def test_append_audit_event_uses_truncation_helper() -> None:
    """Source pin: `append_audit_event` calls the truncation helper
    BEFORE hashing so the chain hash is computed over the truncated
    form. A regression that bypasses the helper would let huge
    payloads back in."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.append_audit_event)
    assert "_maybe_truncate_audit_payload" in src
    # And the INSERT uses the truncated form, not the original.
    # Pin the variable name we used.
    assert "stored_payload" in src


def test_append_audit_event_hashes_the_truncated_payload() -> None:
    """The hash chain integrity depends on the body that produced
    the stored row. If we hashed the ORIGINAL payload but stored
    the TRUNCATED one, the chain would fail integrity verification
    on every audit-log inspector."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    src = inspect.getsource(queries.append_audit_event)
    # The body-dict that goes into sha256 references the same
    # variable as the INSERT.
    assert '"payload": stored_payload,' in src


def test_truncate_cap_is_sensible() -> None:
    """8 KiB is a sensible default — much larger than the structured
    payloads the router writes today (typically <500 bytes) but
    small enough that a misbehaving caller can't fill the table."""
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import _AUDIT_PAYLOAD_MAX_BYTES

    assert 1024 <= _AUDIT_PAYLOAD_MAX_BYTES <= 64 * 1024


def test_marker_key_is_namespaced() -> None:
    """R5: the truncation marker key uses a `__bp_audit_*__`
    namespace so a legit caller's `_truncated: True` field can't
    collide with the sentinel. A regression that reverts to plain
    `_truncated` fails this pin."""
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import AUDIT_TRUNCATION_MARKER_KEY

    # Namespaced shape: starts AND ends with `__`, contains `bp` for
    # disambiguation.
    assert AUDIT_TRUNCATION_MARKER_KEY.startswith("__")
    assert AUDIT_TRUNCATION_MARKER_KEY.endswith("__")
    assert "bp" in AUDIT_TRUNCATION_MARKER_KEY


def test_legit_caller_passing_underscore_truncated_passes_through() -> None:
    """Pre-R5 a caller writing `{"_truncated": True}` (≤8 KiB) was
    indistinguishable from the truncation marker on read. Post-R5
    the legit caller's payload round-trips unchanged because the
    sentinel key is now `__bp_audit_truncated__`."""
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import (
        AUDIT_TRUNCATION_MARKER_KEY,
        _maybe_truncate_audit_payload,
    )

    legit_payload = {
        "_truncated": True,
        "user_action": "manual_truncate",
        "field": "description",
    }
    out = _maybe_truncate_audit_payload(legit_payload)
    # Original payload round-trips (it's under cap).
    assert out is legit_payload
    # The namespaced sentinel is NOT present — readers can't be
    # fooled into thinking this was a truncated row.
    assert AUDIT_TRUNCATION_MARKER_KEY not in out


def test_append_audit_event_writes_marker_for_huge_payload() -> None:
    """Functional: feed `append_audit_event` a 100 KB payload and
    assert the INSERT row carries the marker, not the original."""
    pytest.importorskip("asyncpg")
    import asyncio

    from bp_router.db import queries

    async def _run() -> None:
        conn = MagicMock()
        # Stub the transactional context manager.
        conn.transaction.return_value.__aenter__ = AsyncMock(return_value=conn)
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=None)  # empty audit_log

        huge_payload = {"blob": "y" * 100_000}
        await queries.append_audit_event(
            conn,
            actor_kind="user",
            actor_id="usr_z",
            event="test.huge_event",
            payload=huge_payload,
        )

        # The INSERT executes with the marker, not the original payload.
        from bp_router.db.queries import AUDIT_TRUNCATION_MARKER_KEY

        insert_call = next(
            c for c in conn.execute.call_args_list
            if "INSERT INTO audit_log" in str(c.args[0])
        )
        # 7th positional argument is the payload.
        stored = insert_call.args[7]
        assert stored.get(AUDIT_TRUNCATION_MARKER_KEY) is True
        assert "blob" not in stored

    asyncio.run(_run())
