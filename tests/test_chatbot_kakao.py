"""chatbot KakaoTalk channel.

PR1 covered the plumbing (gate, CF Queues pull/ack, skeleton loop). PR2
adds the gateway + deadline/next-touch state machine; the tests below
split into:

  * no-DB unit tests — the gate, the client (pull/ack + callback, incl.
    the auth-split), chunking, the registry (fakeredis), and the
    quick-reply poll/stop routing.
  * DB-backed turn tests (`suite_db_url`) — the in-time deliver and the
    overran → park → next-touch deliver paths, mirroring
    test_chatbot_gateway's fakes.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from bp_agents.agents.chatbot import kakao_gateway as kg
from bp_agents.agents.chatbot.agent import _kakao_configured
from bp_agents.agents.chatbot.kakao_client import (
    HttpKakaoClient,
    KakaoJob,
    _file_links_card,
    chunk_for_kakao,
)
from bp_agents.agents.chatbot.kakao_consumer import kakao_consume_loop
from bp_agents.agents.chatbot.kakao_gateway import (
    CHECK_LABEL,
    STOP_LABEL,
    KakaoGateway,
)
from bp_agents.agents.chatbot.kakao_registry import KakaoTaskRegistry
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.types import AgentOutput, TaskStatus


def _settings(**kw) -> SuiteSettings:
    base = dict(
        kakao_cf_account_id="A", kakao_cf_queue_id="Q", kakao_cf_api_token="T"
    )
    base.update(kw)
    return SuiteSettings(_env_file=None, **base)  # type: ignore[call-arg]


def _redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _job(msg_id="m1", **body) -> KakaoJob:
    # `received_at` defaults to now (epoch ms) so the callback budget is fresh;
    # the relay always sets it, and a missing value is treated as stale.
    b = {
        "chat_id": "kc1", "callback_url": "https://cb.kakao/x", "utterance": "",
        "received_at": int(time.time() * 1000),
    }
    b.update(body)
    return KakaoJob(msg_id=msg_id, lease_id="L1", body=b)


async def _set_pending(reg, chat="kc1", user="usr", task="tsk1") -> None:
    """Set up a claimed, in-flight (pending) turn for a chat (test helper)."""
    await reg.try_begin(chat)
    await reg.set_task(chat, user, task)


async def _set_ready(reg, chat, text, images="") -> None:
    """Set up a parked (ready) answer for a chat (test helper)."""
    await reg.try_begin(chat)
    await reg.store_ready_unless_stopped(chat, text, images)


class _RecordingClient:
    """Captures post_callback so the gateway's delivery can be asserted.
    posts are (callback_url, text, quick_replies, images, files)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, object, object, object]] = []
        self.inbound_bytes: bytes = b""

    async def pull(self, *, batch_size, visibility_timeout_s):
        return []

    async def ack(self, lease_ids):
        return None

    async def post_callback(
        self, callback_url, text, *, quick_replies=None, images=None, files=None
    ):
        self.posts.append((callback_url, text, quick_replies, images, files))

    async def fetch_inbound_image(self, url):
        return self.inbound_bytes

    async def aclose(self) -> None:
        return None


# --- activation gate / settings ----------------------------------------


def test_kakao_configured_requires_all_three() -> None:
    assert _kakao_configured(SuiteSettings(_env_file=None)) is False  # type: ignore[call-arg]
    assert _kakao_configured(_settings(kakao_cf_api_token=None)) is False
    assert _kakao_configured(_settings()) is True


def test_api_token_is_secret() -> None:
    s = _settings()
    assert s.kakao_cf_api_token is not None
    assert "T" not in repr(s.kakao_cf_api_token)
    assert s.kakao_cf_api_token.get_secret_value() == "T"


# --- CF Queues pull/ack -------------------------------------------------


def test_pull_request_shape_and_parsing() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "messages": [
                        {"id": "m1", "lease_id": "L1", "body": {"utterance": "hi"}}
                    ]
                },
            },
        )

    async def _drive() -> list[KakaoJob]:
        c = HttpKakaoClient(_settings())
        c._queue_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        jobs = await c.pull(batch_size=7, visibility_timeout_s=30)
        await c.aclose()
        return jobs

    jobs = asyncio.run(_drive())
    assert captured["url"].endswith("/accounts/A/queues/Q/messages/pull")
    assert captured["body"] == {"batch_size": 7, "visibility_timeout_ms": 30000}
    assert (jobs[0].msg_id, jobs[0].lease_id, jobs[0].body["utterance"]) == (
        "m1", "L1", "hi"
    )


def test_ack_posts_lease_ids_and_noops_on_empty() -> None:
    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        c._queue_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.ack([])  # no-op — must not hit the network
        await c.ack(["L1", "L2"])
        await c.aclose()

    asyncio.run(_drive())
    assert calls == [{"acks": [{"lease_id": "L1"}, {"lease_id": "L2"}]}]


# --- callback delivery (auth split + shape) ----------------------------


