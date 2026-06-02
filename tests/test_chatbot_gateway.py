"""chatbot gateway — inbound message engine against a live suite DB.

Fakes the Telegram client and the SDK root-dispatcher; uses a real
`bp_suite` database. Covers identity resolution, the user-turn write +
dispatch + relay, the unmapped/`/help`/failure paths, and per-session
serialization.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from bp_agents.agents.chatbot.gateway import (
    _LINK_INVALID,
    _LINK_OK,
    _LINK_USAGE,
    BOT_COMMANDS,
    HELP_TEXT,
    REGISTER_PROMPT,
    ChatbotGateway,
)
from bp_agents.agents.chatbot.telegram import HttpTelegramClient
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput, TaskStatus


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, *, offset, timeout_s):
        return []

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class _FakeDispatcher:
    def __init__(self, *, reply: str = "hi from orch", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.spawns: list[tuple] = []

    async def spawn_root_for_user(
        self, dest, payload, *, user_id, session_id, mode=None, **kw
    ) -> str:
        prompt = getattr(payload, "prompt", None)  # summarizer payloads have none
        self.spawns.append((dest, prompt, user_id, session_id, mode))
        if self.fail:
            raise RuntimeError("admit failed")
        return f"tsk:{prompt}"

    async def await_root_result(self, task_id, *, timeout_s=None, **kw):
        return ResultFrame(
            agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content=self.reply),
        )


async def _seed(pool, *, chat_id="tg1", user_id="usr_a", session_id="ses_1") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id=chat_id, user_id=user_id
        )
        await queries.create_user_config(
            conn, user_id=user_id, default_session_id=session_id
        )
        await queries.create_session_info(
            conn, session_id=session_id, user_id=user_id, channel="chatbot_telegram"
        )


def test_gateway_dispatches_and_relays(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher(reply="the answer")
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "what's up?")

            # Injected to the orchestrator on behalf of the user.
            assert disp.spawns == [
                ("orchestrator", "what's up?", "usr_a", "ses_1", "message")
            ]
            # Reply relayed.
            assert tg.sent == [("tg1", "the answer")]
            # User turn written verbatim to the orchestrator thread.
            async with pool.acquire() as conn:
                rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
            assert [(r.role, r.message) for r in rows] == [("user", "what's up?")]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_unmapped_chat_gets_register_prompt(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg_unknown", "hello")
            assert tg.sent == [("tg_unknown", REGISTER_PROMPT)]
            assert disp.spawns == []
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_help_command(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "/help")
            assert tg.sent == [("tg1", HELP_TEXT)]
            assert disp.spawns == []
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_dispatch_failure_is_surfaced(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher(fail=True)
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=tg)

            await gw.handle_update("tg1", "boom please")
            assert len(tg.sent) == 1
            assert "went wrong" in tg.sent[0][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_gateway_serializes_per_session(suite_db_url: str) -> None:
    """Two concurrent turns on one session must not interleave —
    spawn/result for one completes before the next begins."""

    class _OrderingDispatcher:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def spawn_root_for_user(
            self, dest, payload, *, user_id, session_id, mode=None, **kw
        ) -> str:
            self.events.append(f"spawn:{payload.prompt}")
            return f"tsk:{payload.prompt}"

        async def await_root_result(self, task_id, *, timeout_s=None, **kw):
            await asyncio.sleep(0.05)  # hold the session "busy"
            prompt = task_id.split(":", 1)[1]
            self.events.append(f"result:{prompt}")
            return ResultFrame(
                agent_id="orchestrator", trace_id="0" * 32, span_id="0" * 16,
                task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
                output=AgentOutput(content="ok"),
            )

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _OrderingDispatcher()
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=_FakeTelegram())

            await asyncio.gather(
                gw.handle_update("tg1", "a"),
                gw.handle_update("tg1", "b"),
            )
            # No interleave: each spawn is immediately followed by its
            # own result (whichever turn won the lock first).
            assert disp.events[0].startswith("spawn:")
            first = disp.events[0].split(":", 1)[1]
            assert disp.events[1] == f"result:{first}"
            assert disp.events[2].startswith("spawn:")
            second = disp.events[2].split(":", 1)[1]
            assert disp.events[3] == f"result:{second}"
            assert {first, second} == {"a", "b"}
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- command registration (setMyCommands) -------------------------------


def test_help_text_lists_every_command() -> None:
    # HELP_TEXT is derived from BOT_COMMANDS, so each stays in lockstep.
    for name, desc in BOT_COMMANDS:
        assert f"/{name}" in HELP_TEXT
        assert desc in HELP_TEXT


def test_set_my_commands_posts_normalized_payload() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    async def _drive() -> None:
        client = HttpTelegramClient("TOKEN", base_url="https://api.telegram.org")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        # Leading slash + mixed case must be normalized away.
        await client.set_my_commands([("/Help", "show help"), ("v", "verbose")])
        await client.aclose()

    asyncio.run(_drive())
    assert captured["url"].endswith("/botTOKEN/setMyCommands")
    assert captured["body"]["commands"] == [
        {"command": "help", "description": "show help"},
        {"command": "v", "description": "verbose"},
    ]


# --- slash-command routing + failure surfacing -------------------------


def test_cron_routes_to_config_agent(suite_db_url: str) -> None:
    """/cron is hosted on the config agent (the chatbot can't spawn to
    itself — the router denies self-call)."""
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            disp = _FakeDispatcher(reply="your jobs: none")
            gw = ChatbotGateway(dispatcher=disp, pool=pool, telegram=_FakeTelegram())
            await gw.handle_update("tg1", "/cron")
            assert disp.spawns == [
                ("config", "List my scheduled jobs.", "usr_a", "ses_1", "cron")
            ]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_cmd_agent_surfaces_failed_task(suite_db_url: str) -> None:
    """A FAILED task result is surfaced as an error, not masked as 'Done.'."""
    class _FailingDispatcher(_FakeDispatcher):
        async def await_root_result(self, task_id, *, timeout_s=None, **kw):
            return ResultFrame(
                agent_id="config", trace_id="0" * 32, span_id="0" * 16,
                task_id=task_id, status=TaskStatus.FAILED, status_code=500,
                output=None,
            )

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            gw = ChatbotGateway(dispatcher=_FailingDispatcher(), pool=pool, telegram=tg)
            await gw.handle_update("tg1", "/config")
            assert len(tg.sent) == 1
            assert "went wrong" in tg.sent[0][1]
            assert "Done." not in tg.sent[0][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- /delegate · /undelegate -------------------------------------------

_DELEGATABLE = frozenset({"research", "computer_use", "deep_reasoning"})


def _deleg_gw(pool, tg, disp):
    return ChatbotGateway(
        dispatcher=disp, pool=pool, telegram=tg, delegatable_agents=_DELEGATABLE
    )


def test_delegate_sets_state_and_seeds_delegate_thread(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="user", message="help me plan a trip",
                )
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(reply="trip-planning summary"))
            await gw.handle_update("tg1", "/delegate research")

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
                seed_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="research"
                )
            assert info.delegated_to == "research"
            assert seed_rows and "delegated this conversation" in seed_rows[-1].message
            assert "trip-planning summary" in seed_rows[-1].message  # summarizer output
            assert "Research" in tg.sent[-1][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_delegate_rejects_unknown_agent(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher())
            await gw.handle_update("tg1", "/delegate memory")
            assert "Can't delegate" in tg.sent[-1][1]
            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
            assert info.delegated_to is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_undelegate_folds_back_to_main(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.update_session_info(conn, "ses_1", delegated_to="research")
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="research",
                    role="assistant", message="found 3 flights",
                )
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(reply="did the research"))
            await gw.handle_update("tg1", "/undelegate")

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
                main = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                deleg = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="research"
                )
            assert info.delegated_to is None
            assert info.delegate_summary is None
            assert any("Returned from Research" in r.message for r in main)
            assert deleg == []  # delegate episode demoted
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_undelegate_when_not_delegated_is_noop(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher())
            await gw.handle_update("tg1", "/undelegate")
            assert "main assistant" in tg.sent[-1][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_delegate_switch_folds_old_then_seeds_new(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.update_session_info(conn, "ses_1", delegated_to="research")
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="research",
                    role="assistant", message="research output",
                )
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(reply="s"))
            await gw.handle_update("tg1", "/delegate computer_use")

            async with pool.acquire() as conn:
                info = await queries.get_session_info(conn, "ses_1")
                main = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                old = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="research"
                )
                new = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="computer_use"
                )
            assert info.delegated_to == "computer_use"
            assert any("Returned from Research" in r.message for r in main)  # old folded
            assert old == []                                                 # old demoted
            assert new and "delegated this conversation" in new[-1].message  # new seeded
        finally:
            await pool.close()

    asyncio.run(_drive())


class _FakeCreds:
    """Minimal ChannelCredentials for the `/new` lifecycle: open returns a
    fresh id, close records the (user, session) pair."""

    def __init__(self, *, new_session: str = "ses_2") -> None:
        self._new = new_session
        self.opened: list[str] = []
        self.closed: list[tuple[str, str]] = []

    async def open_session(self, *, user_id, metadata=None) -> str:
        self.opened.append(user_id)
        return self._new

    async def close_session(self, *, user_id, session_id) -> None:
        self.closed.append((user_id, session_id))


def test_new_closes_and_releases_previous_session(suite_db_url: str) -> None:
    """`/new` archives the prior session on the router AND clears its
    channel-origin flag so the webapp can reopen/remove it, then opens +
    points default at the fresh session."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # default_session_id = ses_1 (chatbot_telegram)
            creds = _FakeCreds(new_session="ses_2")
            gw = ChatbotGateway(
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=_FakeTelegram(), credentials=creds,
            )

            await gw.handle_update("tg1", "/new")

            # Previous session closed on the router.
            assert creds.closed == [("usr_a", "ses_1")]
            assert creds.opened == ["usr_a"]
            async with pool.acquire() as conn:
                prev = await queries.get_session_info(conn, "ses_1")
                new = await queries.get_session_info(conn, "ses_2")
                cfg = await queries.get_user_config(conn, "usr_a")
            # Released: prev row kept (history intact) but channel cleared.
            assert prev is not None and prev.channel is None
            # New session tracked + made default.
            assert new is not None and new.channel == "chatbot_telegram"
            assert cfg.default_session_id == "ses_2"
        finally:
            await pool.close()

    asyncio.run(_drive())


