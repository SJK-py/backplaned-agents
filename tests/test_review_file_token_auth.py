"""Phase 1 of first-class file transfer: the auth foundation.

Agents only carry an `agent`-kind JWT, which the session-scoped
file endpoints reject (`wrong_kind`) — so the router-proxy HTTP
path was unusable for external agents. This adds two router-minted
token kinds + their endpoints:

  * `file-upload` — one-shot, content-bound (sha/size), user-
    scoped; consumed by `POST /v1/files/upload`.
  * `file-fetch`  — bound to ONE file_id + the owning user; lets
    `GET /v1/files/{id}` authorise an agent attachment fetch
    without a session JWT. The session path is byte-for-byte
    unchanged (dual-auth falls back to `_principal_from_request`).

Pure-unit on the jwt helpers + mock-state on the deps/handlers
(the project's established security-test style).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest

from bp_router.security import jwt as J

_SECRET = "k" * 32
_KV = 3


# ---------------------------------------------------------------------------
# jwt mint/verify round-trips
# ---------------------------------------------------------------------------


def test_file_upload_token_round_trip() -> None:
    tok, exp, jti = J.issue_file_upload_token(
        user_id="usr_a", sha256="ab" * 32, byte_size=4096,
        mime_type="image/png", secret=_SECRET, ttl_s=60, key_version=_KV,
    )
    g = J.verify_file_upload_token(tok, secret=_SECRET, key_version=_KV)
    assert (g.user_id, g.sha256, g.byte_size, g.mime_type) == (
        "usr_a", "ab" * 32, 4096, "image/png"
    )
    assert g.jti == jti


def test_file_fetch_token_round_trip() -> None:
    tok, _exp, jti = J.issue_file_fetch_token(
        file_id="file_x", user_id="usr_a", secret=_SECRET,
        ttl_s=60, key_version=_KV,
    )
    g = J.verify_file_fetch_token(tok, secret=_SECRET, key_version=_KV)
    assert g.file_id == "file_x"
    assert g.user_id == "usr_a"
    assert g.jti == jti


def test_cross_kind_is_rejected_both_ways() -> None:
    """A session/agent/file-upload token must NOT verify as
    file-fetch (and vice-versa) — `wrong_kind`."""
    sess, _, _ = J.issue_session_token(
        user_id="u", level="tier0", secret=_SECRET, ttl_s=60, key_version=_KV
    )
    agent, _, _ = J.issue_agent_token(
        agent_id="agt", secret=_SECRET, ttl_s=60, key_version=_KV,
        protocol_version="1",
    )
    up, _, _ = J.issue_file_upload_token(
        user_id="u", sha256="x", byte_size=1, mime_type=None,
        secret=_SECRET, ttl_s=60, key_version=_KV,
    )
    fe, _, _ = J.issue_file_fetch_token(
        file_id="f", user_id="u", secret=_SECRET, ttl_s=60, key_version=_KV
    )
    for bad in (sess, agent, up):
        with pytest.raises(J.TokenError):
            J.verify_file_fetch_token(bad, secret=_SECRET, key_version=_KV)
    for bad in (sess, agent, fe):
        with pytest.raises(J.TokenError):
            J.verify_file_upload_token(bad, secret=_SECRET, key_version=_KV)


def test_expired_tampered_and_stale_kver_rejected() -> None:
    tok, _, _ = J.issue_file_fetch_token(
        file_id="f", user_id="u", secret=_SECRET, ttl_s=-1, key_version=_KV
    )
    with pytest.raises(J.TokenError):  # expired
        J.verify_file_fetch_token(tok, secret=_SECRET, key_version=_KV)

    good, _, _ = J.issue_file_fetch_token(
        file_id="f", user_id="u", secret=_SECRET, ttl_s=60, key_version=_KV
    )
    with pytest.raises(J.TokenError):  # tampered
        J.verify_file_fetch_token(good[:-2] + "xy", secret=_SECRET, key_version=_KV)
    with pytest.raises(J.TokenError):  # wrong signing secret
        J.verify_file_fetch_token(good, secret="z" * 32, key_version=_KV)
    with pytest.raises(J.TokenError):  # stale key version
        J.verify_file_fetch_token(good, secret=_SECRET, key_version=_KV + 1)


def test_file_upload_token_missing_bound_claims_rejected() -> None:
    """A correctly-signed `file-upload` token whose `sha`/`sz` are
    absent/malformed must fail closed (not pass with a bogus
    grant)."""
    now = int(time.time())
    base = {
        "iss": J.ISSUER, "sub": "u", "iat": now, "exp": now + 60,
        "kind": "file-upload", "kver": _KV, "jti": "j1",
    }
    no_sha = pyjwt.encode(base, _SECRET, algorithm="HS256")
    bad_sz = pyjwt.encode(
        {**base, "sha": "abc", "sz": "not-an-int"}, _SECRET, algorithm="HS256"
    )
    for bad in (no_sha, bad_sz):
        with pytest.raises(J.TokenError):
            J.verify_file_upload_token(bad, secret=_SECRET, key_version=_KV)


# ---------------------------------------------------------------------------
# file_read_access dual-auth dependency
# ---------------------------------------------------------------------------


def _req(token: str | None) -> MagicMock:
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {token}"} if token else {}
    state = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = _SECRET
    state.settings.jwt_key_version = _KV
    state.settings.jwt_algorithm = "HS256"
    req.app.state.bp = state
    return req


def test_file_read_access_accepts_fetch_key() -> None:
    tok, _, _ = J.issue_file_fetch_token(
        file_id="file_q", user_id="usr_z", secret=_SECRET,
        ttl_s=60, key_version=_KV,
    )
    access = asyncio.run(J.file_read_access(_req(tok)))
    assert access.user_id == "usr_z"
    assert access.via_key_file_id == "file_q"


def test_file_read_access_missing_bearer_401() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(J.file_read_access(_req(None)))
    assert ei.value.status_code == 401


def test_file_read_access_falls_back_to_session_principal(monkeypatch) -> None:
    """A non-file-fetch token must NOT be silently rejected — the
    dep must delegate to the EXISTING session resolution so
    UI/admin (revocation + soft-delete) is byte-for-byte
    unchanged."""
    sentinel = J.SessionPrincipal(
        user_id="usr_sess", level="admin",
        expires_at=J.datetime.now(J.UTC), jti="js",
    )
    called = {}

    async def _fake_principal(request):  # type: ignore[no-untyped-def]
        called["yes"] = True
        return sentinel

    monkeypatch.setattr(J, "_principal_from_request", _fake_principal)
    sess, _, _ = J.issue_session_token(
        user_id="usr_sess", level="admin", secret=_SECRET,
        ttl_s=60, key_version=_KV,
    )
    access = asyncio.run(J.file_read_access(_req(sess)))
    assert called.get("yes") is True
    assert access.user_id == "usr_sess"
    assert access.via_key_file_id is None  # session, not a key


# ---------------------------------------------------------------------------
# download: a key is bound to ONE file_id
# ---------------------------------------------------------------------------


def test_download_rejects_key_minted_for_a_different_file() -> None:
    """A `file-fetch` key for file A hitting `/v1/files/B` must
    403 BEFORE any DB/storage access (the cross-file IDOR guard)."""
    from fastapi import HTTPException

    from bp_router.api import files

    req = MagicMock()
    access = J.FileReadAccess(user_id="u", via_key_file_id="file_A")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.download(file_id="file_B", request=req, access=access))
    assert ei.value.status_code == 403


def test_download_keyed_fetch_serves_cross_user_file(monkeypatch) -> None:
    """Positive keyed path (the #219 carryover). A verified key
    whose bound file_id matches the request resolves the row via the
    UNSCOPED get_file_by_id, so a file owned by ANOTHER user is
    served — the capability is the sole authorization, not 404'd by
    a user-scoped lookup. The user-scoped path is stubbed to None so
    this fails if the handler ever regresses off get_file_by_id."""
    from fastapi.responses import StreamingResponse

    from bp_router.api import files

    # Capability holder is NOT the file's owner.
    access = J.FileReadAccess(user_id="caller_user", via_key_file_id="file_X")
    row = MagicMock(
        sha256="ab" * 32, byte_size=10,
        mime_type="application/pdf", original_filename="r.pdf",
    )
    unscoped = AsyncMock(return_value=row)
    scoped_get = AsyncMock(return_value=None)  # cross-user → would 404
    monkeypatch.setattr(files.queries, "get_file_by_id", unscoped)
    monkeypatch.setattr(
        files.queries.Scope, "user",
        staticmethod(lambda conn, uid: MagicMock(get_file=scoped_get)),
    )
    monkeypatch.setattr(
        files.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    conn = MagicMock()
    state = MagicMock()
    state.db_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    state.db_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    # A keyed (agent) fetch must STREAM, never 302 to a presigned URL — the
    # agent reaches only the router on the internal net, not the object store.
    async def _stream():
        yield b"data"
    state.file_store.open = AsyncMock(return_value=_stream())
    state.file_store.presigned_url = AsyncMock(
        return_value="https://signed.example/obj"
    )
    req = MagicMock()
    req.app.state.bp = state

    resp = asyncio.run(
        files.download(file_id="file_X", request=req, access=access)
    )
    assert isinstance(resp, StreamingResponse)
    # Streamed, not redirected — presigned_url not consulted for an agent fetch.
    state.file_store.presigned_url.assert_not_awaited()
    state.file_store.open.assert_awaited_once()
    # Resolved via the UNSCOPED lookup; the user-scoped path was
    # never taken (so the cross-user file is not 404'd).
    unscoped.assert_awaited_once_with(conn, "file_X")
    scoped_get.assert_not_awaited()


def test_download_presigned_pins_attachment_and_safe_mime(monkeypatch) -> None:
    """L3: the presigned-redirect path must carry the SAME
    download-hardening as the streamed path. A stored `text/html`
    must redirect with a downgraded content-type and an `attachment`
    disposition so the S3-direct fetch can't render inline (stored
    XSS) — the bug was the presigned URL omitting both."""
    from fastapi.responses import RedirectResponse

    from bp_router.api import files

    # SESSION principal (no via_key_file_id): a browser/UI client that CAN
    # reach the object store, so the presigned-redirect path applies. (Agent
    # keyed fetches stream instead — see the cross-user test above.)
    access = J.FileReadAccess(user_id="u", via_key_file_id=None)
    row = MagicMock(
        sha256="ab" * 32, byte_size=9,
        mime_type="text/html; charset=utf-8",
        original_filename="evil.html",
    )
    scoped_get = AsyncMock(return_value=row)
    monkeypatch.setattr(
        files.queries.Scope, "user",
        staticmethod(lambda conn, uid: MagicMock(get_file=scoped_get)),
    )
    monkeypatch.setattr(
        files.queries, "append_audit_event", AsyncMock(return_value=None)
    )

    conn = MagicMock()
    state = MagicMock()
    state.db_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    state.db_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    presigned = AsyncMock(return_value="https://s3.example/obj?sig=1")
    state.file_store.presigned_url = presigned
    req = MagicMock()
    req.app.state.bp = state

    resp = asyncio.run(
        files.download(file_id="file_h", request=req, access=access)
    )
    assert isinstance(resp, RedirectResponse)
    kw = presigned.await_args.kwargs
    # text/html is NOT on the response allowlist → downgraded.
    assert kw["content_type"] == "application/octet-stream"
    # Forced attachment (+ sanitised filename) so the browser
    # downloads rather than renders.
    assert kw["content_disposition"].startswith("attachment;")
    assert 'filename="evil.html"' in kw["content_disposition"]


# ---------------------------------------------------------------------------
# upload_with_grant
# ---------------------------------------------------------------------------


def _upload_req() -> MagicMock:
    req = MagicMock()
    state = MagicMock()
    state.settings.jwt_secret.get_secret_value.return_value = _SECRET
    state.settings.jwt_key_version = _KV
    state.settings.jwt_algorithm = "HS256"
    state.settings.max_upload_bytes = 10_000_000
    state.settings.file_default_ttl_s = 3600
    state.settings.file_fetch_token_ttl_s = 60
    req.app.state.bp = state
    return req, state


def _upload_file() -> MagicMock:
    f = MagicMock()
    f.filename = "data.bin"
    f.seek = AsyncMock()
    f.read = AsyncMock(side_effect=[b"", b""])
    return f


def test_upload_with_grant_rejects_missing_and_invalid_token() -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    req, _ = _upload_req()
    req.headers = {}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.upload_with_grant(file=_upload_file(), request=req))
    assert ei.value.status_code == 401

    req2, _ = _upload_req()
    req2.headers = {"authorization": "Bearer not.a.jwt"}
    with pytest.raises(HTTPException) as ei2:
        asyncio.run(files.upload_with_grant(file=_upload_file(), request=req2))
    assert ei2.value.status_code == 401


def test_upload_with_grant_refuses_content_not_matching_grant(monkeypatch) -> None:
    """The grant authorised exactly (sha, size). A body that hashes
    differently must 400 and NOT be persisted — a leaked grant
    can't be repurposed for other bytes."""
    from fastapi import HTTPException

    from bp_router.api import files

    req, state = _upload_req()
    tok, _, _ = J.issue_file_upload_token(
        user_id="u", sha256="expected" + "0" * 56, byte_size=100,
        mime_type=None, secret=_SECRET, ttl_s=60, key_version=_KV,
    )
    req.headers = {"authorization": f"Bearer {tok}"}
    # Body hashes to something else.
    monkeypatch.setattr(
        files, "hash_with_size_cap",
        AsyncMock(return_value=("different" + "0" * 55, 100)),
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.upload_with_grant(file=_upload_file(), request=req))
    assert ei.value.status_code == 400
    state.file_store.put.assert_not_called()


