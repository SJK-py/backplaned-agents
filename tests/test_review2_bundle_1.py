"""Tests for the second-pass review fixes (C1, C2, H1, H3).

C1 — `LocalFileStore._path` and `S3FileStore._key` validate that
the sha256 is exactly 64 lowercase hex chars before constructing
a storage path. Refuses path-traversal payloads at the boundary.

C2 — `/agent/refresh-token` revokes the OLD jti via
`revoke_jti(...)` and force-closes any live WS socket
authenticated under that jti, so a leaked agent token can't
keep talking to the router after rotation.

H1 — `update_rule` (ACL PATCH endpoint) switched from
`exclude_none=True` to `exclude_unset=True`, matching the
fix PR #67 applied to `update_llm_preset` for nullable column
clearing. Plus the matching `asyncpg.NotNullViolationError`
handler for explicit-null on NOT NULL columns.

H3 — `AgentInfo.documentation_url` rejects schemes other than
`http(s)://`. Stops a malicious agent from registering
`javascript:...` payloads that would run as stored XSS in
the admin UI's `<a href>` rendering.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# C1: path-traversal in storage backends
# ===========================================================================


def test_local_store_rejects_non_hex_sha256(tmp_path: Path) -> None:
    """A `sha256` argument with `..`, `/`, or any non-hex chars
    must raise BEFORE `Path` arithmetic happens. Without this,
    `Path / "../etc/passwd"` would produce a path outside the
    storage root (review item C1)."""
    from bp_router.storage.local import LocalFileStore

    store = LocalFileStore(tmp_path)
    bad_inputs = [
        "../../../etc/passwd",
        "abc/def",  # contains slash
        "ABCDEF" * 11,  # uppercase, 64 chars but not lowercase
        "g" * 64,  # 64 chars but `g` isn't hex
        "abc" * 21,  # 63 chars (off-by-one)
        "abc" * 22,  # 66 chars (off-by-one other side)
        "",
        " " * 64,  # whitespace
        "abcd" + "\x00" + "ef" * 30,  # null byte
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="sha256"):
            store._path(bad)


def test_local_store_accepts_valid_hex_sha256(tmp_path: Path) -> None:
    """64 lowercase hex chars is the only accepted shape."""
    from bp_router.storage.local import LocalFileStore

    store = LocalFileStore(tmp_path)
    valid = "a" * 64  # all `a`s — valid hex, valid length
    out = store._path(valid)
    assert isinstance(out, Path)
    # And the constructed path is inside the storage root.
    assert tmp_path.resolve() in out.resolve().parents


def test_local_store_path_traversal_payload_does_not_escape_root(
    tmp_path: Path,
) -> None:
    """Behavioral pin: even if a future caller tries to use the
    backslash-escape trick or other path-traversal variant, the
    regex refusal catches it before any filesystem touch."""
    from bp_router.storage.local import LocalFileStore

    store = LocalFileStore(tmp_path)
    payloads = [
        "..\\windows\\system32",  # backslash variant
        "%2e%2e%2fpasswd",  # URL-encoded
        "‮/admin",  # right-to-left override
    ]
    for payload in payloads:
        with pytest.raises(ValueError):
            store._path(payload)


def test_s3_store_key_validates_sha256() -> None:
    """`S3FileStore._key` mirrors the local validation. Pinned at
    source level so a refactor doesn't drop the check from one
    backend without the other."""
    pytest.importorskip("aioboto3")
    from bp_router.storage import s3 as s3_mod

    src = inspect.getsource(s3_mod.S3FileStore._key)
    assert "_validate_sha256" in src


def test_validate_sha256_helper_pin() -> None:
    """The shared validator must check exactly: 64 chars, lowercase
    hex. Pin the regex so a refactor that loosens it (e.g. allows
    uppercase) breaks the test."""
    from bp_router.storage.local import _validate_sha256

    # Valid.
    _validate_sha256("a" * 64)
    _validate_sha256("0123456789abcdef" * 4)
    # Invalid.
    with pytest.raises(ValueError):
        _validate_sha256("A" * 64)  # uppercase rejected
    with pytest.raises(ValueError):
        _validate_sha256("a" * 63)  # too short
    with pytest.raises(ValueError):
        _validate_sha256("a" * 65)  # too long
    with pytest.raises(ValueError):
        _validate_sha256("../" + "a" * 60)


# ===========================================================================
# C2: auth-token rotation revokes old jti + closes socket
# ===========================================================================


def test_refresh_token_revokes_old_jti(monkeypatch: pytest.MonkeyPatch) -> None:
    """After `/agent/refresh-token` succeeds, the OLD jti is
    revoked via `revoke_jti` so any subsequent request /
    handshake under it fails (review item C2)."""
    pytest.importorskip("fastapi")
    from bp_router.api import onboard
    from bp_router.security.jwt import AgentPrincipal

    revoked: list[tuple[Any, str, int]] = []

    async def _fake_revoke(redis: Any, jti: str, *, ttl_s: int) -> None:
        revoked.append((redis, jti, ttl_s))

    monkeypatch.setattr(onboard, "revoke_jti", _fake_revoke)

    # Stub verify_agent_token to return a known principal.
    fake_principal = AgentPrincipal(
        agent_id="agt_alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=3600),
        jti="old_jti_to_revoke",
        sdk_protocol_version="1",
    )
    monkeypatch.setattr(
        onboard, "verify_agent_token", lambda *a, **kw: fake_principal
    )
    monkeypatch.setattr(
        onboard, "is_jti_revoked", AsyncMock(return_value=False)
    )

    # Stub agent row lookup: agent is active.
    agent_row = MagicMock()
    agent_row.status = "active"
    monkeypatch.setattr(
        onboard.queries, "get_agent", AsyncMock(return_value=agent_row)
    )

    state = MagicMock()
    state.redis = MagicMock()  # configured
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.agent_token_ttl_s = 86_400

    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # No active socket — closer path is exercised in a separate test.
    state.socket_registry = MagicMock()
    state.socket_registry.get = lambda agent_id: None

    request = MagicMock()
    request.app.state.bp = state

    req = onboard.RefreshAgentTokenRequest(agent_id="agt_alice")
    out = asyncio.run(onboard.refresh_agent_token(
        req, request, authorization="Bearer fake-token-text",
    ))

    # Old jti was revoked.
    assert len(revoked) == 1
    _redis, jti_arg, ttl_arg = revoked[0]
    assert jti_arg == "old_jti_to_revoke"
    assert ttl_arg >= state.settings.agent_token_ttl_s
    # New token returned.
    assert out.auth_token != "fake-token-text"


def test_refresh_token_closes_active_socket_with_matching_jti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the rotated jti matches the active WS socket's
    `auth_jti`, the socket is force-closed (review item C2).
    Without this, the live socket keeps handling frames under
    the old (now-revoked) token until natural expiry."""
    pytest.importorskip("fastapi")
    from bp_router.api import onboard
    from bp_router.security.jwt import AgentPrincipal

    fake_principal = AgentPrincipal(
        agent_id="agt_alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=3600),
        jti="active_jti",
        sdk_protocol_version="1",
    )
    monkeypatch.setattr(
        onboard, "verify_agent_token", lambda *a, **kw: fake_principal
    )
    monkeypatch.setattr(
        onboard, "is_jti_revoked", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(onboard, "revoke_jti", AsyncMock(return_value=None))

    agent_row = MagicMock()
    agent_row.status = "active"
    monkeypatch.setattr(
        onboard.queries, "get_agent", AsyncMock(return_value=agent_row)
    )

    state = MagicMock()
    state.redis = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.agent_token_ttl_s = 86_400
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Active socket with the SAME jti.
    entry = MagicMock()
    entry.auth_jti = "active_jti"  # matches!
    entry.websocket.close = AsyncMock()
    entry.closed = MagicMock()
    state.socket_registry.get = lambda agent_id: entry

    request = MagicMock()
    request.app.state.bp = state
    req = onboard.RefreshAgentTokenRequest(agent_id="agt_alice")

    asyncio.run(onboard.refresh_agent_token(
        req, request, authorization="Bearer fake",
    ))

    # Socket was closed with the rotation reason.
    entry.websocket.close.assert_awaited_once()
    close_kwargs = entry.websocket.close.call_args.kwargs
    assert close_kwargs.get("code") == 4001
    assert close_kwargs.get("reason") == "auth_token_rotated"
    entry.closed.set.assert_called_once()


def test_refresh_token_does_not_close_socket_with_different_jti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the active socket was authenticated with a DIFFERENT
    jti (e.g. the agent already reconnected with a fresh token),
    the rotation revoke must NOT close it. Only the precise
    old-jti socket is killed."""
    pytest.importorskip("fastapi")
    from bp_router.api import onboard
    from bp_router.security.jwt import AgentPrincipal

    fake_principal = AgentPrincipal(
        agent_id="agt_alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=3600),
        jti="old_being_rotated",
        sdk_protocol_version="1",
    )
    monkeypatch.setattr(
        onboard, "verify_agent_token", lambda *a, **kw: fake_principal
    )
    monkeypatch.setattr(
        onboard, "is_jti_revoked", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(onboard, "revoke_jti", AsyncMock(return_value=None))

    agent_row = MagicMock()
    agent_row.status = "active"
    monkeypatch.setattr(
        onboard.queries, "get_agent", AsyncMock(return_value=agent_row)
    )

    state = MagicMock()
    state.redis = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = "x" * 32
    state.settings.jwt_algorithm = "HS256"
    state.settings.jwt_key_version = 1
    state.settings.agent_token_ttl_s = 86_400
    pool = MagicMock()
    state.db_pool = pool
    conn = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Active socket with a DIFFERENT jti.
    entry = MagicMock()
    entry.auth_jti = "different_jti_already_reconnected"
    entry.websocket.close = AsyncMock()
    state.socket_registry.get = lambda agent_id: entry

    request = MagicMock()
    request.app.state.bp = state
    req = onboard.RefreshAgentTokenRequest(agent_id="agt_alice")

    asyncio.run(onboard.refresh_agent_token(
        req, request, authorization="Bearer fake",
    ))

    # Socket was NOT closed.
    entry.websocket.close.assert_not_called()


def test_socket_entry_carries_auth_jti() -> None:
    """`SocketEntry` must declare `auth_jti` so the rotation path
    can compare it. Pin the dataclass field so a refactor doesn't
    drop it silently."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketEntry

    fields = {f.name for f in SocketEntry.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "auth_jti" in fields


# ===========================================================================
# H1: update_rule exclude_unset semantics
# ===========================================================================


def test_update_rule_uses_exclude_unset() -> None:
    """Source pin: `update_rule` uses `model_dump(exclude_unset=True)`,
    matching the fix PR #67 applied to `update_llm_preset`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_rule)
    assert "model_dump(exclude_unset=True)" in src
    assert "model_dump(exclude_none=True)" not in src
    # And the matching NotNullViolationError handler.
    assert "asyncpg.NotNullViolationError" in src
    assert "cannot be set to null" in src


# ===========================================================================
# H3: documentation_url scheme validation
# ===========================================================================


@pytest.mark.parametrize("url", [
    "http://docs.example.com/agent",
    "https://docs.example.com/agent",
    "HTTPS://Docs.Example.com/agent",  # case-insensitive
    None,  # nullable; accepted unchanged
    "",  # empty; accepted (treated as no URL)
])
def test_documentation_url_accepts_valid_schemes(url: Any) -> None:
    """`http(s)://...` and `None` / empty are the only accepted
    shapes."""
    from bp_protocol.types import AgentInfo

    info = AgentInfo(
        agent_id="agt_test",
        description="x",
        documentation_url=url,
    )
    assert info.documentation_url == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "javascript:fetch('/admin/users', {method:'POST'})",
    "JavaScript:alert(1)",  # mixed case
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com",  # not http(s)
    "//evil.com/payload",  # protocol-relative
    "javascript: alert(1)",  # leading space inside scheme
    "  javascript:alert(1)",  # leading whitespace before scheme
    "/relative/path",  # relative URL
    "agent.example.com",  # no scheme at all
])
def test_documentation_url_rejects_xss_vectors(url: str) -> None:
    """A malicious agent shouldn't be able to register a URL
    whose scheme would execute as JavaScript when rendered into
    the admin UI's `<a href>` (review item H3)."""
    from pydantic import ValidationError

    from bp_protocol.types import AgentInfo

    with pytest.raises(ValidationError, match="documentation_url"):
        AgentInfo(
            agent_id="agt_test",
            description="x",
            documentation_url=url,
        )


def test_documentation_url_validator_present_at_protocol_level() -> None:
    """Pin the field validator on the model. A refactor that
    moves validation to a higher layer (e.g. only at the admin
    UI) would let WS-handshake / catalog paths receive the bad
    URL — defence-in-depth means the protocol type itself
    refuses it."""
    from bp_protocol.types import AgentInfo

    src = inspect.getsource(AgentInfo)
    assert "_documentation_url_scheme" in src
    assert "@field_validator(\"documentation_url\")" in src
