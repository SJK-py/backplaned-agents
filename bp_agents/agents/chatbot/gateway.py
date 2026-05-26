"""chatbot.gateway — the inbound message engine.

The testable core of the channel: resolve a chat to a user/session,
serialize turns per session, write the user turn, inject it as a root
task on behalf of the user, await the result, and relay it. Transport
(Telegram) and task injection (the SDK agent) are injected so the engine
is unit-testable without a network or a router.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from bp_agents.agents.chatbot.telegram import TelegramClient
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
CHANNEL = "chatbot_telegram"
ORCHESTRATOR_AGENT_ID = "orchestrator"

HELP_TEXT = (
    "I'm your personal assistant. Just send me a message and I'll help.\n\n"
    "Commands:\n"
    "/help — show this message"
)
REGISTER_PROMPT = (
    "You're not registered yet. Registration isn't available on this "
    "channel yet — please check back soon."
)
_NO_SESSION = (
    "Your account has no active conversation yet. Please contact an "
    "administrator."
)
_DISPATCH_FAILED = "Sorry — something went wrong handling that. Please try again."


class RootDispatcher(Protocol):
    """The slice of the SDK `Agent` the gateway needs — root-task
    injection on behalf of an end user (B1)."""

    async def spawn_root_for_user(
        self,
        destination_agent_id: str,
        payload: Any,
        *,
        user_id: str,
        session_id: str,
        mode: str | None = None,
        **kwargs: Any,
    ) -> str: ...

    async def await_root_result(
        self, task_id: str, *, timeout_s: float | None = None, **kwargs: Any
    ) -> Any: ...


class ChatbotGateway:
    """Handles one inbound message end-to-end. One instance per process;
    the per-session locks live here."""

    def __init__(
        self,
        *,
        dispatcher: RootDispatcher,
        pool: asyncpg.Pool,
        telegram: TelegramClient,
        result_timeout_s: float = 180.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._pool = pool
        self._telegram = telegram
        self._result_timeout_s = result_timeout_s
        # Per-`session_id` FIFO serialization ([sessions.md] §4). In-memory
        # (single channel instance); Redis / session-affinity for multi-worker.
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def handle_update(self, chat_id: str, text: str) -> None:
        """Entry point for one inbound text message."""
        text = text.strip()
        if text.startswith("/"):
            await self._handle_command(chat_id, text)
            return

        async with self._pool.acquire() as conn:
            user_id = await queries.resolve_user_id(
                conn, platform=PLATFORM, chat_id=chat_id
            )
            if user_id is None:
                await self._telegram.send_message(
                    chat_id=chat_id, text=REGISTER_PROMPT
                )
                return
            cfg = await queries.get_user_config(conn, user_id)

        session_id = cfg.default_session_id if cfg else None
        if session_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=_NO_SESSION)
            return

        await self._dispatch_turn(chat_id, user_id, session_id, text)

    async def _handle_command(self, chat_id: str, text: str) -> None:
        cmd = text.split(maxsplit=1)[0].lower()
        if cmd in ("/help", "/start"):
            await self._telegram.send_message(chat_id=chat_id, text=HELP_TEXT)
            return
        await self._telegram.send_message(
            chat_id=chat_id,
            text="That command isn't supported yet. Try /help.",
        )

    async def _dispatch_turn(
        self, chat_id: str, user_id: str, session_id: str, text: str
    ) -> None:
        """Serialize on the session, write the user turn, inject the task,
        await the result, and relay it."""
        async with self._session_lock(session_id):
            # Routing: the delegate during an active delegation, else the
            # orchestrator. Phase 1 has no delegation, so `delegated_to`
            # is always None and this resolves to the orchestrator.
            async with self._pool.acquire() as conn:
                info = await queries.get_session_info(conn, session_id)
                dest = (
                    info.delegated_to
                    if info and info.delegated_to
                    else ORCHESTRATOR_AGENT_ID
                )
                mode = (
                    "delegated_message"
                    if info and info.delegated_to
                    else "message"
                )
                # The channel is the sole writer of user turns, written
                # verbatim BEFORE dispatch so the agent's reload sees it.
                await queries.append_history(
                    conn,
                    session_id=session_id,
                    agent_id=dest,
                    role="user",
                    message=text,
                )

            try:
                task_id = await self._dispatcher.spawn_root_for_user(
                    dest,
                    MessagePayload(prompt=text),
                    user_id=user_id,
                    session_id=session_id,
                    mode=mode,
                )
                result = await self._dispatcher.await_root_result(
                    task_id, timeout_s=self._result_timeout_s
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dispatch_failed",
                    extra={
                        "event": "dispatch_failed",
                        "bp.session_id": session_id,
                    },
                )
                await self._telegram.send_message(
                    chat_id=chat_id, text=_DISPATCH_FAILED
                )
                return

            reply = (result.output.content if result.output else "") or ""
            await self._telegram.send_message(
                chat_id=chat_id, text=reply or "(no response)"
            )
