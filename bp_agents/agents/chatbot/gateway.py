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
import mimetypes
from typing import TYPE_CHECKING, Any, Protocol

from bp_agents.agents.chatbot.credentials import ChannelCredentials
from bp_agents.agents.chatbot.telegram import TelegramClient
from bp_agents.channel import (
    VERBOSE_PREFIX,
    ChannelCore,
    agent_tag,
    progress_producer,
    render_progress_line,
)
from bp_agents.common.payloads import MessagePayload
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries
from bp_protocol.types import TaskStatus

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
CHANNEL = "chatbot_telegram"

# Leading magic-byte signatures → MIME type. Telegram hands us only a
# file_id, so the stash upload would otherwise default to
# application/octet-stream — which the router refuses to inline as an
# image. Sniff the real type from the bytes (with an extension fallback)
# so inbound photos/PDFs are stored as a multimodal-supported MIME.
_MAGIC_MIME: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),
)


def _detect_mime(data: bytes, filename: str) -> str:
    """Best-effort MIME for an inbound attachment: sniff magic bytes
    first, fall back to the filename extension, then octet-stream."""
    for sig, mime in _MAGIC_MIME:
        if data.startswith(sig):
            return mime
    # RIFF....WEBP — the "WEBP" tag sits at offset 8, after the size field.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


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
CONFIG_AGENT_ID = "config"
CHATBOT_AGENT_ID = "chatbot"

