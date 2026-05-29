"""Session-authed named-store HTTP endpoints (`bp_router/api/files.py`).

`POST /v1/files/names`, `GET /v1/files/names`, `GET /v1/files/names/resolve`
give a gateway agent (channel / webapp) — which holds a per-user session
JWT but is never a task's active executor — the same named-store
operations as the WS `FileStore`/`FileFetch`/`FileManage` frames. The
dedup / scope / quota helpers are shared via `bp_router.file_store`, so
these tests focus on the endpoint wiring (scope resolution, ownership,
error mapping) rather than re-testing the allocation policy.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastapi")


class _FakeScope:
    """Stand-in for `queries.Scope.user(...)`. Drives the real
    `_allocate_name` / list / resolve through in-memory dicts."""

    def __init__(
        self,
        *,
        sessions: tuple[str, ...] = (),
        blobs: dict[str, Any] | None = None,
        names: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self._sessions = set(sessions)
        self._blobs = blobs or {}
        self.rows: dict[tuple[str, str], Any] = names or {}

    async def get_session(self, session_id: str):  # type: ignore[no-untyped-def]
        return (
            SimpleNamespace(session_id=session_id)
            if session_id in self._sessions
            else None
        )

    async def get_file_by_sha256(self, sha256: str):  # type: ignore[no-untyped-def]
        return self._blobs.get(sha256)

    async def resolve_file_name(self, scope: str, filename: str):  # type: ignore[no-untyped-def]
        return self.rows.get((scope, filename))

    async def insert_file_name(self, *, scope, filename, file_id, byte_size) -> bool:  # type: ignore[no-untyped-def]
        if (scope, filename) in self.rows:
            return False
        self.rows[(scope, filename)] = SimpleNamespace(
            filename=filename, file_id=file_id, byte_size=byte_size
        )
        return True

    async def repoint_file_name(self, *, scope, filename, file_id, byte_size) -> None:  # type: ignore[no-untyped-def]
        self.rows[(scope, filename)] = SimpleNamespace(
            filename=filename, file_id=file_id, byte_size=byte_size
        )

    async def list_file_names(self, scope, *, query=None, stored_after=None):  # type: ignore[no-untyped-def]
        return [
            r
            for (s, f), r in self.rows.items()
            if s == scope and (query is None or query.lower() in f.lower())
        ]

    async def count_user_storage_bytes(self) -> int:
        return sum(r.byte_size for r in self.rows.values())


class _FakeTx:
    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    fake_scope: _FakeScope,
    *,
    quota_ok: bool = True,
) -> Any:
    """Patch the files module to run against the fake scope + a fake
    pool, and return a `request` stand-in. The real `_allocate_name`
    runs against `fake_scope`; `_quota_ok` and audit are stubbed."""
    from bp_router.api import files

    monkeypatch.setattr(
        files.queries.Scope, "user", lambda conn, uid: fake_scope
    )
    monkeypatch.setattr(
        files.queries, "append_audit_event", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(files, "_quota_ok", AsyncMock(return_value=quota_ok))
    monkeypatch.setattr(
        files, "_mint_fetch_key", lambda settings, fid, uid: f"key-{fid}"
    )

    conn = MagicMock()
    conn.transaction = _FakeTx
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state = MagicMock()
    state.db_pool = pool
    request = MagicMock()
    request.app.state.bp = state
    return request


def _principal(user_id: str = "usr_alice0001") -> Any:
    return SimpleNamespace(user_id=user_id, level="tier0")


def _blob(file_id: str = "fil_a", byte_size: int = 10) -> Any:
    return SimpleNamespace(file_id=file_id, byte_size=byte_size)


# ---------------------------------------------------------------------------
# bind_name
# ---------------------------------------------------------------------------


def test_bind_session_scoped_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    from bp_router.api import files

    scope = _FakeScope(sessions=("s1",), blobs={"sha": _blob()})
    request = _wire(monkeypatch, scope)
    body = files.BindNameRequest(name="chart.png", sha256="sha", session_id="s1")

    out = asyncio.run(files.bind_name(body, request, _principal()))
    assert out == {"saved_name": "chart.png"}
    assert ("session:s1", "chart.png") in scope.rows
    files.queries.append_audit_event.assert_awaited_once()


def test_bind_persist_scope_needs_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router.api import files

    scope = _FakeScope(blobs={"sha": _blob()})  # no sessions
    request = _wire(monkeypatch, scope)
    body = files.BindNameRequest(name="persist/report.pdf", sha256="sha")

    out = asyncio.run(files.bind_name(body, request, _principal()))
    assert out == {"saved_name": "persist/report.pdf"}
    assert ("persist", "report.pdf") in scope.rows


def test_bind_invalid_name_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    request = _wire(monkeypatch, _FakeScope(sessions=("s1",)))
    body = files.BindNameRequest(name="a/b", sha256="sha", session_id="s1")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 400


def test_bind_unowned_session_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    request = _wire(monkeypatch, _FakeScope(sessions=()))  # s1 not owned
    body = files.BindNameRequest(name="x.png", sha256="sha", session_id="s1")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 404


def test_bind_missing_session_id_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    request = _wire(monkeypatch, _FakeScope())
    body = files.BindNameRequest(name="x.png", sha256="sha")  # session scope, no id
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 400


def test_bind_blob_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    request = _wire(monkeypatch, _FakeScope(sessions=("s1",), blobs={}))
    body = files.BindNameRequest(name="x.png", sha256="missing", session_id="s1")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 404


def test_bind_append_count_on_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router.api import files

    # Name already bound to a DIFFERENT blob → append_count picks x_1.png.
    scope = _FakeScope(
        sessions=("s1",),
        blobs={"sha": _blob(file_id="fil_new")},
        names={
            ("session:s1", "x.png"): SimpleNamespace(
                filename="x.png", file_id="fil_old", byte_size=5
            )
        },
    )
    request = _wire(monkeypatch, scope)
    body = files.BindNameRequest(
        name="x.png", sha256="sha", session_id="s1", dedup="append_count"
    )
    out = asyncio.run(files.bind_name(body, request, _principal()))
    assert out == {"saved_name": "x_1.png"}


def test_bind_error_dedup_collision_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    scope = _FakeScope(
        sessions=("s1",),
        blobs={"sha": _blob(file_id="fil_new")},
        names={
            ("session:s1", "x.png"): SimpleNamespace(
                filename="x.png", file_id="fil_old", byte_size=5
            )
        },
    )
    request = _wire(monkeypatch, scope)
    body = files.BindNameRequest(
        name="x.png", sha256="sha", session_id="s1", dedup="error"
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 409
    assert ei.value.detail == "filename_exists"


def test_bind_quota_exceeded_413(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    scope = _FakeScope(sessions=("s1",), blobs={"sha": _blob()})
    request = _wire(monkeypatch, scope, quota_ok=False)
    body = files.BindNameRequest(name="x.png", sha256="sha", session_id="s1")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(files.bind_name(body, request, _principal()))
    assert ei.value.status_code == 413


# ---------------------------------------------------------------------------
# list_names / resolve_name
# ---------------------------------------------------------------------------


def test_list_names_returns_display_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bp_router.api import files

    scope = _FakeScope(
        sessions=("s1",),
        names={
            ("session:s1", "a.png"): SimpleNamespace(
                filename="a.png", file_id="f1", byte_size=1
            ),
            ("persist", "b.png"): SimpleNamespace(
                filename="b.png", file_id="f2", byte_size=1
            ),
        },
    )
    request = _wire(monkeypatch, scope)
    out = asyncio.run(
        files.list_names(request, session_id="s1", persistent=False,
                         query=None, principal=_principal())
    )
    assert out == {"names": ["a.png"]}

    out_persist = asyncio.run(
        files.list_names(request, session_id=None, persistent=True,
                         query=None, principal=_principal())
    )
    assert out_persist == {"names": ["persist/b.png"]}


def test_resolve_name_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    from bp_router.api import files

    scope = _FakeScope(
        sessions=("s1",),
        names={
            ("session:s1", "a.png"): SimpleNamespace(
                filename="a.png", file_id="fil_a", byte_size=42
            )
        },
    )
    request = _wire(monkeypatch, scope)
    out = asyncio.run(
        files.resolve_name(request, name="a.png", session_id="s1",
                           principal=_principal())
    )
    assert out["file_id"] == "fil_a"
    assert out["name"] == "a.png"
    assert out["path"] == "/v1/files/fil_a"
    assert out["key"] == "key-fil_a"


def test_resolve_name_unbound_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from bp_router.api import files

    request = _wire(monkeypatch, _FakeScope(sessions=("s1",)))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            files.resolve_name(request, name="nope.png", session_id="s1",
                               principal=_principal())
        )
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# Route ordering — `/names` must be declared BEFORE `/{file_id}` or the
# path-param route would swallow it (`file_id="names"`).
# ---------------------------------------------------------------------------


def test_names_routes_precede_file_id_route() -> None:
    from bp_router.api import files

    paths = [getattr(r, "path", "") for r in files.router.routes]
    names_idx = paths.index("/names")
    file_id_idx = paths.index("/{file_id}")
    assert names_idx < file_id_idx, (
        "GET /names must be registered before GET /{file_id}, "
        f"else it is shadowed (got order: {paths})"
    )
