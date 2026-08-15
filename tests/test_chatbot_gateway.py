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

from bp_agents.agents.chatbot.credentials import LinkRefused
from bp_agents.agents.chatbot.gateway import (
    _LINK_INVALID,
    _LINK_OK,
    _LINK_PRIVILEGED,
    _LINK_USAGE,
    _SETDEFAULT_OK,
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
from tests.fake_store import FakeChannelStore, FakeStore
from tests.fake_store import state_value as _state

# The gateway takes SuiteSettings for the operator defaults behind an
# unset user preference; `database_url` is never read through it here.
_SETTINGS = SuiteSettings(database_url="postgresql://unused/unused")


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
            "TRUNCATE TABLE user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.upsert_platform_mapping(
            conn, platform="telegram", chat_id=chat_id, user_id=user_id,
            session_id=session_id,
        )
        await queries.create_user_config(
            conn, user_id=user_id, default_session_id=session_id
        )


def test_gateway_dispatches_and_relays(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            disp = _FakeDispatcher(reply="the answer")
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=tg, store=FakeChannelStore(store))

            await gw.handle_update("tg1", "what's up?")

            # Injected to the orchestrator on behalf of the user.
            assert disp.spawns == [
                ("orchestrator", "what's up?", "usr_a", "ses_1", "message")
            ]
            # Reply relayed.
            assert tg.sent == [("tg1", "the answer")]
            # The channel wrote NO history — the user's words ride the task
            # payload and the executing agent records them under its own
            # authorship. There is no endpoint through which it could.
            assert store.messages == []
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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=tg, store=FakeChannelStore(store))

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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=tg, store=FakeChannelStore(store))

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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=tg, store=FakeChannelStore(store))

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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=_FakeTelegram(), store=FakeChannelStore(store))

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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=disp, pool=pool, telegram=_FakeTelegram(), store=FakeChannelStore(store))
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
            store = FakeStore()
            gw = ChatbotGateway(settings=_SETTINGS, dispatcher=_FailingDispatcher(), pool=pool, telegram=tg, store=FakeChannelStore(store))
            await gw.handle_update("tg1", "/config")
            assert len(tg.sent) == 1
            assert "went wrong" in tg.sent[0][1]
            assert "Done." not in tg.sent[0][1]
        finally:
            await pool.close()

    asyncio.run(_drive())


# --- /delegate · /undelegate -------------------------------------------

_DELEGATABLE = frozenset({"research", "computer_use", "deep_reasoning"})


def _deleg_gw(pool, tg, disp, store=None):
    return ChatbotGateway(
        settings=_SETTINGS,
        dispatcher=disp, pool=pool, telegram=tg,
        store=FakeChannelStore(store or FakeStore()),
        delegatable_agents=_DELEGATABLE,
    )