# Single source of truth for the bot's commands: drives both the /help
# text and the Telegram `setMyCommands` registration (the "/" menu).
BOT_COMMANDS: list[tuple[str, str]] = [
    ("register", "request access (an admin approves it)"),
    ("link", "link this chat to your existing account (/link <token>)"),
    ("new", "start a fresh conversation"),
    ("stop", "stop the current in-progress reply"),
    ("config", "view or change your settings"),
    ("cron", "manage scheduled reminders/tasks"),
    ("delegate", "hand this chat to a specialist (e.g. /delegate research)"),
    ("undelegate", "return to the main assistant"),
    ("setdefault", "make this chat's conversation your default for reminders"),
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
    "email) to request access; an administrator will review it.\n\n"
    "Already have an account on the web or another chat? Get a token there "
    "with /password, then send /link <token> here to use it from this chat."
)
_LINK_USAGE = (
    "Send /link <token> to link this chat to your existing account. Get a "
    "token by sending /password from a chat that's already linked (or the "
    "web app)."
)
_LINK_OK = (
    "Linked — this chat now uses your existing account, with its own "
    "conversation here (separate from your other chats)."
)
_LINK_INVALID = (
    "That token is invalid or expired. Mint a fresh one with /password and "
    "try again."
)
_SETDEFAULT_OK = (
    "Done — this chat's conversation is now your default. Scheduled reminders "
    "and out-of-band messages will arrive here."
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
        redis: Any | None = None,
        delegatable_agents: frozenset[str] = frozenset(),
    ) -> None:
        self._dispatcher = dispatcher
        self._pool = pool
        self._telegram = telegram
        self._credentials = credentials
        self._result_timeout_s = result_timeout_s
        # Transport-free channel engine: routing, the per-session lock,
        # `delegated_to` maintenance, summarization, /delegate·/undelegate,
        # and `memory.add` ([channel.md], shared with the webapp frontend).
        self._core = ChannelCore(
            dispatcher=dispatcher,
            pool=pool,
            delegatable_agents=delegatable_agents,
            result_timeout_s=result_timeout_s,
            fire_memory=fire_memory,
            redis=redis,
        )
        # chat_id → (user_id, task_id) of the in-flight turn, for /stop.
        self._current_task: dict[str, tuple[str, str]] = {}

    def session_lock(self, session_id: str):  # noqa: ANN202 — async-ctx guard
        """The per-session lock, shared with the cron scheduler so its
        applied turns serialize with inbound user turns ([sessions.md] §4)."""
        return self._core.session_lock(session_id)

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

        resolved = await self._resolve_chat(chat_id)
        if resolved is None:
            await self._telegram.send_message(chat_id=chat_id, text=REGISTER_PROMPT)
            return
        user_id, cfg, session_id = resolved
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
        elif cmd == "/link":
            await self._cmd_link(chat_id, arg)
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
        elif cmd == "/delegate":
            await self._cmd_delegate(chat_id, arg)
        elif cmd == "/undelegate":
            await self._cmd_undelegate(chat_id)
        elif cmd == "/setdefault":
            await self._cmd_setdefault(chat_id)
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
        resolved = await self._resolve_chat(chat_id)
        if resolved is None:
            await self._telegram.send_message(chat_id=chat_id, text=REGISTER_PROMPT)
            return
        user_id, _cfg, session_id = resolved
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

    # ------------------------------------------------------------------
    # /delegate · /undelegate — user-driven delegation switch
    # ([delegation.md] §6: the deterministic, channel-driven path).
    # ------------------------------------------------------------------

    async def _resolve_chat(
        self, chat_id: str
    ) -> tuple[str, Any, str | None] | None:
        """`(user_id, cfg, session_id)` for a registered chat, or None if the
        chat is unmapped. `session_id` is the chat's OWN current session
        (`mapping.session_id`), falling back to the user's `default_session_id`
        (the cron fallback) only until the chat has one of its own; it may
        still be None when the user has no session at all."""
        async with self._pool.acquire() as conn:
            mapping = await queries.get_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id
            )
            if mapping is None:
                return None
            cfg = await queries.get_user_config(conn, mapping.user_id)
        session_id = mapping.session_id or (cfg.default_session_id if cfg else None)
        return mapping.user_id, cfg, session_id

    async def _resolve_session(self, chat_id: str) -> tuple[str, str] | None:
        """`(user_id, session_id)` for a registered chat, or None after
        sending the appropriate prompt."""
        resolved = await self._resolve_chat(chat_id)
        if resolved is None:
            await self._telegram.send_message(chat_id=chat_id, text=REGISTER_PROMPT)
            return None
        user_id, _cfg, session_id = resolved
        if session_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=_NO_SESSION)
            return None
        return user_id, session_id

    async def _cmd_delegate(self, chat_id: str, arg: str) -> None:
        resolved = await self._resolve_session(chat_id)
        if resolved is None:
            return
        user_id, session_id = resolved
        target = arg.split(maxsplit=1)[0] if arg.split(maxsplit=1) else ""
        msg = await self._core.delegate(user_id, session_id, target)
        await self._telegram.send_message(chat_id=chat_id, text=msg)

    async def _cmd_undelegate(self, chat_id: str) -> None:
        resolved = await self._resolve_session(chat_id)
        if resolved is None:
            return
        user_id, session_id = resolved
        msg = await self._core.undelegate(user_id, session_id)
        await self._telegram.send_message(chat_id=chat_id, text=msg)

    async def _cmd_setdefault(self, chat_id: str) -> None:
        """Point the user's `default_session_id` (the cron fallback / async
        delivery target) at THIS chat's current session. For a multi-channel
        user this picks which channel's conversation reminders land in."""
        async with self._pool.acquire() as conn:
            mapping = await queries.get_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id
            )
            if mapping is None:
                await self._telegram.send_message(
                    chat_id=chat_id, text=REGISTER_PROMPT
                )
                return
            if mapping.session_id is None:
                await self._telegram.send_message(chat_id=chat_id, text=_NO_SESSION)
                return
            await queries.set_default_session_id(
                conn, user_id=mapping.user_id, session_id=mapping.session_id
            )
        await self._telegram.send_message(chat_id=chat_id, text=_SETDEFAULT_OK)

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

    async def _cmd_link(self, chat_id: str, arg: str) -> None:
        """Bind this (unmapped) chat to a pre-existing account by verifying a
        password-reset token minted on a channel the user is already on
        ([channel.md] §6). The token proves ownership; on success we map
        (PLATFORM, chat_id) → the returned user_id and open a fresh session for
        THIS chat, so it shares the account (memory/files) but keeps its own
        conversation instead of interleaving into another channel's thread."""
        if await self._resolve_user(chat_id) is not None:
            await self._telegram.send_message(
                chat_id=chat_id, text=_ALREADY_REGISTERED
            )
            return
        token = arg.split(maxsplit=1)[0] if arg else ""
        if not token:
            await self._telegram.send_message(chat_id=chat_id, text=_LINK_USAGE)
            return
        if self._credentials is None:
            await self._telegram.send_message(chat_id=chat_id, text=_UNAVAILABLE)
            return
        try:
            # Consumes the token AND grants this channel's service principal
            # serviced_by over the account (so /password recovery + scheduled
            # delivery work from here on) — router /v1/auth/link-channel.
            user_id = await self._credentials.link_channel(token=token)
        except Exception:  # noqa: BLE001
            logger.exception(
                "link_verify_failed", extra={"event": "link_verify_failed"}
            )
            await self._telegram.send_message(chat_id=chat_id, text=_DISPATCH_FAILED)
            return
        if user_id is None:
            await self._telegram.send_message(chat_id=chat_id, text=_LINK_INVALID)
            return
        async with self._pool.acquire() as conn:
            await queries.upsert_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id, user_id=user_id
            )
        # Open this chat's OWN session so it doesn't land in the account's
        # other-channel conversation. Best-effort: if it fails, the mapping
        # stands and the chat falls back to the default until its first /new.
        try:
            new_session = await self._credentials.open_session(
                user_id=user_id,
                metadata={"kind": CHANNEL, "external_id": chat_id},
            )
            async with self._pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id=new_session, user_id=user_id,
                    channel=CHANNEL, chat_id=chat_id,
                )
                await queries.set_mapping_session_id(
                    conn, platform=PLATFORM, chat_id=chat_id, session_id=new_session,
                )
                # Adopt this Telegram session as the cron/notification
                # fallback when the account has none — OR when the current
                # default lives on a non-Telegram channel. Telegram is the
                # only channel that delivers scheduled-task results
                # out-of-band ([cron.md] §6), so a web-first account whose
                # default is a (non-pushable) webapp session should hand the
                # default to Telegram on link. An existing Telegram default
                # from another chat is left untouched.
                cfg = await queries.get_user_config(conn, user_id)
                cur_default = (
                    await queries.get_session_info(conn, cfg.default_session_id)
                    if cfg and cfg.default_session_id else None
                )
                if cur_default is None or cur_default.channel != CHANNEL:
                    await queries.set_default_session_id(
                        conn, user_id=user_id, session_id=new_session,
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "link_open_session_failed",
                extra={"event": "link_open_session_failed"},
            )
        await self._telegram.send_message(chat_id=chat_id, text=_LINK_OK)

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
        # Retire THIS chat's previous conversation (its own session, NOT the
        # shared default — another channel may be using that): close it on the
        # router (archive) and release its channel-origin flag so the webapp
        # can reopen/remove it ([webapp.md] §4). Best-effort — a failure here
        # must not block starting the new conversation.
        async with self._pool.acquire() as conn:
            mapping = await queries.get_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id
            )
        prev_session = mapping.session_id if mapping else None
        if prev_session is not None:
            try:
                await self._credentials.close_session(
                    user_id=user_id, session_id=prev_session
                )
                async with self._pool.acquire() as conn:
                    await queries.update_session_info(
                        conn, prev_session, channel=None
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "new_close_prev_failed",
                    extra={"event": "new_close_prev_failed"},
                )

        new_session = await self._credentials.open_session(
            user_id=user_id,
            metadata={"kind": CHANNEL, "external_id": chat_id},
        )
        async with self._pool.acquire() as conn:
            await queries.create_session_info(
                conn, session_id=new_session, user_id=user_id,
                channel=CHANNEL, chat_id=chat_id,
            )
            # This chat now rides the fresh session; it also becomes the cron
            # fallback (the re-pointing rule — the newest conversation wins).
            await queries.set_mapping_session_id(
                conn, platform=PLATFORM, chat_id=chat_id, session_id=new_session,
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
            text = f"{VERBOSE_PREFIX}{agent_tag(progress_producer(pf))}{render_progress_line(lp)}"
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
        await the result, and relay it. The channel logic (routing,
        delegated_to maintenance, summarization, memory) lives in
        `ChannelCore`; this keeps only Telegram I/O + /stop tracking."""
        reply = ""
        async with self._core.session_lock(session_id):
            dest, mode = await self._core.route(session_id)

            # Inbound files: save to the session stash + record a (T,T)
            # history row BEFORE dispatch so the agent discovers them
            # ([channel.md] §7). agent_id = the dispatch target.
            for tg_file_id, filename in attachments or []:
                await self._save_inbound_file(
                    user_id, session_id, dest, tg_file_id, filename
                )

            # The channel is the sole writer of user turns, written verbatim
            # BEFORE dispatch so the agent's reload sees it. (Skip an empty
            # turn for a file-only message — the file row above is the input.)
            if text:
                await self._core.record_user_turn(session_id, dest, text)

            prompt = text or "(the user sent a file — see the attached file.)"
            try:
                task_id = await self._core.spawn(
                    user_id, session_id, dest, mode, prompt
                )
                # Record the in-flight task so /stop can cancel it.
                self._current_task[chat_id] = (user_id, task_id)
                async with self._typing(chat_id):
                    result = await self._core.await_result(
                        task_id,
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
            reply_text = f"{agent_tag(result.agent_id)}{reply}" if reply else "(no response)"
            await self._telegram.send_message(chat_id=chat_id, text=reply_text)

            # Outbound files: the agent returned file-store NAMES; resolve
            # each + send the bytes ([channel.md] §7).
            out_files = list(result.output.files) if result.output else []
            for name in out_files:
                await self._send_outbound_file(chat_id, user_id, session_id, name)

            # delegated_to maintenance + post-turn summarization, still inside
            # the session lock so they serialize with the next turn
            # ([delegation.md] §2, [sessions.md] §3.1).
            context_tokens = await self._core.after_result(session_id, dest, result)
            await self._core.maybe_summarize(session_id, dest, context_tokens)

        # memory.add is per-USER (not per-session) and a multi-LLM-call
        # extraction, so it runs OUTSIDE the session lock, fire-and-forget
        # ([overview.md] §2.2). No-op unless `fire_memory` and a real reply.
        self._core.fire_memory_add(user_id, session_id, text, reply)
        # Title the conversation from its first message (first turn only).
        self._core.fire_name_session(user_id, session_id, text)

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
                mime_type=_detect_mime(data, filename),
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
