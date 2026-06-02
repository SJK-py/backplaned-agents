"""chatbot KakaoTalk channel (PR1 plumbing) — no DB required.

Covers the activation gate, the CF Queues pull/ack client (request shape
+ response parsing, via httpx.MockTransport like the Telegram client
tests), and the skeleton consumer loop (drain → ack → stop). Turn
processing lands in a later PR and is not exercised here.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from bp_agents.agents.chatbot.agent import _kakao_configured
from bp_agents.agents.chatbot.kakao_client import HttpKakaoClient, KakaoJob
from bp_agents.agents.chatbot.kakao_consumer import kakao_consume_loop
from bp_agents.settings import SuiteSettings


def _settings(**kw) -> SuiteSettings:
    return SuiteSettings(_env_file=None, **kw)  # type: ignore[call-arg]


def _configured() -> SuiteSettings:
    return _settings(
        kakao_cf_account_id="A", kakao_cf_queue_id="Q", kakao_cf_api_token="T"
    )


# --- activation gate ---------------------------------------------------


def test_kakao_configured_requires_all_three() -> None:
    assert _kakao_configured(_settings()) is False
    assert _kakao_configured(_settings(kakao_cf_account_id="a")) is False
    assert (
        _kakao_configured(
            _settings(kakao_cf_account_id="a", kakao_cf_queue_id="q")
        )
        is False
    )
    assert _kakao_configured(_configured()) is True


def test_api_token_is_secret() -> None:
    s = _configured()
    assert s.kakao_cf_api_token is not None
    # masked in repr, recoverable via get_secret_value()
    assert "T" not in repr(s.kakao_cf_api_token)
    assert s.kakao_cf_api_token.get_secret_value() == "T"


# --- CF Queues pull/ack client -----------------------------------------


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
                        {
                            "id": "m1",
                            "lease_id": "L1",
                            "body": {"chat_id": "c1", "utterance": "hi"},
                        }
                    ]
                },
            },
        )

    async def _drive() -> list[KakaoJob]:
        c = HttpKakaoClient(_configured())
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        jobs = await c.pull(batch_size=7, visibility_timeout_s=30)
        await c.aclose()
        return jobs

    jobs = asyncio.run(_drive())
    assert captured["url"].endswith("/accounts/A/queues/Q/messages/pull")
    assert captured["body"] == {"batch_size": 7, "visibility_timeout_ms": 30000}
    assert len(jobs) == 1
    assert (jobs[0].msg_id, jobs[0].lease_id) == ("m1", "L1")
    assert jobs[0].body["utterance"] == "hi"


def test_pull_returns_empty_when_not_success() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "errors": []})

    async def _drive() -> list[KakaoJob]:
        c = HttpKakaoClient(_configured())
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        out = await c.pull(batch_size=1, visibility_timeout_s=1)
        await c.aclose()
        return out

    assert asyncio.run(_drive()) == []


def test_ack_posts_lease_ids_and_noops_on_empty() -> None:
    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async def _drive() -> None:
        c = HttpKakaoClient(_configured())
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        await c.ack([])  # no-op — must not hit the network
        await c.ack(["L1", "L2"])
        await c.aclose()

    asyncio.run(_drive())
    assert calls == [{"acks": [{"lease_id": "L1"}, {"lease_id": "L2"}]}]


# --- skeleton consumer loop --------------------------------------------


def test_consume_loop_drains_acks_then_stops() -> None:
    stop = asyncio.Event()
    batch = [
        KakaoJob(msg_id="m1", lease_id="L1", body={}),
        KakaoJob(msg_id="m2", lease_id="L2", body={}),
    ]

    class _FakeClient:
        def __init__(self) -> None:
            self.acked: list[list[str]] = []
            self.pulls = 0

        async def pull(self, *, batch_size, visibility_timeout_s):
            self.pulls += 1
            return [] if stop.is_set() else batch

        async def ack(self, lease_ids):
            self.acked.append(list(lease_ids))
            stop.set()  # one batch is enough; unblock the loop's exit

        async def aclose(self) -> None: ...

    c = _FakeClient()
    asyncio.run(
        asyncio.wait_for(kakao_consume_loop(c, _configured(), stop), timeout=5)
    )
    assert c.acked == [["L1", "L2"]]
    assert c.pulls == 1