def test_delegate_sets_state_and_seeds_delegate_thread(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            store = FakeStore()
            store.add("orchestrator", "user", "help me plan a trip")
            tg = _FakeTelegram()
            gw = _deleg_gw(
                pool, tg, _FakeDispatcher(reply="trip-planning summary"), store
            )
            await gw.handle_update("tg1", "/delegate research")

            assert store.session_state["delegated_to"].value == "research"
            # The seed is a HAND-OVER, not a write into the delegate's
            # thread: the channel cannot reach it, and the delegate
            # materialises this under its own authorship on its first turn.
            queued = store.handovers["research"]
            assert [i.item_kind for i in queued] == ["seed"]
            seed = queued[0].payload["text"]
            assert "delegated this conversation" in seed
            assert "trip-planning summary" in seed  # summarizer output
            assert store.thread("research") == []
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
            store = FakeStore()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(), store)
            await gw.handle_update("tg1", "/delegate memory")
            assert "Can't delegate" in tg.sent[-1][1]
            assert "delegated_to" not in store.session_state
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_undelegate_folds_back_to_main(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            store = FakeStore()
            store.session_state["delegated_to"] = _state("delegated_to", "research")
            last = store.add("research", "assistant", "found 3 flights")
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(reply="did the research"), store)
            await gw.handle_update("tg1", "/undelegate")

            assert "delegated_to" not in store.session_state
            # Both halves are hand-overs: the recap for the orchestrator to
            # materialise, and a retire cutoff for the delegate to apply to
            # its OWN floor. Neither thread is touched by the channel.
            recap = store.handovers["orchestrator"][0]
            assert recap.item_kind == "recap"
            assert "Returned from Research" in recap.payload["text"]
            retire = store.handovers["research"][0]
            assert retire.item_kind == "retire"
            assert retire.payload["through_id"] == last.id
            assert store.floors == {}, "only the delegate may move its floor"
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
            store = FakeStore()
            store.session_state["delegated_to"] = _state("delegated_to", "research")
            store.add("research", "assistant", "research output")
            tg = _FakeTelegram()
            gw = _deleg_gw(pool, tg, _FakeDispatcher(reply="s"), store)
            await gw.handle_update("tg1", "/delegate computer_use")

            assert store.session_state["delegated_to"].value == "computer_use"
            kinds = {
                agent: [i.item_kind for i in items]
                for agent, items in store.handovers.items() if items
            }
            # The old delegate is folded back (recap to the orchestrator,
            # retire to itself) and the new one seeded — all four via the
            # queue, since the channel writes no thread.
            assert kinds["orchestrator"] == ["recap"]
            assert kinds["research"] == ["retire"]
            assert kinds["computer_use"] == ["seed"]
            recap = store.handovers["orchestrator"][0].payload["text"]
            assert "Returned from Research" in recap
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
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=_FakeTelegram(), credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg1", "/new")

            # Previous session closed on the router.
            assert creds.closed == [("usr_a", "ses_1")]
            assert creds.opened == ["usr_a"]
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
            # Released: the previous session keeps its history (the router's
            # now) but loses its channel-origin flag, so the webapp may
            # reopen or remove it.
            assert "kind" not in store.metadata_by_session["ses_1"]
            # The fresh session is opened with the channel metadata by the
            # router itself, and becomes the cron fallback.
            assert creds.opened == ["usr_a"]
            assert cfg.default_session_id == "ses_2"
        finally:
            await pool.close()

    asyncio.run(_drive())


class _LinkCreds:
    """Credentials double for the /link flow: link_channel returns the
    configured user_id (or None to simulate a bad/refused token), and
    open_session hands back a fresh session id for the linked chat's own
    conversation."""

    def __init__(
        self,
        *,
        user_id: str | None,
        new_session: str = "ses_link",
        refuse: bool = False,
    ) -> None:
        self._user_id = user_id
        self._new = new_session
        self._refuse = refuse
        self.verified: list[str] = []
        self.opened: list[str] = []

    async def link_channel(self, *, token: str) -> str | None:
        self.verified.append(token)
        if self._refuse:
            raise LinkRefused
        return self._user_id

    async def open_session(self, *, user_id, metadata=None) -> str:
        self.opened.append(user_id)
        return self._new


