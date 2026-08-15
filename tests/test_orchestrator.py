"""orchestrator `message` turn — core logic, DB-free.

Stubs `ctx.llm` (so no router / provider is needed) and runs the whole turn
against the fake session store. There is no suite database here any more:
the conversation is the router's store and the user's settings are its user
scope, so the orchestrator holds no pool at all.
"""

from __future__ import annotations

import asyncio

from bp_agents.agents.orchestrator import ORCHESTRATOR_AGENT_ID, run_orchestrator_message
from bp_agents.common.payloads import MessagePayload
from bp_agents.settings import SuiteSettings
from bp_sdk import LlmResponse, Message
from tests.fake_store import FakeHistory, FakeStore, state_value

_SETTINGS = SuiteSettings(database_url="postgresql://unused/unused")


class _StubLlm:
    def __init__(self, text: str) -> None:
        self.text = text
        self.captured: list[Message] | None = None

    async def generate(self, messages, **kw) -> LlmResponse:
        self.captured = list(messages)
        return LlmResponse(text=self.text, tool_calls=[])


class _StubPeers:
    def visible(self, *, for_user_level=None):
        return {}


class _StubProgress:
    async def emit(self, *a, **k) -> None:
        return None


class _StubCtx:
    def __init__(self, user_id: str, session_id: str, llm, store=None) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.user_level = "tier0"
        self.llm = llm
        self.peers = _StubPeers()
        self.progress = _StubProgress()
        self.store = store if store is not None else FakeStore()
        self.history = FakeHistory(self.store, "orchestrator")


def test_orchestrator_message_uses_history_and_persists_reply() -> None:
    async def _drive() -> None:
        store = FakeStore()
        store.set_pref("full_name", "Ada")
        store.set_pref("timezone", "UTC")
        store.thread_state[(ORCHESTRATOR_AGENT_ID, "")] = {
            "summary": state_value("summary", "prior summary text")
        }

        llm = _StubLlm("hello back")
        ctx = _StubCtx("usr_a", "ses_1", llm, store)
        out = await run_orchestrator_message(
            ctx,  # type: ignore[arg-type]
            MessagePayload(prompt="hi there"),
            settings=_SETTINGS,
        )

        assert out.content == "hello back"
        assert out.metadata["context_tokens"] > 0

        # System prompt carried the user-config note + rolling summary.
        assert llm.captured is not None
        system = llm.captured[0]
        assert system.role == "system"
        assert "Ada" in system.content
        assert "prior summary text" in system.content

        # The agent recorded the user's turn itself, from the payload,
        # and it appears exactly once in the built context.
        user_msgs = [m for m in llm.captured if m.role == "user"]
        assert [m.content for m in user_msgs] == ["hi there"]

        # Both turns are on the orchestrator's own thread.
        assert store.roles(ORCHESTRATOR_AGENT_ID) == ["user", "assistant"]
        assert store.thread(ORCHESTRATOR_AGENT_ID)[-1].content == "hello back"

    asyncio.run(_drive())


def test_orchestrator_message_runs_for_a_user_with_no_stored_settings() -> None:
    """An empty user scope is the NORMAL case, not an error: a user who has
    never opened Settings has no keys at all, and the turn runs on the
    operator defaults.

    The note is still rendered, carrying those defaults — which is what the
    model actually operates under. Previously a user with no `user_config`
    row got no note at all while one with a freshly seeded row got exactly
    these values; the defaults now apply live, so the two agree."""

    async def _drive() -> None:
        llm = _StubLlm("ok")
        ctx = _StubCtx("usr_x", "ses_x", llm)
        out = await run_orchestrator_message(
            ctx,  # type: ignore[arg-type]
            MessagePayload(prompt="fresh question"),
            settings=_SETTINGS,
        )
        assert out.content == "ok"
        assert llm.captured is not None
        system = llm.captured[0].content
        assert "User's timezone: UTC" in system
        assert "Preferred language: en" in system
        assert "User's name" not in system  # nothing invented
        user_msgs = [m for m in llm.captured if m.role == "user"]
        assert [m.content for m in user_msgs] == ["fresh question"]

    asyncio.run(_drive())
