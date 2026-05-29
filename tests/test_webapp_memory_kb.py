"""Webapp Memory + Knowledge base pages.

Both dispatch to their agent via `core.call_agent` and render the JSON
`AgentOutput`. Driven with a fake core (records calls, returns canned JSON)
and a fake upstream (supplies an open carrier session) over httpx
ASGITransport against a live suite DB.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import httpx
import pytest

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.types import AgentOutput


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


_OPEN = [{"session_id": "ses_1", "opened_at": "2026-05-01T00:00:00Z", "closed_at": None}]


class _Upstream:
    def __init__(self, *, sub: str = "usr_a", sessions: list[dict] | None = None) -> None:
        self._sub = sub
        self._sessions = _OPEN if sessions is None else sessions

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_sessions(self, *, access_token):
        return self._sessions

    async def aclose(self):
        pass


class _FakeCore:
    """Records call_agent dispatches; returns canned JSON per (dest, mode)."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    async def call_agent(self, *, user_id, session_id, dest, mode, payload):
        self.calls.append(
            {"user_id": user_id, "session_id": session_id, "dest": dest,
             "mode": mode, "payload": payload}
        )
        data = self._responses.get((dest, mode), {})
        return type("R", (), {"output": AgentOutput(content=json.dumps(data))})()


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
            "TRUNCATE TABLE session_history, cron_jobs, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await queries.create_user_config(
            conn, user_id="usr_a", default_session_id="ses_1"
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="webapp"
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str = "/") -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', (await client.get(path)).text)
    return m.group(1) if m else ""


def _run(core, *, db_url, upstream=None, fn=None):
    async def _drive():
        pool = await open_pool(SuiteSettings(database_url=db_url))
        try:
            await _seed(pool)
            app = _build_app(upstream=upstream or _Upstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                return await fn(client)
        finally:
            await pool.close()

    return asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Memory page
# ---------------------------------------------------------------------------


def test_memory_page_renders_facts_and_dispatches_list(suite_db_url: str) -> None:
    core = _FakeCore({("memory", "list"): {
        "items": [{"uid": "u1", "fact": "likes tea", "kind": "preference",
                   "created_at": "", "last_used_at": "2026-05-01T00:00:00Z"}],
        "total": 1,
    }})

    async def _fn(client):
        return (await client.get("/memory")).text

    html = _run(core, db_url=suite_db_url, fn=_fn)
    assert "likes tea" in html
    call = core.calls[0]
    assert call["dest"] == "memory" and call["mode"] == "list"
    assert call["session_id"] == "ses_1"  # rode the carrier (default) session


def test_memory_add_dispatches_manual_add(suite_db_url: str) -> None:
    core = _FakeCore({("memory", "manual_add"): {"added": True}})

    async def _fn(client):
        token = await _csrf(client, "/memory")
        return await client.post(
            "/memory/add", data={"fact": "allergic to peanuts", "kind": "personal_info"},
            headers={"X-CSRF-Token": token},
        )

    r = _run(core, db_url=suite_db_url, fn=_fn)
    assert r.status_code == 204 and r.headers.get("HX-Trigger") == "memoryChanged"
    call = next(c for c in core.calls if c["mode"] == "manual_add")
    assert call["payload"].fact == "allergic to peanuts"
    assert call["payload"].kind == "personal_info"


def test_memory_delete_dispatches_delete(suite_db_url: str) -> None:
    core = _FakeCore({("memory", "delete"): {"deleted": True}})

    async def _fn(client):
        token = await _csrf(client, "/memory")
        return await client.post(
            "/memory/delete", data={"uid": "u1"}, headers={"X-CSRF-Token": token},
        )

    r = _run(core, db_url=suite_db_url, fn=_fn)
    assert r.status_code == 204 and r.headers.get("HX-Trigger") == "memoryChanged"
    assert any(c["mode"] == "delete" and c["payload"].uid == "u1" for c in core.calls)


def test_memory_page_empty_state_without_open_session(suite_db_url: str) -> None:
    core = _FakeCore({})

    async def _fn(client):
        return (await client.get("/memory")).text

    html = _run(core, db_url=suite_db_url, upstream=_Upstream(sessions=[]), fn=_fn)
    assert "Start a conversation" in html
    assert core.calls == []  # no carrier → no dispatch


# ---------------------------------------------------------------------------
# Knowledge base page
# ---------------------------------------------------------------------------


def test_knowledge_page_renders_docs_and_dispatches_browse(suite_db_url: str) -> None:
    core = _FakeCore({("knowledge_base", "browse"): {
        "items": [{"doc_id": "d1", "title": "Tax notes", "collection": "finance",
                   "tags": ["2025"], "description": "", "created_at": "",
                   "updated_at": "2026-05-01T00:00:00Z"}],
        "total": 1,
    }})

    async def _fn(client):
        return (await client.get("/knowledge")).text

    html = _run(core, db_url=suite_db_url, fn=_fn)
    assert "Tax notes" in html and "finance" in html
    assert core.calls[0]["dest"] == "knowledge_base"
    assert core.calls[0]["mode"] == "browse"


def test_knowledge_delete_dispatches_delete(suite_db_url: str) -> None:
    core = _FakeCore({("knowledge_base", "delete"): {"deleted": 1}})

    async def _fn(client):
        token = await _csrf(client, "/knowledge")
        return await client.post(
            "/knowledge/delete", data={"title": "Tax notes", "collection": "finance"},
            headers={"X-CSRF-Token": token},
        )

    r = _run(core, db_url=suite_db_url, fn=_fn)
    assert r.status_code == 204 and r.headers.get("HX-Trigger") == "knowledgeChanged"
    call = next(c for c in core.calls if c["mode"] == "delete")
    assert call["payload"].title == "Tax notes" and call["payload"].collection == "finance"