def test_post_callback_shape_and_no_cf_token_leak() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        # Only the callback client is mocked; it must NOT carry the CF bearer.
        c._callback_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.post_callback(
            "https://cb.kakao/x", "hello", quick_replies=[("확인", "확인")]
        )
        await c.aclose()

    asyncio.run(_drive())
    assert captured["url"] == "https://cb.kakao/x"
    assert captured["auth"] is None  # the CF token never reaches kakao.com
    assert captured["body"]["version"] == "2.0"
    tmpl = captured["body"]["template"]
    assert tmpl["outputs"] == [{"simpleText": {"text": "hello"}}]
    assert tmpl["quickReplies"] == [
        {"label": "확인", "action": "message", "messageText": "확인"}
    ]


def test_post_callback_renders_download_links_as_listcard() -> None:
    """`files` render as one tappable listCard of webLink rows — quickReplies
    can't carry a url, so the card is the only download affordance."""
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        c._callback_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.post_callback(
            "https://cb.kakao/x",
            "여기요",
            files=[("전체 답변", "https://r2/x.md"), ("report.pdf", "https://r2/r.pdf")],
        )
        await c.aclose()

    asyncio.run(_drive())
    outs = captured["body"]["template"]["outputs"]
    assert {"simpleText": {"text": "여기요"}} in outs
    cards = [o["listCard"] for o in outs if "listCard" in o]
    assert len(cards) == 1
    assert [it["link"]["web"] for it in cards[0]["items"]] == [
        "https://r2/x.md",
        "https://r2/r.pdf",
    ]
    assert len(outs) <= 3  # within Kakao's per-template output cap


def test_post_callback_prioritises_card_over_surplus_image() -> None:
    """With the output cap full, the download card survives and a surplus image
    is the casualty — an offloaded long answer must never be the one dropped."""
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        c._callback_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.post_callback(
            "https://cb.kakao/x",
            "preview",
            images=[("u1", "a"), ("u2", "b"), ("u3", "c")],
            files=[("전체 답변", "https://r2/x.md")],
        )
        await c.aclose()

    asyncio.run(_drive())
    outs = captured["body"]["template"]["outputs"]
    assert len(outs) == 3  # capped
    assert sum("listCard" in o for o in outs) == 1  # card survived
    assert sum("simpleImage" in o for o in outs) < 3  # a surplus image dropped


def test_file_links_card_caps_items_and_clips_titles() -> None:
    card = _file_links_card([(f"file-{i}.txt", f"https://r2/{i}") for i in range(8)])
    items = card["listCard"]["items"]
    assert len(items) == 5  # Kakao caps a listCard at 5 items
    clipped = _file_links_card([("z" * 80, "https://r2/z")])["listCard"]["items"][0]
    assert len(clipped["title"]) <= 32 and clipped["link"]["web"] == "https://r2/z"


def test_chunk_for_kakao_splits_and_truncates() -> None:
    assert chunk_for_kakao("short", limit=100, max_bubbles=3) == ["short"]
    # 250 chars fit in three ≤100 bubbles with no loss.
    fits = chunk_for_kakao("a" * 250, limit=100, max_bubbles=3)
    assert len(fits) == 3 and all(len(c) <= 100 for c in fits)
    assert not fits[-1].endswith("…(생략됨)")
    # beyond 3×100 the overflow past the last bubble is marked truncated.
    chunks = chunk_for_kakao("a" * 400, limit=100, max_bubbles=3)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)
    assert chunks[-1].endswith("…(생략됨)")


# --- registry (fakeredis) ----------------------------------------------


def test_registry_dedupe_and_park_lifecycle() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        assert await reg.seen("m1") is False  # first time
        assert await reg.seen("m1") is True   # duplicate

        assert await reg.get_turn("c") is None
        assert await reg.try_begin("c") is True   # atomic claim
        assert await reg.try_begin("c") is False  # already claimed (busy)
        await reg.set_task("c", "usr", "tsk1")
        turn = await reg.get_turn("c")
        assert turn == {"state": "pending", "user_id": "usr", "task_id": "tsk1"}

        assert await reg.store_ready_unless_stopped("c", "the answer") is True
        assert (await reg.get_turn("c"))["state"] == "ready"
        assert await reg.take_ready("c") == ("the answer", "", "")
        assert await reg.take_ready("c") is None  # cleared on take
        assert await reg.get_turn("c") is None

    asyncio.run(_drive())


def test_registry_parks_and_returns_images_and_files() -> None:
    """A parked answer round-trips its outbound image + download-link JSON, so a
    deadline-exceeded turn's attachments survive to the next-touch delivery."""
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await reg.try_begin("c")
        images = '[["https://r2/img", "pic"]]'
        files = '[["전체 답변", "https://r2/x.md"]]'
        assert await reg.store_ready_unless_stopped("c", "ans", images, files) is True
        assert await reg.take_ready("c") == ("ans", images, files)

    asyncio.run(_drive())


def test_registry_store_ready_unless_stopped_respects_stop_and_clear() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        # stopped → don't park
        await reg.try_begin("a")
        await reg.mark_stopped("a")
        assert await reg.store_ready_unless_stopped("a", "x") is False
        # cleared (/new) → don't park (turn no longer exists)
        await reg.try_begin("b")
        await reg.clear("b")
        assert await reg.store_ready_unless_stopped("b", "x") is False

    asyncio.run(_drive())

    asyncio.run(_drive())


# --- quick-reply poll / stop routing (no DB) ---------------------------


def _poll_gateway(client, reg, *, credentials=None) -> KakaoGateway:
    # dispatcher/pool are unused on the poll/stop paths.
    return KakaoGateway(
        dispatcher=object(), pool=None, client=client, registry=reg,
        settings=_settings(), credentials=credentials, redis=None,
    )