class _LinkCreds:
    """Credentials double for the /link flow: verify_link_token returns the
    configured user_id (or None to simulate a bad token), recording the
    token it was asked to verify."""

    def __init__(self, *, user_id: str | None) -> None:
        self._user_id = user_id
        self.verified: list[str] = []

    async def verify_link_token(self, *, token: str) -> str | None:
        self.verified.append(token)
        return self._user_id


def test_link_binds_unmapped_chat_to_existing_account(suite_db_url: str) -> None:
    """`/link <token>` on an unmapped chat verifies the token and maps the
    chat to the returned user_id — so it now resolves to the same account."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # usr_a mapped to chat "tg1"
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id="usr_a")
            gw = ChatbotGateway(
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
            )

            await gw.handle_update("tg_new", "/link tok-abc")

            assert creds.verified == ["tok-abc"]
            assert tg.sent == [("tg_new", _LINK_OK)]
            async with pool.acquire() as conn:
                resolved = await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg_new"
                )
            assert resolved == "usr_a"
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_link_invalid_token_reports_and_does_not_map(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id=None)  # router rejected the token
            gw = ChatbotGateway(
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
            )

            await gw.handle_update("tg_new", "/link bad")

            assert tg.sent == [("tg_new", _LINK_INVALID)]
            async with pool.acquire() as conn:
                resolved = await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg_new"
                )
            assert resolved is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_link_without_token_shows_usage(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id="usr_a")
            gw = ChatbotGateway(
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
            )

            await gw.handle_update("tg_new", "/link")

            assert tg.sent == [("tg_new", _LINK_USAGE)]
            assert creds.verified == []  # never reached the router
        finally:
            await pool.close()

    asyncio.run(_drive())
