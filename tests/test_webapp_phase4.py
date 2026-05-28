"""Webapp Phase 4 — delegation control + file stash pane.

Delegation: the dropdown/return-button POSTs drive ChannelCore.delegate /
undelegate (same deterministic path as the Telegram /delegate). File
stash: list/upload/download over the router name store with the user's
token. Driven on one loop via httpx.ASGITransport (asyncpg is loop-bound).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import httpx
import pytest

from bp_agents.channel import ChannelCore
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream:
    """Fake router client: login + the file-stash surface."""

    def __init__(self, *, sub: str = "usr_a") -> None:
        self._sub = sub
        self.uploaded: list[tuple[str, bool, str | None, int]] = []
        self.session_names = ["report.md", "data.csv"]
        self.persist_names = ["persist/notes.txt"]

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_names(self, *, access_token, session_id=None, persistent=False):
        return self.persist_names if persistent else self.session_names

    async def upload_file(self, *, access_token, filename, data, session_id=None,
                          persistent=False, mime_type=None):
        self.uploaded.append((filename, persistent, session_id, len(data)))
        return f"persist/{filename}" if persistent else filename

    async def resolve_named_file(self, *, access_token, session_id, name):
        return "file_abc"

    async def fetch_file(self, *, access_token, file_id):
        return b"FILE-BYTES"

    async def aclose(self):
        pass


class _SummDispatcher:
    """Any spawn → a result whose content is the 'summary' (drives the
    summarizer hop inside ChannelCore.delegate / undelegate)."""

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return "tsk"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):
        return ResultFrame(
            agent_id="history_summarizer", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="summarized"),
        )


def _build_app(*, upstream, pool, core):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    cfg = WebappConfig(session_secret=SecretStr("x" * 32), session_cookie_secure=False)
    return create_app(cfg, upstream=upstream, pool=pool, core=core)


async def _seed(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="webapp",
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str) -> str:
    page = await client.get(path)
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    return m.group(1) if m else ""


def _core(pool):
    return ChannelCore(
        dispatcher=_SummDispatcher(), pool=pool,
        delegatable_agents=frozenset({"research", "computer_use"}),
    )


# ---------------------------------------------------------------------------
# Delegation control
# ---------------------------------------------------------------------------


def test_chat_view_shows_delegation_picker_then_return_button(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[str, str]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                not_delegated = (await client.get("/chat/ses_1")).text
                async with pool.acquire() as conn:
                    await queries.update_session_info(conn, "ses_1", delegated_to="research")
                delegated = (await client.get("/chat/ses_1")).text
            return not_delegated, delegated
        finally:
            await pool.close()

    not_delegated, delegated = asyncio.run(_drive())
    # Picker present (a delegatable option), no return button, when not delegated.
    assert 'name="agent"' in not_delegated
    assert "Computer Use" in not_delegated  # option label, prettified
    assert "Return to assistant" not in not_delegated
    # When delegated: status badge + return button, no picker.
    assert "Talking to: Research Agent" in delegated
    assert "Return to assistant" in delegated


def test_delegate_endpoint_sets_delegated_to(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str | None, str | None]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/chat/ses_1")
                r = await client.post(
                    "/chat/ses_1/delegate", data={"agent": "research"},
                    headers={"X-CSRF-Token": token},
                )
                async with pool.acquire() as conn:
                    info = await queries.get_session_info(conn, "ses_1")
            return r.status_code, r.headers.get("HX-Redirect"), info.delegated_to
        finally:
            await pool.close()

    status, redirect, delegated_to = asyncio.run(_drive())
    assert status == 204
    assert redirect == "/chat/ses_1"
    assert delegated_to == "research"


def test_undelegate_endpoint_clears_delegated_to(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str | None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.update_session_info(conn, "ses_1", delegated_to="research")
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/chat/ses_1")
                await client.post(
                    "/chat/ses_1/undelegate", headers={"X-CSRF-Token": token},
                )
                async with pool.acquire() as conn:
                    info = await queries.get_session_info(conn, "ses_1")
            return info.delegated_to
        finally:
            await pool.close()

    assert asyncio.run(_drive()) is None


# ---------------------------------------------------------------------------
# File stash pane
# ---------------------------------------------------------------------------


def test_stash_view_lists_both_scopes(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/files/ses_1")
            assert r.status_code == 200, r.text[:300]
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "report.md" in html and "data.csv" in html  # session scope
    assert "persist/notes.txt" in html  # persistent scope
    assert 'href="/files/ses_1/report.md"' in html  # download link


def test_stash_upload_posts_to_upstream(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str | None, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            app = _build_app(upstream=upstream, pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/files/ses_1")
                r = await client.post(
                    "/files/ses_1",
                    data={"scope": "persist"},
                    files={"file": ("hello.txt", b"hi there", "text/plain")},
                    headers={"X-CSRF-Token": token},
                )
            return r.status_code, r.headers.get("HX-Redirect"), upstream.uploaded
        finally:
            await pool.close()

    status, redirect, uploaded = asyncio.run(_drive())
    assert status == 204
    assert redirect == "/files/ses_1?tab=persist"
    assert uploaded == [("hello.txt", True, "ses_1", len(b"hi there"))]


def test_stash_download_streams_bytes(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, bytes, str]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/files/ses_1/report.md")
            return r.status_code, r.content, r.headers.get("content-disposition", "")
        finally:
            await pool.close()

    status, content, disposition = asyncio.run(_drive())
    assert status == 200
    assert content == b"FILE-BYTES"
    assert "report.md" in disposition


def test_stash_404_for_unowned_session(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> int:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=_Upstream(), pool=pool, core=_core(pool))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/files/ses_nope")
            return r.status_code
        finally:
            await pool.close()

    assert asyncio.run(_drive()) == 404
