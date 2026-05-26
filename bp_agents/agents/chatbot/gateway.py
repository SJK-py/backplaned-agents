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

from bp_agents.agents.chatbot.credentials import ChannelCredentials
from bp_agents.agents.chatbot.telegram import TelegramClient
from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_protocol.types import TaskStatus

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
CHANNEL = "chatbot_telegram"
ORCHESTRATOR_AGENT_ID = "orchestrator"
MEMORY_AGENT_ID = "memory"
CONFIG_AGENT_ID = "config"
CHATBOT_AGENT_ID = "chatbot"

# Summarization tuning ([sessions.md] §3): fold the oldest ~70% of the
# incumbent window once a thread crosses the soft limit, but only when
# there's a meaningful number of turns to compress.
_SUMMARIZE_FRACTION = 0.7
_MIN_ROWS_TO_SUMMARIZE = 6
_DEFAULT_CONTEXT_LIMIT = 120_000

HELP_TEXT = (
    "I'm your personal assistant. Just send me a message and I'll help.\n\n"
    "Commands:\n"
    "/register [email] — request access (an admin approves it)\n"
    "/new — start a fresh conversation\n"
    "/stop — stop the current in-progress reply\n"
    "/config [text] — view or change your settings\n"
    "/cron [text] — manage scheduled reminders/tasks\n"
    "/help — show this message"
)
REGISTER_PROMPT = (
    "You're not registered yet. Send /register (optionally with your "
    "email) to request access; an administrator will review it."
)
_REGISTER_SUBMITTED = (
    "Thanks — your registration request was submitted. An administrator "
    "will review it, and I'll be ready once you're approved."
)
_ALREADY_REGISTERED = "You're already registered. Just send me a message!"
_UNAVAILABLE = "That command isn't available right now."
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
        credentials: ChannelCredentials | None = None,
        result_timeout_s: float = 180.0,
        fire_memory: bool = False,
    ) -> None:
        self._dispatcher = dispatcher
        self._pool = pool
        self._telegram = telegram
        self._credentials = credentials
        self._result_timeout_s = result_timeout_s
        self._fire_memory = fire_memory
        # Per-`session_id` FIFO serialization ([sessions.md] §4). In-memory
        # (single channel instance); Redis / session-affinity for multi-worker.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # chat_id → (user_id, task_id) of the in-flight turn, for /stop.
        self._current_task: dict[str, tuple[str, str]] = {}
        # Detached fire-and-forget memory.add tasks (tracked for cleanup).
        self._memory_tasks: set[asyncio.Task] = set()

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
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/help", "/start"):
            await self._telegram.send_message(chat_id=chat_id, text=HELP_TEXT)
        elif cmd == "/register":
            await self._cmd_register(chat_id, arg)
        elif cmd == "/new":
            await self._cmd_new(chat_id)
        elif cmd == "/stop":
            await self._cmd_stop(chat_id)
        elif cmd == "/config":
            await self._cmd_agent(chat_id, CONFIG_AGENT_ID, "message",
                                  arg or "Show my current settings.")
        elif cmd == "/cron":
            await self._cmd_agent(chat_id, CHATBOT_AGENT_ID, "cron",
                                  arg or "List my scheduled jobs.")
        else:
            await self._telegram.send_message(
                chat_id=chat_id,
                text="That command isn't supported. Try /help.",
            )

    async def _cmd_agent(
        self, chat_id: str, dest: str, mode: str, prompt: str
    ) -> None:
        """Route a slash command to an agent (config / cron), bypassing the
        orchestrator and the conversation thread, and relay the reply."""
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=REGISTER_PROMPT)
            return
        async with self._pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        session_id = cfg.default_session_id if cfg else None
        if session_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=_NO_SESSION)
            return
        try:
            task_id = await self._dispatcher.spawn_root_for_user(
                dest, MessagePayload(prompt=prompt),
                user_id=user_id, session_id=session_id, mode=mode,
            )
            result = await self._dispatcher.await_root_result(
                task_id, timeout_s=self._result_timeout_s
            )
        except Exception:  # noqa: BLE001
            logger.exception("command_dispatch_failed",
                             extra={"event": "command_dispatch_failed", "cmd": mode})
            await self._telegram.send_message(chat_id=chat_id, text=_DISPATCH_FAILED)
            return
        reply = (result.output.content if result.output else "") or "Done."
        await self._telegram.send_message(chat_id=chat_id, text=reply)

    async def _resolve_user(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            return await queries.resolve_user_id(
                conn, platform=PLATFORM, chat_id=chat_id
            )

    async def _cmd_register(self, chat_id: str, email_arg: str) -> None:
        if await self._resolve_user(chat_id) is not None:
            await self._telegram.send_message(
                chat_id=chat_id, text=_ALREADY_REGISTERED
            )
            return
        if self._credentials is None:
            await self._telegram.send_message(chat_id=chat_id, text=_UNAVAILABLE)
            return
        try:
            await self._credentials.submit_registration(
                channel=CHANNEL,
                external_id=chat_id,
                requested_email=email_arg or None,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "registration_submit_failed",
                extra={"event": "registration_submit_failed"},
            )
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Couldn't submit your registration. Please try again.",
            )
            return
        await self._telegram.send_message(
            chat_id=chat_id, text=_REGISTER_SUBMITTED
        )

    async def _cmd_new(self, chat_id: str) -> None:
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._telegram.send_message(
                chat_id=chat_id, text=REGISTER_PROMPT
            )
            return
        if self._credentials is None:
            await self._telegram.send_message(chat_id=chat_id, text=_UNAVAILABLE)
            return
        new_session = await self._credentials.open_session(
            user_id=user_id,
            metadata={"kind": CHANNEL, "external_id": chat_id},
        )
        async with self._pool.acquire() as conn:
            await queries.create_session_info(
                conn, session_id=new_session, user_id=user_id,
                channel=CHANNEL, chat_id=chat_id,
            )
            await queries.set_default_session_id(
                conn, user_id=user_id, session_id=new_session
            )
        await self._telegram.send_message(
            chat_id=chat_id, text="Started a new conversation."
        )

    async def _cmd_stop(self, chat_id: str) -> None:
        current = self._current_task.get(chat_id)
        if current is None or self._credentials is None:
            await self._telegram.send_message(
                chat_id=chat_id, text="Nothing is running right now."
            )
            return
        user_id, task_id = current
        try:
            await self._credentials.cancel_task(user_id=user_id, task_id=task_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "cancel_failed", extra={"event": "cancel_failed"}
            )
        await self._telegram.send_message(chat_id=chat_id, text="Stopped.")

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
                # Record the in-flight task so /stop can cancel it.
                self._current_task[chat_id] = (user_id, task_id)
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
            finally:
                self._current_task.pop(chat_id, None)

            reply = (result.output.content if result.output else "") or ""
            await self._telegram.send_message(
                chat_id=chat_id, text=reply or "(no response)"
            )

            # delegated_to maintenance via result-source observation
            # ([delegation.md] §2) — who produced the result vs who we
            # dispatched to. Channel-owned (session.management).
            await self._update_delegation(session_id, dest, result)

            # Post-turn summarization check, still inside the session lock
            # so it serializes with the next turn ([sessions.md] §3.1). The
            # agent measured `context_tokens` while building its context.
            context_tokens = (
                result.output.metadata.get("context_tokens")
                if result.output
                else None
            )
            await self._maybe_summarize(session_id, dest, context_tokens)

        # memory.add is per-USER (not per-session) and a multi-LLM-call
        # extraction, so it runs OUTSIDE the session lock, fire-and-forget
        # ([overview.md] §2.2). Detached so the next turn isn't blocked.
        if self._fire_memory and reply:
            task = asyncio.create_task(
                self._fire_memory_add(user_id, session_id, text, reply)
            )
            self._memory_tasks.add(task)
            task.add_done_callback(self._memory_tasks.discard)

    async def _fire_memory_add(
        self, user_id: str, session_id: str, user_prompt: str, reply: str
    ) -> None:
        """Spawn `memory.add` for the turn (fire-and-forget — the result
        is ignored). Best-effort: a memory failure never affects the user."""
        from bp_agents.common.payloads import MemAdd  # noqa: PLC0415

        try:
            await self._dispatcher.spawn_root_for_user(
                MEMORY_AGENT_ID,
                MemAdd(user_prompt=user_prompt, assistant_response=reply),
                user_id=user_id, session_id=session_id, mode="add",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "memory_add_failed", extra={"event": "memory_add_failed"}
            )

    async def _update_delegation(self, session_id: str, dest: str, result) -> None:  # noqa: ANN001
        """Maintain `delegated_to` from the result source ([delegation.md] §2).

        - dispatched orchestrator but a delegate produced the result ⇒
          hand-off ⇒ set `delegated_to = <delegate>`.
        - dispatched a delegate but orchestrator produced the result ⇒
          hand-back ⇒ clear.
        - a delegated turn FAILED (F2) ⇒ revert to the orchestrator so the
          session isn't stuck routing to a broken delegate.
        """
        producer = result.agent_id
        failed = result.status != TaskStatus.SUCCEEDED
        update: tuple[str | None] | None = None  # (value,) when a change applies
        if failed and dest != ORCHESTRATOR_AGENT_ID:
            update = (None,)  # F2: broken delegate → back to orchestrator
        elif dest == ORCHESTRATOR_AGENT_ID and producer not in (
            ORCHESTRATOR_AGENT_ID, "router",
        ):
            update = (producer,)  # hand-off
        elif dest != ORCHESTRATOR_AGENT_ID and producer == ORCHESTRATOR_AGENT_ID:
            update = (None,)  # hand-back
        if update is not None:
            async with self._pool.acquire() as conn:
                await queries.update_session_info(
                    conn, session_id, delegated_to=update[0]
                )

    async def _maybe_summarize(
        self, session_id: str, agent_id: str, context_tokens: int | None
    ) -> None:
        """If the thread's context is over the user's soft limit, fold its
        oldest ~70% of incumbent turns into the rolling summary and demote
        them. Best-effort — a summarizer failure never breaks the turn."""
        if not context_tokens:
            return
        async with self._pool.acquire() as conn:
            info = await queries.get_session_info(conn, session_id)
            if info is None:
                return
            cfg = await queries.get_user_config(conn, info.user_id)
            limit = (
                cfg.max_context_token_limit
                if cfg
                else _DEFAULT_CONTEXT_LIMIT
            )
            if context_tokens <= limit:
                return
            rows = await queries.reload_incumbent(
                conn, session_id=session_id, agent_id=agent_id
            )
        if len(rows) < _MIN_ROWS_TO_SUMMARIZE:
            return

        # Fold the oldest ~70% of the incumbent window.
        cutoff_idx = max(1, int(len(rows) * _SUMMARIZE_FRACTION))
        up_to = rows[cutoff_idx - 1].id
        is_main = agent_id == ORCHESTRATOR_AGENT_ID
        previous = info.history_summary if is_main else info.delegate_summary

        try:
            from bp_agents.agents.history_summarizer import (  # noqa: PLC0415
                HISTORY_SUMMARIZER_AGENT_ID,
                SummarizeIncumbent,
            )

            task_id = await self._dispatcher.spawn_root_for_user(
                HISTORY_SUMMARIZER_AGENT_ID,
                SummarizeIncumbent(
                    agent_id=agent_id, up_to=up_to, previous_summary=previous
                ),
                user_id=info.user_id,
                session_id=session_id,
                mode="summarize_incumbent",
            )
            result = await self._dispatcher.await_root_result(
                task_id, timeout_s=self._result_timeout_s
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "summarize_failed",
                extra={"event": "summarize_failed", "bp.session_id": session_id},
            )
            return

        new_summary = (result.output.content if result.output else "") or ""
        field = "history_summary" if is_main else "delegate_summary"
        async with self._pool.acquire() as conn:
            await queries.update_session_info(conn, session_id, **{field: new_summary})
            await queries.demote_incumbent_through(
                conn, session_id=session_id, agent_id=agent_id, up_to_id=up_to
            )