def test_check_delivers_ready_result() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await _set_ready(reg, "kc1", "parked answer")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/check"))
        assert client.posts == [("https://cb.kakao/x", "parked answer", None, None, None)]
        assert await reg.get_turn("kc1") is None  # cleared

    asyncio.run(_drive())


def test_check_while_pending_reports_still_working() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await _set_pending(reg, "kc1", "usr", "tsk1")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/check"))
        url, text, qr, imgs, files = client.posts[0]
        assert text == kg._STILL_WORKING_TEXT
        assert qr == [(CHECK_LABEL, "/check"), (STOP_LABEL, "/stop")]

    asyncio.run(_drive())


def test_check_while_idle_reports_nothing_running() -> None:
    """/check with no active turn → 'nothing running' (routes through the
    command path now, not a separate poll branch)."""
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/check"))
        assert client.posts[0][1] == kg._NOTHING_RUNNING_TEXT

    asyncio.run(_drive())


def test_format_progress_lines() -> None:
    """Each progress kind/tool renders the spec'd Korean status line."""
    f = kg._format_progress
    # call_<agent> peer tool → calling / analysing "<agent> 에이전트".
    assert f({"kind": "tool_call", "tool": "call_research"}, "orchestrator") == \
        "(research 에이전트를 호출하여 처리 중이에요.)"
    assert f({"kind": "tool_result", "tool": "call_research"}, "orchestrator") == \
        "(research 에이전트의 결과 보고를 분석 중이에요.)"
    # Plain tool run by the orchestrator → no agent prefix.
    assert f({"kind": "tool_call", "tool": "web_search"}, "orchestrator") == \
        "(web_search 도구를 이용하여 처리 중이에요.)"
    assert f({"kind": "tool_result", "tool": "web_search"}, "orchestrator") == \
        "(web_search 도구를 사용하고 결과를 분석 중이에요.)"
    # Plain tool run by a specialist → "<agent> 에이전트 - " prefix.
    assert f({"kind": "tool_call", "tool": "web_search"}, "research") == \
        "(research 에이전트 - web_search 도구를 이용하여 처리 중이에요.)"
    assert f({"kind": "tool_result", "tool": "web_search"}, "research") == \
        "(research 에이전트 - web_search 도구를 사용하고 결과를 분석 중이에요.)"
    # Non-tool kinds (and no producer) → bare fallback, still parenthesised.
    assert f({"kind": "thinking"}, "orchestrator") == "(처리 중이에요.)"
    assert f({"kind": "message"}, None) == "(처리 중이에요.)"


def test_check_while_pending_appends_last_progress() -> None:
    """The [확인] status carries the latest recorded tool-progress line."""
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await _set_pending(reg, "kc1", "usr", "tsk1")
        await reg.set_progress("kc1", "(research 에이전트를 호출하여 처리 중이에요.)")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/check"))
        _url, text, _qr, _imgs, _files = client.posts[0]
        assert kg._STILL_WORKING_TEXT in text
        assert text.endswith("(research 에이전트를 호출하여 처리 중이에요.)")

    asyncio.run(_drive())


def test_stop_cancels_pending_turn() -> None:
    class _Creds:
        def __init__(self) -> None:
            self.cancelled: list[tuple[str, str]] = []

        async def cancel_task(self, *, user_id, task_id):
            self.cancelled.append((user_id, task_id))

    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await _set_pending(reg, "kc1", "usr", "tsk1")
        client, creds = _RecordingClient(), _Creds()
        gw = _poll_gateway(client, reg, credentials=creds)
        await gw.handle_job(_job(utterance="/stop"))
        assert creds.cancelled == [("usr", "tsk1")]
        assert client.posts == [("https://cb.kakao/x", kg._STOPPED_TEXT, None, None, None)]
        assert (await reg.get_turn("kc1")).get("stopped") == "1"

    asyncio.run(_drive())


def test_stop_with_nothing_running() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/stop"))
        assert client.posts == [("https://cb.kakao/x", kg._NOTHING_RUNNING_TEXT, None, None, None)]

    asyncio.run(_drive())


def test_duplicate_job_is_dropped() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await _set_ready(reg, "kc1", "parked")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(msg_id="dup", utterance="/check"))
        await gw.handle_job(_job(msg_id="dup", utterance="/check"))  # redelivery
        assert len(client.posts) == 1  # second is deduped away

    asyncio.run(_drive())


# --- consumer acks successes ([kakao-channel.md] §5) -------------------


def test_consumer_acks_only_successes() -> None:
    class _Gateway:
        async def handle_job(self, job):
            if job.msg_id == "boom":
                raise RuntimeError("infra")

    class _Client:
        def __init__(self) -> None:
            self.acked: list[str] = []
            self._sent = False

        async def pull(self, *, batch_size, visibility_timeout_s):
            if self._sent:
                return []
            self._sent = True
            return [
                KakaoJob("ok1", "Lok", {}),
                KakaoJob("boom", "Lboom", {}),
            ]

        async def ack(self, lease_ids):
            self.acked.extend(lease_ids)
            stop.set()

        async def aclose(self) -> None:
            return None

    stop = asyncio.Event()
    client = _Client()
    asyncio.run(
        asyncio.wait_for(
            kakao_consume_loop(_Gateway(), client, _settings(), stop), timeout=5
        )
    )
    assert client.acked == ["Lok"]  # the raising job is left unacked