def test_link_binds_unmapped_chat_to_existing_account(suite_db_url: str) -> None:
    """`/link <token>` on an unmapped chat verifies the token, maps the chat to
    the returned user_id, AND opens the chat its OWN session — so it joins the
    account but keeps a separate conversation (not the account's default
    ses_1)."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)  # usr_a mapped to chat "tg1", default = ses_1
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id="usr_a", new_session="ses_link")
            store = FakeStore()
            # ses_1 is already this channel's, so /link must NOT re-point the
            # cron default at the new chat's session.
            store.metadata_by_session["ses_1"] = {"kind": "chatbot_telegram"}
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg_new", "/link tok-abc")

            assert creds.verified == ["tok-abc"]
            assert creds.opened == ["usr_a"]  # opened the linked chat's session
            assert tg.sent == [("tg_new", _LINK_OK)]
            async with pool.acquire() as conn:
                mapping = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg_new"
                )
                cfg = await queries.get_user_config(conn, "usr_a")
            assert mapping is not None and mapping.user_id == "usr_a"
            # Its OWN session, distinct from the account's default (ses_1).
            assert mapping.session_id == "ses_link"
            assert cfg.default_session_id == "ses_1"  # default untouched
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
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
                store=FakeChannelStore(store),
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


def test_link_privileged_target_reports_refusal_and_does_not_map(
    suite_db_url: str,
) -> None:
    """A VALID token for an admin/service account is refused by the router
    (403 → LinkRefused). The chat gets the privileged-account message (not the
    misleading "invalid or expired") and stays unmapped."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id="usr_a", refuse=True)
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg_new", "/link tok-admin")

            assert creds.verified == ["tok-admin"]
            assert creds.opened == []  # no session opened
            assert tg.sent == [("tg_new", _LINK_PRIVILEGED)]
            async with pool.acquire() as conn:
                resolved = await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg_new"
                )
            assert resolved is None
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_link_promotes_default_when_current_is_webapp(suite_db_url: str) -> None:
    """A web-first account whose default session lives on a (non-pushable)
    webapp session hands the cron/notification default to Telegram on /link —
    Telegram is the only out-of-band carrier ([cron.md] §6)."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE user_config, "
                    "suite_platform_mappings RESTART IDENTITY"
                )
                # usr_a: a webapp-default account with no Telegram chat yet.
                await queries.create_user_config(
                    conn, user_id="usr_a", default_session_id="ses_web"
                )
            tg = _FakeTelegram()
            creds = _LinkCreds(user_id="usr_a", new_session="ses_tg")
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg_new", "/link tok-xyz")

            assert tg.sent == [("tg_new", _LINK_OK)]
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
            # Promoted from the webapp session to the new Telegram session.
            assert cfg.default_session_id == "ses_tg"
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
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=tg, credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg_new", "/link")

            assert tg.sent == [("tg_new", _LINK_USAGE)]
            assert creds.verified == []  # never reached the router
        finally:
            await pool.close()

    asyncio.run(_drive())


async def _seed_two_chats(pool) -> None:
    """usr_a with TWO chats, each on its OWN session: tg1->ses_1 (also the
    user's default) and tg2->ses_2. Models a multi-channel/linked user."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )
        await queries.create_user_config(
            conn, user_id="usr_a", default_session_id="ses_1"
        )
        for chat, sid in (("tg1", "ses_1"), ("tg2", "ses_2")):
            await queries.upsert_platform_mapping(
                conn, platform="telegram", chat_id=chat, user_id="usr_a",
                session_id=sid,
            )


def test_each_chat_routes_to_its_own_session(suite_db_url: str) -> None:
    """Two chats on one account route to their OWN sessions, not the shared
    default — a message on tg2 dispatches to ses_2 even though the user's
    default_session_id is ses_1."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed_two_chats(pool)
            disp = _FakeDispatcher(reply="ok")
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=disp, pool=pool, telegram=_FakeTelegram(),
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg2", "hello from kakao-side")

            # Dispatched to tg2's own session, not the default ses_1.
            assert disp.spawns == [
                ("orchestrator", "hello from kakao-side", "usr_a", "ses_2",
                 "message")
            ]
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_setdefault_points_default_at_this_chats_session(suite_db_url: str) -> None:
    """/setdefault moves the cron-fallback default to the session of the chat
    it's issued from (tg2 -> ses_2), without touching other chats."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed_two_chats(pool)  # default starts at ses_1
            tg = _FakeTelegram()
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool, telegram=tg,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg2", "/setdefault")

            assert tg.sent == [("tg2", _SETDEFAULT_OK)]
            async with pool.acquire() as conn:
                cfg = await queries.get_user_config(conn, "usr_a")
                m1 = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg1"
                )
            assert cfg.default_session_id == "ses_2"  # moved to tg2's session
            assert m1.session_id == "ses_1"  # tg1's own session unchanged
        finally:
            await pool.close()

    asyncio.run(_drive())


def test_new_repoints_only_this_chats_session(suite_db_url: str) -> None:
    """/new on tg2 closes tg2's OWN session (ses_2), opens a fresh one, and
    re-points both tg2's mapping and the default — WITHOUT closing tg1's
    session (ses_1)."""

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed_two_chats(pool)
            creds = _FakeCreds(new_session="ses_2b")
            store = FakeStore()
            gw = ChatbotGateway(
                settings=_SETTINGS,
                dispatcher=_FakeDispatcher(), pool=pool,
                telegram=_FakeTelegram(), credentials=creds,
                store=FakeChannelStore(store),
            )

            await gw.handle_update("tg2", "/new")

            # Closed tg2's own session only — NOT tg1's ses_1.
            assert creds.closed == [("usr_a", "ses_2")]
            async with pool.acquire() as conn:
                m1 = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg1"
                )
                m2 = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg2"
                )
                cfg = await queries.get_user_config(conn, "usr_a")
            assert m1.session_id == "ses_1"  # untouched
            assert m2.session_id == "ses_2b"  # tg2 moved to the fresh session
            assert cfg.default_session_id == "ses_2b"  # re-pointed (newest wins)
        finally:
            await pool.close()

    asyncio.run(_drive())
