"""Webapp chat pane + SSE progress (Phase 3).

Drives the full flow on one event loop (asyncpg pool is loop-bound) via
httpx.ASGITransport: log in → open the chat view (history) → POST a
message → stream the turn over SSE. A fake dispatcher stands in for the
router, emitting LoopProgress frames then a terminal result, so the
ChannelCore path (route → record → spawn → await(on_progress) →
after_result) runs end-to-end against a real suite DB.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from bp_agents.channel import ChannelCore
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ProgressFrame, ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _FakeUpstream:
    def __init__(self, *, sub: str = "usr_a") -> None:
        self._sub = sub
        self.cancels: list[tuple[str, str]] = []  # (access_token, task_id)

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt(self._sub),
            "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "level": "tier1",
        }

    async def cancel_task(self, *, access_token: str, task_id: str) -> None:
        self.cancels.append((access_token, task_id))

    async def aclose(self) -> None:
        pass


class _ChatDispatcher:
    """Emits the given LoopProgress frames via on_progress, then a result."""

    def __init__(self, *, content: str, progress: list[dict],
                 agent_id: str = "orchestrator", files: list[str] | None = None,
                 status: TaskStatus = TaskStatus.SUCCEEDED) -> None:
        self.content = content
        self.progress = progress
        self.agent_id = agent_id
        self.files = files or []
        self.status = status
        self.spawns: list[tuple[str, str | None]] = []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        self.spawns.append((dest, mode))
        return "tsk"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):
        if on_progress is not None:
            for lp in self.progress:
                await on_progress(ProgressFrame(
                    agent_id=self.agent_id, trace_id="0" * 32, span_id="0" * 16,
                    task_id=task_id, event=lp["kind"], metadata={LOOP_PROGRESS_KEY: lp},
                ))
        # A cancelled task carries no output (mirrors a router-side /stop).
        output = (
            None if self.status is TaskStatus.CANCELLED
            else AgentOutput(content=self.content, files=self.files)
        )
        return ResultFrame(
            agent_id=self.agent_id, trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=self.status, status_code=200, output=output,
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
        # A visible exchange + a hidden internal row (must NOT render).
        await queries.append_history(
            conn, session_id="ses_1", agent_id="orchestrator", role="user",
            message="earlier question",
        )
        await queries.append_history(
            conn, session_id="ses_1", agent_id="orchestrator", role="assistant",
            message="earlier answer",
        )
        await queries.append_history(
            conn, session_id="ses_1", agent_id="orchestrator", role="user",
            message="SECRET SEED ROW", incumbent=True, hidden=True,
        )
        # A session owned by someone else (ownership guard).
        await queries.create_session_info(
            conn, session_id="ses_other", user_id="usr_b", channel="webapp",
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str = "/chat/ses_1") -> str:
    """The session's CSRF token, scraped from a rendered page's meta tag."""
    page = await client.get(path)
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    return m.group(1) if m else ""


def test_chat_view_renders_visible_history_only(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/chat/ses_1")
            assert r.status_code == 200, r.text[:300]
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "earlier question" in html and "earlier answer" in html
    assert "SECRET SEED ROW" not in html  # hidden rows are not shown


def test_chat_view_404_for_unowned_session(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> int:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/chat/ses_other")
            return r.status_code
        finally:
            await pool.close()

    assert asyncio.run(_drive()) == 404


def test_chat_send_then_stream_progress_and_result(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[str, str, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _ChatDispatcher(
                content="the assistant reply",
                progress=[
                    {"kind": "thinking", "round": 1},
                    {"kind": "tool_call", "tool": "call_knowledge_base", "round": 1},
                ],
                files=["report.md"],
            )
            core = ChannelCore(dispatcher=disp, pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                send = await client.post(
                    "/chat/ses_1", data={"message": "do the thing"},
                    headers={"X-CSRF-Token": token},
                )
                assert send.status_code == 200, send.text[:300]
                # The pending bubble subscribes to the session's in-flight turn.
                assert 'sse-connect="/chat/ses_1/stream"' in send.text
                stream = await client.get("/chat/ses_1/stream")
                stream_text = stream.text
            # The user turn was recorded by the channel.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            return send.text, stream_text, [r.message for r in rows]
        finally:
            await pool.close()

    send_html, stream_text, messages = asyncio.run(_drive())
    # POST returns the user bubble + the SSE-connected pending bubble.
    assert "do the thing" in send_html
    assert "sse-connect=" in send_html
    # SSE: two progress events (rendered), then the result, then close.
    assert stream_text.count("event: progress") == 2
    assert "event: result" in stream_text
    assert "event: done" in stream_text
    assert "Thinking" in stream_text
    assert "knowledge_base" in stream_text  # call_ prefix stripped by renderer
    assert "the assistant reply" in stream_text
    assert "report.md" in stream_text  # produced file → download chip
    # The channel recorded the user turn verbatim.
    assert "do the thing" in messages


def test_chat_stream_no_active_turn_closes(suite_db_url: str) -> None:
    """Subscribing with nothing in flight (e.g. the turn already finished)
    closes the stream at once — the answer is in history on the page."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/chat/ses_1/stream")
            return r.status_code, r.text
        finally:
            await pool.close()

    status, text = asyncio.run(_drive())
    assert status == 200
    assert "event: done" in text and "event: progress" not in text