# --- DB-backed turn lifecycle ------------------------------------------


class _FakeDispatcher:
    def __init__(self, *, reply="the answer", delay=0.0, files=None) -> None:
        self.reply = reply
        self.delay = delay
        self.files = files or []

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return f"tsk:{getattr(payload, 'prompt', None)}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        from bp_protocol.frames import ResultFrame
        if self.delay:
            await asyncio.sleep(self.delay)
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self.reply, files=self.files),
        )


async def _seed(pool, *, chat_id="kc1", user_id="usr_k", session_id="ses_k") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="kakao", chat_id=chat_id, user_id=user_id,
            session_id=session_id,
        )
        await queries.create_user_config(
            conn, user_id=user_id, default_session_id=session_id
        )
        await queries.create_session_info(
            conn, session_id=session_id, user_id=user_id, channel="chatbot_kakao"
        )


def _gateway(pool, client, disp, reg, settings) -> KakaoGateway:
    return KakaoGateway(
        dispatcher=disp, pool=pool, client=client, registry=reg,
        settings=settings, credentials=None, redis=None,
    )


def test_turn_delivers_in_time(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            gw = _gateway(pool, client, _FakeDispatcher(reply="hi back"), reg, _settings())

            await gw.handle_job(_job(utterance="hello?"))

            # delivered on the callback, no buttons, no parked state left
            assert client.posts == [("https://cb.kakao/x", "hi back", None, None, None)]
            assert await reg.get_turn("kc1") is None
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_k", agent_id="orchestrator"
                )
            assert [(r.role, r.message) for r in rows] == [("user", "hello?")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_bare_confirm_word_is_a_normal_message(suite_db_url: str) -> None:
    """Typing the bare button word '확인' is NOT hijacked into a poll — it's a
    normal message that runs a turn. Only '/check' polls (the button sends the
    slash command, not the visible word)."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            gw = _gateway(pool, client, _FakeDispatcher(reply="네"), reg, _settings())

            await gw.handle_job(_job(utterance="확인"))

            # A turn ran and delivered the reply — not the idle poll response.
            assert client.posts == [("https://cb.kakao/x", "네", None, None, None)]
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_k", agent_id="orchestrator"
                )
            assert [(r.role, r.message) for r in rows] == [("user", "확인")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_turn_overruns_then_parks_for_next_touch(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            # deadline below the dispatch delay → the turn overruns the callback
            settings = _settings(kakao_callback_deadline_s=0.05)
            disp = _FakeDispatcher(reply="slow answer", delay=0.3)
            gw = _gateway(pool, client, disp, reg, settings)

            await gw.handle_job(_job(utterance="do something slow"))
            # first callback is the "still working" status + buttons
            url, text, qr, imgs, files = client.posts[0]
            assert text == kg._WORKING_TEXT
            assert qr == [(CHECK_LABEL, "/check"), (STOP_LABEL, "/stop")]

            # let the background turn finish and park its result
            for _ in range(50):
                if (await reg.get_turn("kc1") or {}).get("state") == "ready":
                    break
                await asyncio.sleep(0.02)

            # next touch ([확인]) delivers the parked answer on a fresh callback
            client.posts.clear()
            await gw.handle_job(_job(msg_id="m2", utterance="/check"))
            assert client.posts == [("https://cb.kakao/x", "slow answer", None, None, None)]
            assert await reg.get_turn("kc1") is None
        finally:
            await pool.close()

    asyncio.run(_drive())


class _ProgressDispatcher(_FakeDispatcher):
    """Replays tool-progress frames through `on_progress` before returning."""

    def __init__(self, *, frames, **kw) -> None:
        super().__init__(**kw)
        self._frames = frames

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):
        if on_progress:
            for fr in self._frames:
                await on_progress(fr)
        return await super().await_root_result(task_id, timeout_s=timeout_s)


def test_turn_progress_recorded_and_shown_on_check(suite_db_url: str) -> None:
    """End to end: a turn's tool steps are recorded as it runs and surface in
    the [확인] 'still working' status while it's in flight."""
    from types import SimpleNamespace

    from bp_agents.common.progress import LOOP_PROGRESS_KEY

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            settings = _settings(kakao_callback_deadline_s=0.05)
            frame = SimpleNamespace(
                metadata={LOOP_PROGRESS_KEY: {"kind": "tool_call", "tool": "call_research"}},
                agent_id="orchestrator",
            )
            disp = _ProgressDispatcher(frames=[frame], reply="done", delay=0.3)
            gw = _gateway(pool, client, disp, reg, settings)

            await gw.handle_job(_job(utterance="do something slow"))
            # The 50 s overrun status (the FIRST callback) already carries the
            # progress recorded during the turn.
            assert kg._WORKING_TEXT in client.posts[0][1]
            assert "(research 에이전트를 호출하여 처리 중이에요.)" in client.posts[0][1]
            # Turn overran → parked pending; progress recorded as it ran.
            for _ in range(50):
                if (await reg.get_turn("kc1") or {}).get("progress"):
                    break
                await asyncio.sleep(0.02)
            assert (await reg.get_turn("kc1") or {}).get("progress") == \
                "(research 에이전트를 호출하여 처리 중이에요.)"

            # [확인] while pending → the status carries the progress line.
            client.posts.clear()
            await gw.handle_job(_job(msg_id="m2", utterance="/check"))
            _url, text, _qr, _imgs, _files = client.posts[0]
            assert "(research 에이전트를 호출하여 처리 중이에요.)" in text

            # Let the background turn finish so loop teardown is clean.
            for _ in range(50):
                if (await reg.get_turn("kc1") or {}).get("state") in ("ready", None):
                    break
                await asyncio.sleep(0.02)
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- PR3: images -------------------------------------------------------

from bp_agents.agents.chatbot.kakao_files import (  # noqa: E402
    R2FileEgress,
    detect_image_mime,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _FakeCreds:
    """Records inbound stores; serves a fixed blob for outbound resolves."""

    def __init__(self, *, file_bytes=b"", saved="image.png") -> None:
        self.stored: list[tuple[str, str | None, int]] = []
        self.file_bytes = file_bytes
        self.saved = saved

    async def store_named_file(self, *, user_id, session_id, filename, data, mime_type=None):
        self.stored.append((filename, mime_type, len(data)))
        return self.saved

    async def resolve_named_file(self, *, user_id, session_id, name):
        return f"fid:{name}"

    async def fetch_file(self, *, user_id, file_id):
        return self.file_bytes


class _FakeEgress:
    def __init__(self, url="https://r2.example/signed.png") -> None:
        self.url = url
        # (content_type, key, ttl_s) per upload, so a test can assert the
        # long download TTL is used for links and the short one for images.
        self.puts: list[tuple[str, str, object]] = []

    download_ttl_s = 86_400

    async def put_file(self, data, *, content_type, key, ttl_s=None):
        self.puts.append((content_type, key, ttl_s))
        return self.url


def test_detect_image_mime() -> None:
    assert detect_image_mime(_PNG) == "image/png"
    assert detect_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert detect_image_mime(b"GIF89a....") == "image/gif"
    assert detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPxx") == "image/webp"
    assert detect_image_mime(b"\x00\x00", "x.png") == "image/png"  # ext fallback
    assert detect_image_mime(b"\x00\x00", "x") == "application/octet-stream"


def test_r2_egress_configured_gate() -> None:
    assert R2FileEgress.configured(_settings()) is False
    s = _settings(
        kakao_r2_endpoint_url="https://r2", kakao_r2_bucket="b",
        kakao_r2_access_key_id="k", kakao_r2_secret_access_key="sec",
    )
    assert R2FileEgress.configured(s) is True


def test_r2_egress_put_file_awaits_presigned() -> None:
    s = _settings(
        kakao_r2_endpoint_url="https://r2", kakao_r2_bucket="b",
        kakao_r2_access_key_id="k", kakao_r2_secret_access_key="sec",
        kakao_r2_url_ttl_s=300,
    )
    egress = R2FileEgress(s)
    calls: dict = {}

    class _S3:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put_object(self, **kw):
            calls["put"] = kw

        async def generate_presigned_url(self, **kw):  # must be awaited
            calls["presign"] = kw
            return "https://r2.example/signed"

    egress._client = _S3  # bypass aioboto3/botocore entirely (ctx-mgr factory)

    url = asyncio.run(
        egress.put_file(_PNG, content_type="image/png", key="kakao/s/abc/x.png")
    )
    assert url == "https://r2.example/signed"
    assert calls["put"]["Bucket"] == "b" and calls["put"]["ContentType"] == "image/png"
    assert calls["presign"]["ClientMethod"] == "get_object"
    assert calls["presign"]["ExpiresIn"] == 300


def test_post_callback_with_images_no_token_leak() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        c._callback_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.post_callback(
            "https://cb.kakao/x", "caption",
            images=[("https://r2/a.png", "a.png")],
        )
        await c.aclose()

    asyncio.run(_drive())
    assert captured["auth"] is None
    outs = captured["body"]["template"]["outputs"]
    assert {"simpleText": {"text": "caption"}} in outs
    assert {
        "simpleImage": {"imageUrl": "https://r2/a.png", "altText": "a.png"}
    } in outs


def test_fetch_inbound_image_caps_and_guards_scheme() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG)

    async def _drive() -> bytes:
        c = HttpKakaoClient(_settings())
        c._callback_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        data = await c.fetch_inbound_image("https://8.8.8.8/x.png")  # public IP literal
        with pytest.raises(ValueError):
            await c.fetch_inbound_image("ftp://nope")  # scheme rejected by guard
        await c.aclose()
        return data

    assert asyncio.run(_drive()).startswith(b"\x89PNG")


def test_inbound_image_stored_and_recorded(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            client.inbound_bytes = _PNG
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            creds = _FakeCreds(saved="image.png")
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(reply="nice pic"), pool=pool,
                client=client, registry=reg, settings=_settings(),
                credentials=creds, egress=None, redis=None,
            )
            await gw.handle_job(_job(utterance="", image_url="https://img.kakao/x.png"))

            # stored to the router named store as image/png
            assert creds.stored and creds.stored[0][1] == "image/png"
            # a hidden (T,T) row records the saved name
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, message, hidden FROM session_history "
                    "WHERE session_id='ses_k' ORDER BY id"
                )
            assert any(
                r["hidden"] and "image saved as image.png" in r["message"]
                for r in rows
            )
            # the reply is still delivered on the callback
            assert client.posts[-1][1] == "nice pic"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_outbound_image_uploaded_and_delivered(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            creds = _FakeCreds(file_bytes=_PNG)
            egress = _FakeEgress("https://r2.example/chart.png")
            disp = _FakeDispatcher(reply="here is your chart", files=["chart.png"])
            gw = KakaoGateway(
                dispatcher=disp, pool=pool, client=client, registry=reg,
                settings=_settings(), credentials=creds, egress=egress, redis=None,
            )
            await gw.handle_job(_job(utterance="make a chart"))

            url, text, qr, imgs, files = client.posts[-1]
            assert text == "here is your chart"
            assert imgs == [("https://r2.example/chart.png", "chart.png")]
            assert not files  # an image inlines as a bubble, not a download card
            # inline image → short TTL (Kakao fetches it on receipt)
            assert egress.puts and egress.puts[0][0] == "image/png"
            assert egress.puts[0][2] is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_outbound_nonimage_file_delivered_as_link(suite_db_url: str) -> None:
    """A produced non-image file rides as a tappable download link in a
    listCard (Kakao can't inline documents): no image bubble, long-TTL url,
    and the reply text stays clean."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            creds = _FakeCreds(file_bytes=b"%PDF-1.4 hello")
            egress = _FakeEgress("https://r2.example/report.pdf")
            disp = _FakeDispatcher(reply="여기 보고서예요", files=["report.pdf"])
            gw = KakaoGateway(
                dispatcher=disp, pool=pool, client=client, registry=reg,
                settings=_settings(), credentials=creds, egress=egress, redis=None,
            )
            await gw.handle_job(_job(utterance="보고서 만들어줘"))

            _url, text, _qr, imgs, files = client.posts[-1]
            assert "여기 보고서예요" in text
            assert files == [("report.pdf", "https://r2.example/report.pdf")]
            assert "https://r2.example/report.pdf" not in text  # link is in the card
            assert imgs is None  # a document → download link, not an image bubble
            # uploaded under its detected (non-image) content type, with the long
            # download TTL (a user taps it later, not Kakao's servers on receipt)
            assert egress.puts and egress.puts[0][0] == "application/pdf"
            assert egress.puts[0][2] == egress.download_ttl_s
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_long_answer_offloaded_to_download_link(suite_db_url: str) -> None:
    """An answer past the 3-bubble budget is offloaded to a Markdown download
    (long TTL) surfaced as a '전체 답변' link, with a short preview bubble —
    rather than truncating the tail away."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            egress = _FakeEgress("https://r2.example/answer.md")
            long_answer = "가" * 5000  # >> 3×1000-char budget
            disp = _FakeDispatcher(reply=long_answer)
            gw = KakaoGateway(
                dispatcher=disp, pool=pool, client=client, registry=reg,
                settings=_settings(), credentials=_FakeCreds(), egress=egress,
                redis=None,
            )
            await gw.handle_job(_job(utterance="긴 답 주세요"))

            _url, text, _qr, _imgs, files = client.posts[-1]
            assert files == [(kg._FULL_ANSWER_LABEL, "https://r2.example/answer.md")]
            assert kg._OVERFLOW_NOTICE in text  # preview points at the link
            assert len(text) <= _settings().kakao_msg_char_limit  # one bubble
            # full answer offloaded as Markdown, with the long download TTL
            ct, _key, ttl = egress.puts[-1]
            assert ct.startswith("text/markdown")
            assert ttl == egress.download_ttl_s
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- PR4: remaining commands + registration reconcile ------------------

from datetime import UTC, datetime  # noqa: E402

from bp_agents.agents.chatbot import approval as kapproval  # noqa: E402
from bp_agents.agents.chatbot.credentials import ServicedSession  # noqa: E402


class _CredsForCmds:
    """Minimal credentials for the command tests."""

    def __init__(self, *, token="pw-token-123") -> None:
        self.token = token

    async def mint_password_reset_token(self, *, user_id):
        return self.token


def test_help_and_unknown_command() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance="/help"))
        await gw.handle_job(_job(msg_id="m2", utterance="/wat"))
        assert client.posts[0][1] == kg.HELP_TEXT
        assert client.posts[1][1] == kg._UNKNOWN_CMD_TEXT

    asyncio.run(_drive())


