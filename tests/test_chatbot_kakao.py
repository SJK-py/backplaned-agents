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

import httpx
import pytest

from bp_agents.agents.chatbot import kakao_gateway as kg
from bp_agents.agents.chatbot.agent import _kakao_configured
from bp_agents.agents.chatbot.kakao_client import (
    HttpKakaoClient,
    KakaoJob,
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
    b = {"chat_id": "kc1", "callback_url": "https://cb.kakao/x", "utterance": ""}
    b.update(body)
    return KakaoJob(msg_id=msg_id, lease_id="L1", body=b)


class _RecordingClient:
    """Captures post_callback so the gateway's delivery can be asserted."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, object]] = []

    async def pull(self, *, batch_size, visibility_timeout_s):
        return []

    async def ack(self, lease_ids):
        return None

    async def post_callback(self, callback_url, text, *, quick_replies=None):
        self.posts.append((callback_url, text, quick_replies))

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
        await reg.set_inflight("c", "usr", "tsk1")
        turn = await reg.get_turn("c")
        assert turn == {"state": "pending", "user_id": "usr", "task_id": "tsk1"}

        await reg.store_ready("c", "the answer")
        assert (await reg.get_turn("c"))["state"] == "ready"
        assert await reg.take_ready("c") == "the answer"
        assert await reg.take_ready("c") is None  # cleared on take
        assert await reg.get_turn("c") is None

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
        await reg.store_ready("kc1", "parked answer")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance=CHECK_LABEL))
        assert client.posts == [("https://cb.kakao/x", "parked answer", None)]
        assert await reg.get_turn("kc1") is None  # cleared

    asyncio.run(_drive())


def test_check_while_pending_reports_still_working() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await reg.set_inflight("kc1", "usr", "tsk1")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance=CHECK_LABEL))
        url, text, qr = client.posts[0]
        assert text == kg._STILL_WORKING_TEXT
        assert qr == [(CHECK_LABEL, CHECK_LABEL), (STOP_LABEL, STOP_LABEL)]

    asyncio.run(_drive())


def test_stop_cancels_pending_turn() -> None:
    class _Creds:
        def __init__(self) -> None:
            self.cancelled: list[tuple[str, str]] = []

        async def cancel_task(self, *, user_id, task_id):
            self.cancelled.append((user_id, task_id))

    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await reg.set_inflight("kc1", "usr", "tsk1")
        client, creds = _RecordingClient(), _Creds()
        gw = _poll_gateway(client, reg, credentials=creds)
        await gw.handle_job(_job(utterance=STOP_LABEL))
        assert creds.cancelled == [("usr", "tsk1")]
        assert client.posts == [("https://cb.kakao/x", kg._STOPPED_TEXT, None)]
        assert (await reg.get_turn("kc1")).get("stopped") == "1"

    asyncio.run(_drive())


def test_stop_with_nothing_running() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(utterance=STOP_LABEL))
        assert client.posts == [("https://cb.kakao/x", kg._NOTHING_RUNNING_TEXT, None)]

    asyncio.run(_drive())


def test_duplicate_job_is_dropped() -> None:
    async def _drive() -> None:
        reg = KakaoTaskRegistry(_redis(), ttl_s=60)
        await reg.store_ready("kc1", "parked")
        client = _RecordingClient()
        gw = _poll_gateway(client, reg)
        await gw.handle_job(_job(msg_id="dup", utterance=CHECK_LABEL))
        await gw.handle_job(_job(msg_id="dup", utterance=CHECK_LABEL))  # redelivery
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
    def __init__(self, *, reply="the answer", delay=0.0) -> None:
        self.reply = reply
        self.delay = delay

    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):
        return f"tsk:{getattr(payload, 'prompt', None)}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        from bp_protocol.frames import ResultFrame
        if self.delay:
            await asyncio.sleep(self.delay)
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self.reply),
        )


async def _seed(pool, *, chat_id="kc1", user_id="usr_k", session_id="ses_k") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="kakao", chat_id=chat_id, user_id=user_id
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
            assert client.posts == [("https://cb.kakao/x", "hi back", None)]
            assert await reg.get_turn("kc1") is None
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_k", agent_id="orchestrator"
                )
            assert [(r.role, r.message) for r in rows] == [("user", "hello?")]
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
            url, text, qr = client.posts[0]
            assert text == kg._WORKING_TEXT
            assert qr == [(CHECK_LABEL, CHECK_LABEL), (STOP_LABEL, STOP_LABEL)]

            # let the background turn finish and park its result
            for _ in range(50):
                if (await reg.get_turn("kc1") or {}).get("state") == "ready":
                    break
                await asyncio.sleep(0.02)

            # next touch ([확인]) delivers the parked answer on a fresh callback
            client.posts.clear()
            await gw.handle_job(_job(msg_id="m2", utterance=CHECK_LABEL))
            assert client.posts == [("https://cb.kakao/x", "slow answer", None)]
            assert await reg.get_turn("kc1") is None
        finally:
            await pool.close()

    asyncio.run(_drive())