def test_chat_stream_unowned_session_is_404(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> int:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/chat/ses_other/stream")
            return r.status_code
        finally:
            await pool.close()

    assert asyncio.run(_drive()) == 404


def test_chat_stop_cancels_active_turn(suite_db_url: str) -> None:
    """POST /stop cancels the session's in-flight router task (the Stop button,
    parity with the chatbot /stop command)."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            up = _FakeUpstream()
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=up, pool=pool, core=core)
            # Simulate a turn in flight for this session (a runner with its
            # router task spawned but not yet done).
            app.state.active_turns["ses_1"] = SimpleNamespace(
                task_id="tsk_live", done=asyncio.Event()
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                r = await client.post(
                    "/chat/ses_1/stop", headers={"X-CSRF-Token": token}
                )
            return r.status_code, up.cancels
        finally:
            await pool.close()

    status, cancels = asyncio.run(_drive())
    assert status == 204
    assert cancels and cancels[0][1] == "tsk_live"


def test_chat_stop_unowned_session_is_404(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, list]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            up = _FakeUpstream()
            core = ChannelCore(dispatcher=_ChatDispatcher(content="x", progress=[]), pool=pool)
            app = _build_app(upstream=up, pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                r = await client.post(
                    "/chat/ses_other/stop", headers={"X-CSRF-Token": token}
                )
            return r.status_code, up.cancels
        finally:
            await pool.close()

    status, cancels = asyncio.run(_drive())
    assert status == 404
    assert cancels == []  # never reached the cancel call


def test_chat_stream_cancelled_renders_stopped(suite_db_url: str) -> None:
    """A turn that comes back CANCELLED (router-side stop) renders a clean
    'Stopped' bubble rather than the generic error."""
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _ChatDispatcher(
                content="ignored", progress=[], status=TaskStatus.CANCELLED
            )
            core = ChannelCore(dispatcher=disp, pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                await client.post(
                    "/chat/ses_1", data={"message": "go"},
                    headers={"X-CSRF-Token": token},
                )
                stream = await client.get("/chat/ses_1/stream")
                return stream.text
        finally:
            await pool.close()

    text = asyncio.run(_drive())
    assert "event: result" in text and "Stopped" in text
    assert "something went wrong" not in text


class _BlockingDispatcher:
    """Holds the turn open until released — lets a test observe an in-flight
    turn (the runner is mid-await) before it completes."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return "tsk_block"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):
        self.started.set()
        await self.release.wait()
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="late answer", files=[]),
        )


def test_chat_view_resumes_in_flight_bubble(suite_db_url: str) -> None:
    """Navigating back to the chat mid-turn re-renders the pending bubble (Stop
    button + reconnecting SSE) — the turn survives the dropped connection."""
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _BlockingDispatcher()
            core = ChannelCore(dispatcher=disp, pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                # Start a turn; it blocks mid-flight (detached from any stream).
                await client.post(
                    "/chat/ses_1", data={"message": "go"},
                    headers={"X-CSRF-Token": token},
                )
                await asyncio.wait_for(disp.started.wait(), timeout=5)
                # "Navigate back": a fresh page load while the turn runs.
                view = (await client.get("/chat/ses_1")).text
                # Let the turn finish and drain its task before teardown.
                disp.release.set()
                runner = app.state.active_turns["ses_1"]
                await asyncio.wait_for(runner.task, timeout=5)
            return view
        finally:
            await pool.close()

    view = asyncio.run(_drive())
    # The reloaded view re-attaches the live bubble: a reconnecting SSE + Stop.
    assert 'sse-connect="/chat/ses_1/stream"' in view
    assert 'hx-post="/chat/ses_1/stop"' in view  # the Stop button is back
    assert "go" in view  # the user message was recorded under the lock


def test_chat_stream_resume_skips_seen_events(suite_db_url: str) -> None:
    """An EventSource reconnect (carrying Last-Event-ID) must replay only
    UNSEEN events — otherwise the activity strip duplicates on every reconnect."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[str, str]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _ChatDispatcher(
                content="the reply",
                progress=[
                    {"kind": "thinking", "round": 1},
                    {"kind": "tool_call", "tool": "call_x", "round": 1},
                ],
            )
            core = ChannelCore(dispatcher=disp, pool=pool)
            app = _build_app(upstream=_FakeUpstream(), pool=pool, core=core)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client)
                await client.post(
                    "/chat/ses_1", data={"message": "go"},
                    headers={"X-CSRF-Token": token},
                )
                first = (await client.get("/chat/ses_1/stream")).text
                # Reconnect after having seen event id 2 (the two progress rows).
                resumed = (await client.get(
                    "/chat/ses_1/stream", headers={"Last-Event-ID": "2"}
                )).text
            return first, resumed
        finally:
            await pool.close()

    first, resumed = asyncio.run(_drive())
    # Events are id-tagged so the browser can resume.
    assert "id: 1" in first and "id: 3" in first
    assert first.count("event: progress") == 2
    # The reconnect skips the two seen progress rows but still gets the result.
    assert resumed.count("event: progress") == 0
    assert "event: result" in resumed