def test_password_command(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(), pool=pool, client=client,
                registry=reg, settings=_settings(),
                credentials=_CredsForCmds(token="tok-xyz"), redis=None,
            )
            await gw.handle_job(_job(utterance="/password"))
            assert "tok-xyz" in client.posts[-1][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


class _LinkCreds:
    """Credentials double for the /link flow; open_session hands the linked
    chat its own session id."""

    def __init__(self, *, user_id: str | None, new_session: str = "ses_link") -> None:
        self._user_id = user_id
        self._new = new_session
        self.verified: list[str] = []
        self.opened: list[str] = []

    async def verify_link_token(self, *, token: str) -> str | None:
        self.verified.append(token)
        return self._user_id

    async def open_session(self, *, user_id, metadata=None) -> str:
        self.opened.append(user_id)
        return self._new


def test_link_binds_unmapped_chat(suite_db_url: str) -> None:
    """`/link <token>` on an unmapped Kakao chat verifies the token, maps the
    chat to the existing account (usr_k), AND opens it its OWN session — so it
    keeps a separate conversation from the account's default (ses_k)."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # usr_k mapped to chat "kc1", default = ses_k
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            creds = _LinkCreds(user_id="usr_k", new_session="ses_link")
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(), pool=pool, client=client,
                registry=reg, settings=_settings(), credentials=creds, redis=None,
            )
            await gw.handle_job(_job(chat_id="kc_new", utterance="/link tok-abc"))

            assert creds.verified == ["tok-abc"]
            assert creds.opened == ["usr_k"]
            assert client.posts[-1][1] == kg._LINK_OK_TEXT
            async with pool.acquire() as conn:
                mapping = await queries.get_platform_mapping(
                    conn, platform="kakao", chat_id="kc_new"
                )
                cfg = await queries.get_user_config(conn, "usr_k")
            assert mapping is not None and mapping.user_id == "usr_k"
            assert mapping.session_id == "ses_link"  # its own session
            assert cfg.default_session_id == "ses_k"  # default untouched
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_setdefault_points_default_at_this_chats_session(suite_db_url: str) -> None:
    """/setdefault moves the cron-fallback default to the Kakao chat's own
    session (kc1 -> ses_k here; default already ses_k, so assert it's set and
    confirmed)."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # kc1 -> ses_k, default ses_k
            # Point the user's default elsewhere so the move is observable.
            async with pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id="ses_other", user_id="usr_k",
                    channel="chatbot_telegram", chat_id="tg9",
                )
                await queries.set_default_session_id(
                    conn, user_id="usr_k", session_id="ses_other"
                )
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(), pool=pool, client=client,
                registry=reg, settings=_settings(), credentials=None, redis=None,
            )
            await gw.handle_job(_job(chat_id="kc1", utterance="/setdefault"))

            assert client.posts[-1][1] == kg._SETDEFAULT_OK_TEXT
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_k")
            assert cfg.default_session_id == "ses_k"  # moved to kc1's session
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_link_invalid_token_does_not_map(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            creds = _LinkCreds(user_id=None)
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(), pool=pool, client=client,
                registry=reg, settings=_settings(), credentials=creds, redis=None,
            )
            await gw.handle_job(_job(chat_id="kc_new", utterance="/link bad"))

            assert client.posts[-1][1] == kg._LINK_INVALID_TEXT
            async with pool.acquire() as conn:
                resolved = await queries.resolve_user_id(
                    conn, platform="kakao", chat_id="kc_new"
                )
            assert resolved is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_config_command_dispatches_to_config_agent(suite_db_url: str) -> None:
    class _ConfigDispatcher(_FakeDispatcher):
        def __init__(self) -> None:
            super().__init__(reply="your settings: ...")
            self.spawns: list[tuple] = []

        async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
            self.spawns.append((dest, mode, getattr(payload, "prompt", None)))
            return await super().spawn_root_for_user(
                dest, payload, user_id=user_id, session_id=session_id, mode=mode
            )

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            disp = _ConfigDispatcher()
            gw = KakaoGateway(
                dispatcher=disp, pool=pool, client=client, registry=reg,
                settings=_settings(), credentials=None, redis=None,
            )
            await gw.handle_job(_job(utterance="/config"))
            assert ("config", "message") == disp.spawns[0][:2]
            assert client.posts[-1][1] == "your settings: ..."
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_delegate_and_reject(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            client = _RecordingClient()
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            gw = KakaoGateway(
                dispatcher=_FakeDispatcher(reply="summary"), pool=pool,
                client=client, registry=reg, settings=_settings(),
                credentials=None, redis=None,
            )
            # unknown target → rejected, no state change
            await gw.handle_job(_job(utterance="/delegate nope"))
            assert "delegate" in client.posts[-1][1].lower()
            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_k")
            assert info.delegated_to is None

            # valid target (research is in the default allow-list)
            await gw.handle_job(_job(msg_id="m2", utterance="/delegate research"))
            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_k")
            assert info.delegated_to == "research"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_kakao_registration_reconcile_maps_kakao_platform(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE session_history, session_info, user_config, "
                    "suite_platform_mappings RESTART IDENTITY"
                )
            rec = ServicedSession(
                user_id="usr_k2", session_id="ses_k2", external_id="kchat2",
                channel="chatbot_kakao", opened_at=datetime.now(UTC),
            )
            n = await kapproval.reconcile_serviced_sessions(
                pool, [rec], settings=_settings(),
                platform="kakao", channel="chatbot_kakao",
                default_language="ko",
            )
            assert n == 1
            async with pool.acquire() as conn:
                uid = await queries.resolve_user_id(
                    conn, platform="kakao", chat_id="kchat2"
                )
                info = await queries.get_session_info(conn, "ses_k2")
                cfg = await queries.get_user_config(conn, "usr_k2")
            assert uid == "usr_k2"
            assert info.channel == "chatbot_kakao"
            # KakaoTalk seeds Korean as the user's default language.
            assert cfg.language == "ko"
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- hardening: SSRF guard, fan-out, delivery-failure park -------------


def test_fetch_inbound_image_blocks_internal_targets() -> None:
    """The SSRF guard rejects loopback / RFC1918 / link-local / metadata and
    non-http schemes; a public IP literal is allowed."""
    async def _drive() -> None:
        c = HttpKakaoClient(_settings())
        c._callback_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_PNG))
        )
        for bad in (
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/x",
            "http://192.168.1.1/x",
            "ftp://8.8.8.8/x",
        ):
            with pytest.raises(ValueError):
                await c.fetch_inbound_image(bad)
        # public IP literal → guard passes, mock returns the bytes
        assert await c.fetch_inbound_image("http://8.8.8.8/x.png") == _PNG
        await c.aclose()

    asyncio.run(_drive())