def test_upload_with_grant_happy_path_scopes_row_to_grant_user(
    monkeypatch,
) -> None:
    from bp_router.api import files

    req, state = _upload_req()
    sha = "c0ffee" + "0" * 58
    tok, _, _ = J.issue_file_upload_token(
        user_id="usr_owner", sha256=sha, byte_size=2048,
        mime_type="text/csv", secret=_SECRET, ttl_s=60, key_version=_KV,
    )
    req.headers = {"authorization": f"Bearer {tok}"}
    monkeypatch.setattr(
        files, "hash_with_size_cap", AsyncMock(return_value=(sha, 2048))
    )
    state.file_store.put = AsyncMock(return_value="s3://bucket/c0ffee")

    row = MagicMock(file_id="file_new", sha256=sha, byte_size=2048)
    scope = MagicMock()
    scope.insert_file = AsyncMock(return_value=row)
    user_arg = {}

    def _scope_user(conn, uid):  # type: ignore[no-untyped-def]
        user_arg["uid"] = uid
        return scope

    monkeypatch.setattr(files.queries.Scope, "user", staticmethod(_scope_user))
    conn = MagicMock()
    state.db_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    state.db_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    out = asyncio.run(files.upload_with_grant(file=_upload_file(), request=req))
    assert out["file_id"] == "file_new"
    assert out["sha256"] == sha
    assert out["byte_size"] == "2048"
    assert out["path"] == "/v1/files/file_new"
    assert out["protocol"] == "router-proxy"
    # The returned ref is self-authorising: a file-fetch capability
    # key bound to (file_id, the grant's user) — exactly what the
    # attachment resolver and the keyed download verify.
    g = J.verify_file_fetch_token(out["key"], secret=_SECRET, key_version=_KV)
    assert (g.file_id, g.user_id) == ("file_new", "usr_owner")
    # The row is scoped to the grant's user, NOT any session.
    assert user_arg["uid"] == "usr_owner"
    assert scope.insert_file.await_args.kwargs["session_id"] is None
    assert scope.insert_file.await_args.kwargs["task_id"] is None
