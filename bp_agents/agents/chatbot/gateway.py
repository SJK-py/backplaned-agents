"""chatbot.gateway — the inbound message engine.

The testable core of the channel: resolve a chat to a user/session,
serialize turns per session, write the user turn, inject it as a root
task on behalf of the user, await the result, and relay it. Transport
(Telegram) and task injection (the SDK agent) are injected so the engine
is unit-testable without a network or a router.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

from bp_agents.agents.chatbot.credentials import ChannelCredentials
from bp_agents.agents.chatbot.telegram import TelegramClient
from bp_agents.common.payloads import MessagePayload
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries
from bp_protocol.types import TaskStatus

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
CHANNEL = "chatbot_telegram"


async def send_named_file(
    *,
    telegram: TelegramClient,
    credentials: ChannelCredentials | None,
    chat_id: str,
    user_id: str,
    session_id: str,
    name: str,
) -> None:
    """Resolve a produced file-store name to bytes and send it as a
    Telegram document. Best-effort — a failure is logged, never raised
    (an undeliverable attachment must not break the turn). Shared by the
    inbound message path and the cron scheduler ([channel.md] §7)."""
    if credentials is None:
        return
    try:
        file_id = await credentials.resolve_named_file(
            user_id=user_id, session_id=session_id, name=name,
        )
        if file_id is None:
            return
        data = await credentials.fetch_file(user_id=user_id, file_id=file_id)
        await telegram.send_document(
            chat_id=chat_id, filename=name.rsplit("/", 1)[-1], data=data,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "outbound_file_failed", extra={"event": "outbound_file_failed"}
        )
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

# Single source of truth for the bot's commands: drives both the /help
# text and the Telegram `setMyCommands` registration (the "/" menu).
BOT_COMMANDS: list[tuple[str, str]] = [
    ("register", "request access (an admin approves it)"),
    ("new", "start a fresh conversation"),
    ("stop", "stop the current in-progress reply"),
    ("config", "view or change your settings"),
    ("cron", "manage scheduled reminders/tasks"),
    ("password", "get a one-time link to set a web password"),
    ("v", "verbose: prefix a message to see step-by-step progress"),
    ("help", "show the command list"),
]

HELP_TEXT = (
    "I'm your personal assistant. Just send me a message and I'll help.\n\n"
    "Commands:\n" + "\n".join(f"/{name} — {desc}" for name, desc in BOT_COMMANDS)
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
_TYPING_REFRESH_S = 4.0  # Telegram "typing…" lasts ~5s; refresh just under that.

# Verbose-mode rendering ([channel.md] §5).
_KIND_LABEL = {"tool_call": "[Tool]", "tool_result": "[Result]"}
# Leads every verbose/progress line so it's visually distinct from the
# final answer (which carries no marker).
_VERBOSE_PREFIX = "💭 "
# Agents that are NOT a delegation target — their output needs no tag (the
# orchestrator is the assistant the user normally talks to; `router` is the
# platform). Any other producer means the session is delegated to it.
_UNTAGGED_AGENTS = frozenset({ORCHESTRATOR_AGENT_ID, "router"})


def _agent_tag(agent_id: str | None) -> str:
    """`"[Research Agent] "` for a delegate, `""` otherwise. Prettifies the
    agent_id (underscores → spaces, title case) so the user sees which
    specialist currently holds the session."""
    if not agent_id or agent_id in _UNTAGGED_AGENTS:
        return ""
    return f"[{agent_id.replace('_', ' ').title()} Agent] "
# Delegation transition tools read better as plain phrases than as a raw
# `[Tool] hand_off` line (they're terminal tools, not ordinary dispatches).
_TRANSITION_PHRASE = {
    "hand_off": "Delegating to a specialist",
    "end_delegation": "Handing back to the assistant",
}


def _render_progress(lp: dict) -> str:
    """Format one `LoopProgress` payload into a friendly verbose-mode line.

    - `thinking` heartbeat (no detail) → `Thinking…`; with the model's
      reasoning → `(…<reasoning>)`.
    - `tool_call` / `tool_result` → `[Tool]/[Result] <tool> (<detail>)`, the
      `call_` peer-tool prefix stripped for readability.
    - the delegation transition tools (`hand_off` / `end_delegation`) →
      `Delegating to a specialist…` / `Handing back to the assistant…`.
    - anything else falls back to its detail or kind.
    """
    kind = lp.get("kind", "")
    detail = lp.get("detail")
    if kind == "thinking":
        if not detail:
            return "Thinking…"
        lead = "" if detail.startswith("…") else "…"
        return f"({lead}{detail})"
    phrase = _TRANSITION_PHRASE.get(lp.get("tool") or "") if kind == "tool_call" else None
    if phrase:
        return f"{phrase}… ({detail})" if detail else f"{phrase}…"
    label = _KIND_LABEL.get(kind)
    if label:
        name = (lp.get("tool") or "").removeprefix("call_") or "tool"
        head = f"{label} {name}"
        return f"{head} ({detail})" if detail else head
    return detail or kind or "…"



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

    async def handle_update(
        self,
        chat_id: str,
        text: str,
        attachments: list[tuple[str, str]] | None = None,
    ) -> None:
        """Entry point for one inbound message (text and/or files)."""
        text = text.strip()
        # `/v` one-shot verbose prefix, stripped BEFORE slash handling so
        # `/v /register` still routes to /register ([channel.md] §6).
        one_shot_verbose = False
        if text == "/v" or text.startswith("/v "):
            one_shot_verbose = True
            text = text[3:].strip()
        if text.startswith("/"):
            await self._handle_command(chat_id, text)
            return
        if not text and not attachments:
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

        # Effective verbose: /v one-shot > user_config.verbose_default > false.
        verbose = one_shot_verbose or bool(cfg and cfg.verbose_default)
        await self._dispatch_turn(
            chat_id, user_id, session_id, text, attachments or [], verbose=verbose
        )

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
        elif cmd == "/password":
            await self._cmd_password(chat_id)
        elif cmd == "/config":
            await self._cmd_agent(chat_id, CONFIG_AGENT_ID, "message",
                                  arg or "Show my current settings.")
        elif cmd == "/cron":
            await self._cmd_agent(chat_id, CONFIG_AGENT_ID, "cron",
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
            async with self._typing(chat_id):
                result = await self._dispatcher.await_root_result(
                    task_id, timeout_s=self._result_timeout_s
                )
        except Exception:  # noqa: BLE001
            logger.exception("command_dispatch_failed",
                             extra={"event": "command_dispatch_failed", "cmd": mode})
            await self._telegram.send_message(chat_id=chat_id, text=_DISPATCH_FAILED)
            return
        # Surface a failed task instead of masking it as "Done." — a None
        # output on a FAILED result would otherwise read as success.
        if result.status != TaskStatus.SUCCEEDED:
            logger.warning("command_task_failed",
                           extra={"event": "command_task_failed", "cmd": mode,
                                  "status": str(result.status)})
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

    async def _cmd_password(self, chat_id: str) -> None:
        """Mint a one-time password-setup token for the user ([channel.md] §6)
        so they can set a password for the (future) web app login."""
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=REGISTER_PROMPT)
            return
        if self._credentials is None:
            await self._telegram.send_message(chat_id=chat_id, text=_UNAVAILABLE)
            return
        try:
            token = await self._credentials.mint_password_reset_token(user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "password_mint_failed", extra={"event": "password_mint_failed"}
            )
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Couldn't create a password-setup link. Please try again.",
            )
            return
        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "Your one-time password-setup token (expires shortly):\n"
                f"{token}"
            ),
        )

    def _progress_callback(self, chat_id: str):  # noqa: ANN202
        """A per-frame renderer for verbose mode — one Telegram message per
        structured `LoopProgress` frame ([channel.md] §5)."""
        async def _cb(pf) -> None:  # noqa: ANN001
            lp = (pf.metadata or {}).get(LOOP_PROGRESS_KEY)
            if not lp:
                return
            # marker → (delegate tag, if any) → the rendered line. The tag is
            # per-frame: the orchestrator's own lines stay untagged; a
            # specialist's lines show it holds the session.
            text = f"{_VERBOSE_PREFIX}{_agent_tag(pf.agent_id)}{_render_progress(lp)}"
            await self._telegram.send_message(chat_id=chat_id, text=text)
        return _cb

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: str):  # noqa: ANN202
        """Keep Telegram's "typing…" indicator alive while a turn runs. The
        status auto-clears after ~5s, so a background loop refreshes it.
        Best-effort: a client without `send_chat_action`, or a transient
        send error, simply shows no indicator and never breaks the turn."""
        send = getattr(self._telegram, "send_chat_action", None)
        if send is None:
            yield
            return

        async def _keepalive() -> None:
            while True:
                try:
                    await send(chat_id=chat_id, action="typing")
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(_TYPING_REFRESH_S)

        task = asyncio.create_task(_keepalive())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _dispatch_turn(
        self,
        chat_id: str,
        user_id: str,
        session_id: str,
        text: str,
        attachments: list[tuple[str, str]] | None = None,
        *,
        verbose: bool = False,
    ) -> None:
        """Serialize on the session, write the user turn, inject the task,
        await the result, and relay it."""
        async with self._session_lock(session_id):
            # Routing: the delegate during an active delegation, else the
            # orchestrator.
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

            # Inbound files: save to the session stash + record a (T,T)
            # history row BEFORE dispatch so the agent discovers them
            # ([channel.md] §7). agent_id = the dispatch target.
            for tg_file_id, filename in attachments or []:
                await self._save_inbound_file(
                    user_id, session_id, dest, tg_file_id, filename
                )

            async with self._pool.acquire() as conn:
                # The channel is the sole writer of user turns, written
                # verbatim BEFORE dispatch so the agent's reload sees it.
                # (Skip an empty turn for a file-only message — the file
                # row above is the user input.)
                if text:
                    await queries.append_history(
                        conn,
                        session_id=session_id,
                        agent_id=dest,
                        role="user",
                        message=text,
                    )

            prompt = text or "(the user sent a file — see the attached file.)"
            try:
                task_id = await self._dispatcher.spawn_root_for_user(
                    dest,
                    MessagePayload(prompt=prompt),
                    user_id=user_id,
                    session_id=session_id,
                    mode=mode,
                )
                # Record the in-flight task so /stop can cancel it.
                self._current_task[chat_id] = (user_id, task_id)
                async with self._typing(chat_id):
                    result = await self._dispatcher.await_root_result(
                        task_id, timeout_s=self._result_timeout_s,
                        on_progress=self._progress_callback(chat_id) if verbose else None,
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
            # Tag the final reply with the specialist when the session is
            # delegated (producer = result.agent_id), so it's clear who
            # answered ([delegation.md] §2).
            reply_text = f"{_agent_tag(result.agent_id)}{reply}" if reply else "(no response)"
            await self._telegram.send_message(chat_id=chat_id, text=reply_text)

            # Outbound files: the agent returned file-store NAMES; resolve
            # each + send the bytes ([channel.md] §7).
            out_files = list(result.output.files) if result.output else []
            for name in out_files:
                await self._send_outbound_file(chat_id, user_id, session_id, name)

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

    async def _save_inbound_file(
        self, user_id: str, session_id: str, dest: str,
        tg_file_id: str, filename: str,
    ) -> None:
        """Download a Telegram attachment → session stash → (T,T) history
        row. Best-effort: a file failure never breaks the turn."""
        if self._credentials is None:
            return
        try:
            data = await self._telegram.download_file(tg_file_id)
            saved = await self._credentials.store_named_file(
                user_id=user_id, session_id=session_id, filename=filename, data=data,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "inbound_file_failed", extra={"event": "inbound_file_failed"}
            )
            return
        async with self._pool.acquire() as conn:
            await queries.append_history(
                conn, session_id=session_id, agent_id=dest, role="user",
                message=f"user-attached file saved as {saved}",
                incumbent=True, hidden=True,
            )

    async def _send_outbound_file(
        self, chat_id: str, user_id: str, session_id: str, name: str
    ) -> None:
        await send_named_file(
            telegram=self._telegram, credentials=self._credentials,
            chat_id=chat_id, user_id=user_id, session_id=session_id, name=name,
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