def test_consumer_fans_out_slow_does_not_block_fast() -> None:
    """A slow turn (first in the batch) must not delay a fast turn's ack —
    the fan-out makes the consumer head-of-line free."""
    order: list[str] = []
    stop = asyncio.Event()

    class _Gw:
        async def handle_job(self, job):
            if job.msg_id == "slow":
                await asyncio.sleep(0.3)
            order.append(job.msg_id)

    class _Client:
        def __init__(self) -> None:
            self.acked: list[str] = []
            self._sent = False

        async def pull(self, *, batch_size, visibility_timeout_s):
            await asyncio.sleep(0)
            if self._sent:
                return []
            self._sent = True
            return [KakaoJob("slow", "Lslow", {}), KakaoJob("fast", "Lfast", {})]

        async def ack(self, lease_ids):
            self.acked.extend(lease_ids)
            if "Lslow" in lease_ids:
                stop.set()

        async def aclose(self) -> None:
            return None

    client = _Client()
    asyncio.run(
        asyncio.wait_for(
            kakao_consume_loop(_Gw(), client, _settings(), stop), timeout=5
        )
    )
    # the fast job finished and was acked before the slow one
    assert order[0] == "fast"
    assert client.acked.index("Lfast") < client.acked.index("Lslow")


def test_in_time_delivery_failure_parks_for_next_touch(suite_db_url: str) -> None:
    class _FailFirst(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def post_callback(
            self, url, text, *, quick_replies=None, images=None, files=None
        ):
            self.calls += 1
            if self.calls == 1:  # the in-time delivery POST fails (Kakao 5xx)
                raise RuntimeError("kakao 5xx")
            await super().post_callback(
                url, text, quick_replies=quick_replies, images=images, files=files
            )

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            reg = KakaoTaskRegistry(_redis(), ttl_s=60)
            client = _FailFirst()
            gw = _gateway(pool, client, _FakeDispatcher(reply="answer"), reg, _settings())

            await gw.handle_job(_job(utterance="hi"))
            # the spent callback couldn't deliver → answer is PARKED, not lost
            assert (await reg.get_turn("kc1") or {}).get("state") == "ready"
            # next touch ([확인]) delivers it on a fresh callback
            await gw.handle_job(_job(msg_id="m2", utterance="/check"))
            assert client.posts[-1][1] == "answer"
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- cleanup sweep: budget default + mark_stopped ttl ------------------


def test_callback_budget_fresh_missing_and_stale() -> None:
    gw = _poll_gateway(_RecordingClient(), KakaoTaskRegistry(_redis(), ttl_s=60))
    now = int(time.time() * 1000)
    # fresh → ~deadline (50), capped by ttl-margin (55)
    assert gw._callback_budget({"received_at": now}) > 40
    # missing → 0.0 (treated as stale; the safe default → park-only)
    assert gw._callback_budget({}) == 0.0
    # older than the TTL → ≤ 0 → park-only
    assert gw._callback_budget({"received_at": now - 120_000}) <= 0


def test_mark_stopped_sets_ttl() -> None:
    async def _drive() -> None:
        r = _redis()
        reg = KakaoTaskRegistry(r, ttl_s=60)
        await reg.try_begin("c")
        await reg.mark_stopped("c")
        assert await r.ttl("kakao:turn:c") > 0  # recreated key can't leak

    asyncio.run(_drive())


def test_pull_coerces_string_and_b64_body() -> None:
    """CF Queues HTTP pull returns a json body as a STRING (sometimes base64);
    pull() must normalize each to a dict so handle_job never sees a str."""
    import base64 as _b64

    payload = {"chat_id": "kc1", "utterance": "hi", "callback_url": "https://cb/x"}
    as_str = json.dumps(payload)
    as_b64 = _b64.b64encode(as_str.encode()).decode()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "result": {"messages": [
                {"id": "m1", "lease_id": "L1", "body": as_str},   # JSON string
                {"id": "m2", "lease_id": "L2", "body": as_b64},   # base64 of JSON
                {"id": "m3", "lease_id": "L3", "body": "garbage"},  # unparsable → {}
                {"id": "m4", "lease_id": "L4", "body": payload},  # already a dict
            ]},
        })

    async def _drive() -> list[KakaoJob]:
        c = HttpKakaoClient(_settings())
        c._queue_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        jobs = await c.pull(batch_size=4, visibility_timeout_s=10)
        await c.aclose()
        return jobs

    jobs = asyncio.run(_drive())
    assert all(isinstance(j.body, dict) for j in jobs)
    assert jobs[0].body == payload
    assert jobs[1].body == payload
    assert jobs[2].body == {}        # garbage → empty (handled as missing-fields)
    assert jobs[3].body == payload
