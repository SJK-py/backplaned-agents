"""chatbot.kakao_gateway — the KakaoTalk inbound message engine.

A thin transport adapter over the shared `ChannelCore` (per-session lock,
dispatch, result relay) — the same engine the Telegram `ChatbotGateway`
uses. What differs is *delivery*: KakaoTalk gives one single-use
`callbackUrl` per webhook (~1 min TTL), so a turn that outlives the
callback deadline is parked and delivered on the user's next touch
([../../../docs/design/kakao-channel.md] §6–7).

State machine per inbound job:

  * a `[확인]`/`[중지]` quick-reply  → poll / cancel the parked turn.
  * a `/command`                     → the command set (subset in this PR).
  * any message, chat idle           → start a turn, race it against the
    callback deadline: deliver in-time, else post a "still working" status
    + buttons and park the rest.
  * any message, a turn pending      → "still working" (one turn per chat).
  * any message, a result ready      → deliver the parked answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bp_agents.agents.chatbot.kakao_client import KakaoClient
from bp_agents.agents.chatbot.kakao_files import detect_image_mime, egress_key
from bp_agents.agents.chatbot.kakao_registry import KakaoTaskRegistry
from bp_agents.channel import ChannelCore, agent_tag
from bp_agents.db import queries

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.agents.chatbot.credentials import ChannelCredentials
    from bp_agents.agents.chatbot.gateway import RootDispatcher
    from bp_agents.agents.chatbot.kakao_client import KakaoJob
    from bp_agents.agents.chatbot.kakao_files import R2FileEgress
    from bp_agents.settings import SuiteSettings


@dataclass
class TurnReply:
    """A computed turn's user-facing output: the reply text and any outbound
    images (presigned url, alt) to render as Kakao simpleImage bubbles."""

    text: str
    images: list[tuple[str, str]]

logger = logging.getLogger(__name__)

PLATFORM = "kakao"
CHANNEL = "chatbot_kakao"

# Quick-reply button labels (also the `messageText` they echo back as the
# next utterance).
CHECK_LABEL = "확인"
STOP_LABEL = "중지"

# Kakao's callback TTL is ~60s; if a pulled job is already older than this
# (the agent was down), its callback is dead — process it park-only so the
# user turn is recorded and answered on next touch ([kakao-channel.md] §7).
_CALLBACK_TTL_S = 60.0
_CALLBACK_MARGIN_S = 5.0

# User-facing scaffolding text (Korean, matching the relay's "처리 중…").
_WORKING_TEXT = (
    "아직 작업 중이에요. 끝나면 알려드릴게요 — [확인]을 눌러 결과를 "
    "확인하거나 [중지]로 멈출 수 있어요."
)
_STILL_WORKING_TEXT = "아직 작업 중이에요. 잠시 후 [확인]을 눌러 주세요."
_STOPPED_TEXT = "중지했어요."
_NOTHING_RUNNING_TEXT = "지금 진행 중인 작업이 없어요."
_DISPATCH_FAILED_TEXT = "죄송해요, 처리 중 문제가 생겼어요. 다시 시도해 주세요."
_NO_RESPONSE_TEXT = "(응답 없음)"
_REGISTER_PROMPT = (
    "아직 등록되지 않았어요. /register 를 보내 접근을 요청하면 "
    "관리자가 검토할게요. (이메일을 함께 보내도 돼요: /register you@example.com)"
)
_NO_SESSION_TEXT = "활성화된 대화가 없어요. 관리자에게 문의해 주세요."
_UNAVAILABLE_TEXT = "지금은 이 명령을 사용할 수 없어요."
_ALREADY_REGISTERED_TEXT = "이미 등록되어 있어요. 그냥 메시지를 보내 주세요!"
_REGISTER_SUBMITTED_TEXT = (
    "등록 요청을 접수했어요. 관리자 승인 후 바로 도와드릴게요."
)
_REGISTER_FAILED_TEXT = "등록 요청에 실패했어요. 다시 시도해 주세요."
_NEW_STARTED_TEXT = "새 대화를 시작했어요."
_UNKNOWN_CMD_TEXT = "지원하지 않는 명령이에요. /help 를 입력해 보세요."
HELP_TEXT = (
    "개인 비서예요. 메시지를 보내면 도와드릴게요.\n\n"
    "명령어:\n"
    "/register — 접근 요청 (관리자 승인)\n"
    "/new — 새 대화 시작\n"
    "/stop — 진행 중인 작업 중지\n"
    "/help — 명령어 보기"
)


class KakaoGateway:
    """Handles one pulled KakaoTalk job end-to-end. One instance per
    process; the per-session locks and the parked-turn registry are shared."""

    def __init__(
        self,
        *,
        dispatcher: RootDispatcher,
        pool: asyncpg.Pool,
        client: KakaoClient,
        registry: KakaoTaskRegistry,
        settings: SuiteSettings,
        credentials: ChannelCredentials | None = None,
        egress: R2FileEgress | None = None,
        redis: Any | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._credentials = credentials
        self._egress = egress
        self._pool = pool
        self._deadline = settings.kakao_callback_deadline_s
        self._core = ChannelCore(
            dispatcher=dispatcher,
            pool=pool,
            delegatable_agents=frozenset(settings.delegatable_agents),
            result_timeout_s=settings.dispatch_result_timeout_s,
            fire_memory=True,
            redis=redis,
        )
        # Background (parked) turn tasks, tracked for shutdown cleanup.
        self._turns: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        """Cancel any in-flight parked turns (best-effort, on shutdown)."""
        for t in list(self._turns):
            t.cancel()

    # -- entry point ----------------------------------------------------

    async def handle_job(self, job: KakaoJob) -> None:
        """Process one pulled job. Pre-dedupe infra errors (Redis) propagate
        so the consumer leaves the message unacked for redelivery; everything
        after the dedupe mark is best-effort (a failure delivers an apology
        and is acked, since redelivery would just be deduped away)."""
        body = job.body
        chat_id = body.get("chat_id")
        callback_url = body.get("callback_url")
        if not chat_id or not callback_url:
            logger.warning(
                "kakao_job_missing_fields",
                extra={"event": "kakao_job_missing_fields", "kakao.msg_id": job.msg_id},
            )
            return

        if job.msg_id and await self._registry.seen(job.msg_id):
            logger.info(
                "kakao_job_duplicate",
                extra={"event": "kakao_job_duplicate", "kakao.msg_id": job.msg_id},
            )
            return

        utterance = (body.get("utterance") or "").strip()
        # `/v` one-shot verbose prefix, stripped before slash handling (Kakao
        # has no progress stream, so verbose only affects the LLM, not the UI).
        if utterance == "/v" or utterance.startswith("/v "):
            utterance = utterance[3:].strip()

        try:
            if utterance in (CHECK_LABEL, STOP_LABEL):
                await self._handle_poll(chat_id, callback_url, utterance)
            elif utterance.startswith("/"):
                await self._handle_command(chat_id, callback_url, utterance)
            else:
                await self._handle_message(chat_id, callback_url, utterance, body)
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_handle_error",
                extra={"event": "kakao_handle_error", "kakao.msg_id": job.msg_id},
            )
            with contextlib.suppress(Exception):
                await self._client.post_callback(callback_url, _DISPATCH_FAILED_TEXT)

    # -- quick-reply poll / stop ----------------------------------------

    def _poll_buttons(self) -> list[tuple[str, str]]:
        return [(CHECK_LABEL, CHECK_LABEL), (STOP_LABEL, STOP_LABEL)]

    @staticmethod
    def _decode_images(images_json: str) -> list[tuple[str, str]]:
        if not images_json:
            return []
        try:
            return [(u, a) for u, a in json.loads(images_json)]
        except Exception:  # noqa: BLE001
            return []

    async def _deliver(self, callback_url: str, reply: TurnReply) -> None:
        await self._client.post_callback(
            callback_url, reply.text, images=reply.images or None
        )

    async def _deliver_ready(self, chat_id: str, callback_url: str) -> bool:
        """Deliver a parked (ready) answer on `callback_url` if one exists."""
        raw = await self._registry.take_ready(chat_id)
        if raw is None:
            return False
        text, images_json = raw
        await self._client.post_callback(
            callback_url, text, images=self._decode_images(images_json) or None
        )
        return True

    async def _handle_poll(
        self, chat_id: str, callback_url: str, utterance: str
    ) -> None:
        if utterance == STOP_LABEL:
            await self._stop(chat_id, callback_url)
            return
        # [확인] — deliver a ready answer, else report still-working / idle.
        if await self._deliver_ready(chat_id, callback_url):
            return
        turn = await self._registry.get_turn(chat_id)
        if turn and turn.get("state") == "pending":
            await self._client.post_callback(
                callback_url, _STILL_WORKING_TEXT, quick_replies=self._poll_buttons()
            )
            return
        await self._client.post_callback(callback_url, _NOTHING_RUNNING_TEXT)

    async def _stop(self, chat_id: str, callback_url: str) -> None:
        turn = await self._registry.get_turn(chat_id)
        if not turn:
            await self._client.post_callback(callback_url, _NOTHING_RUNNING_TEXT)
            return
        # The work finished before the user pressed stop — deliver it.
        if turn.get("state") == "ready":
            if not await self._deliver_ready(chat_id, callback_url):
                await self._client.post_callback(callback_url, _NOTHING_RUNNING_TEXT)
            return
        await self._registry.mark_stopped(chat_id)
        task_id, user_id = turn.get("task_id"), turn.get("user_id")
        if self._credentials is not None and task_id and user_id:
            try:
                await self._credentials.cancel_task(user_id=user_id, task_id=task_id)
            except Exception:  # noqa: BLE001
                logger.exception("kakao_cancel_failed", extra={"event": "kakao_cancel_failed"})
        await self._client.post_callback(callback_url, _STOPPED_TEXT)

    # -- normal message → turn lifecycle --------------------------------

    async def _handle_message(
        self, chat_id: str, callback_url: str, text: str, body: dict
    ) -> None:
        # A ready answer takes priority: deliver it on this callback.
        if await self._deliver_ready(chat_id, callback_url):
            return
        turn = await self._registry.get_turn(chat_id)
        if turn and turn.get("state") == "pending":
            # One turn per chat — don't start a second alongside the running one.
            await self._client.post_callback(
                callback_url, _STILL_WORKING_TEXT, quick_replies=self._poll_buttons()
            )
            return

        resolved = await self._resolve_session(chat_id, callback_url)
        if resolved is None:
            return  # register / no-session prompt already sent
        user_id, session_id = resolved
        await self._run_turn(chat_id, callback_url, user_id, session_id, text, body)

    async def _run_turn(
        self,
        chat_id: str,
        callback_url: str,
        user_id: str,
        session_id: str,
        text: str,
        body: dict,
    ) -> None:
        """Start the turn and race it against the callback budget: deliver
        in-time, otherwise post a status + park for the next touch."""
        compute = asyncio.create_task(
            self._compute_turn(chat_id, user_id, session_id, text, body)
        )
        self._turns.add(compute)
        compute.add_done_callback(self._turns.discard)

        budget = self._callback_budget(body)
        if budget > 0:
            done, _ = await asyncio.wait({compute}, timeout=budget)
        else:
            done = set()  # callback already dead → park-only, no race

        if compute in done:
            await self._registry.clear(chat_id)
            try:
                reply = compute.result()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_turn_error",
                    extra={"event": "kakao_turn_error", "bp.session_id": session_id},
                )
                await self._client.post_callback(callback_url, _DISPATCH_FAILED_TEXT)
                return
            await self._deliver(callback_url, reply)
            return

        # Overran (or stale callback) — park the rest, status if we still can.
        if budget > 0:
            with contextlib.suppress(Exception):
                await self._client.post_callback(
                    callback_url, _WORKING_TEXT, quick_replies=self._poll_buttons()
                )
        compute.add_done_callback(lambda t: self._schedule_park(chat_id, t))

    def _callback_budget(self, body: dict) -> float:
        """Seconds we may spend before the callback dies: the deadline,
        capped by the callback's remaining TTL given when the relay enqueued
        it. ≤0 means the callback is already (near) dead → park-only."""
        received_at = body.get("received_at")
        if not received_at:
            return self._deadline
        age = max(0.0, time.time() - float(received_at) / 1000.0)
        return min(self._deadline, _CALLBACK_TTL_S - _CALLBACK_MARGIN_S - age)

    def _schedule_park(self, chat_id: str, task: asyncio.Task) -> None:
        """Done-callback (sync) → schedule the async park of a finished turn."""
        t = asyncio.create_task(self._park(chat_id, task))
        self._turns.add(t)
        t.add_done_callback(self._turns.discard)

    async def _park(self, chat_id: str, task: asyncio.Task) -> None:
        turn = await self._registry.get_turn(chat_id)
        stopped = bool(turn and turn.get("stopped"))
        try:
            reply = task.result()
        except asyncio.CancelledError:
            return  # process shutting down — leave state for next touch
        except Exception:  # noqa: BLE001
            logger.exception("kakao_turn_error", extra={"event": "kakao_turn_error"})
            reply = TurnReply(_DISPATCH_FAILED_TEXT, [])
        if stopped:
            await self._registry.clear(chat_id)  # user already saw the stop ack
            return
        await self._registry.store_ready(
            chat_id, reply.text, json.dumps(reply.images)
        )

    async def _compute_turn(
        self, chat_id: str, user_id: str, session_id: str, text: str, body: dict
    ) -> TurnReply:
        """Run one turn through ChannelCore under the session lock and return
        the user-facing reply (+ any outbound images). Delivery (now vs
        parked) is the caller's call."""
        reply = ""
        image_url = body.get("image_url")
        async with self._core.session_lock(session_id):
            dest, mode = await self._core.route(session_id)
            # Inbound image (before dispatch, so the agent's reload sees it).
            if image_url and self._credentials is not None:
                await self._save_inbound_image(user_id, session_id, dest, image_url)
            if text:
                await self._core.record_user_turn(session_id, dest, text)
            prompt = text or (
                "(the user sent an image — see the attached file.)"
                if image_url
                else "(the user sent a message with no text.)"
            )
            task_id = await self._core.spawn(user_id, session_id, dest, mode, prompt)
            await self._registry.set_inflight(chat_id, user_id, task_id)
            result = await self._core.await_result(task_id)

            reply = (result.output.content if result.output else "") or ""
            reply_text = (
                f"{agent_tag(result.agent_id)}{reply}" if reply else _NO_RESPONSE_TEXT
            )
            out_names = list(result.output.files) if result.output else []
            images = await self._upload_outbound(user_id, session_id, out_names)
            context_tokens = await self._core.after_result(session_id, dest, result)
            await self._core.maybe_summarize(session_id, dest, context_tokens)

        self._core.fire_memory_add(user_id, session_id, text, reply)
        self._core.fire_name_session(user_id, session_id, text)
        return TurnReply(reply_text, images)

    async def _save_inbound_image(
        self, user_id: str, session_id: str, dest: str, image_url: str
    ) -> None:
        """Fetch a Kakao-provided image url → router named store → (T,T)
        hidden history row, so the agent discovers it. Best-effort: a file
        failure never breaks the turn (mirrors the Telegram inbound path)."""
        assert self._credentials is not None
        try:
            data = await self._client.fetch_inbound_image(image_url)
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_inbound_image_fetch_failed",
                extra={"event": "kakao_inbound_image_fetch_failed"},
            )
            return
        try:
            mime = detect_image_mime(data)
            filename = f"image{mimetypes.guess_extension(mime) or '.jpg'}"
            saved = await self._credentials.store_named_file(
                user_id=user_id, session_id=session_id, filename=filename,
                data=data, mime_type=mime,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_inbound_image_store_failed",
                extra={"event": "kakao_inbound_image_store_failed"},
            )
            return
        async with self._pool.acquire() as conn:
            await queries.append_history(
                conn, session_id=session_id, agent_id=dest, role="user",
                message=f"user-attached image saved as {saved}",
                incumbent=True, hidden=True,
            )

    async def _upload_outbound(
        self, user_id: str, session_id: str, names: list[str]
    ) -> list[tuple[str, str]]:
        """Resolve produced file names → bytes → R2 presigned urls for Kakao
        to render. Images only — Kakao can't inline arbitrary documents, so a
        non-image produced file is skipped (logged). Needs R2 configured."""
        if self._egress is None or self._credentials is None or not names:
            return []
        images: list[tuple[str, str]] = []
        for name in names:
            try:
                file_id = await self._credentials.resolve_named_file(
                    user_id=user_id, session_id=session_id, name=name
                )
                if file_id is None:
                    continue
                data = await self._credentials.fetch_file(
                    user_id=user_id, file_id=file_id
                )
                mime = detect_image_mime(data, name)
                if not mime.startswith("image/"):
                    logger.info(
                        "kakao_outbound_skip_nonimage",
                        extra={"event": "kakao_outbound_skip_nonimage"},
                    )
                    continue
                url = await self._egress.put_image(
                    data, content_type=mime, key=egress_key(session_id, name)
                )
                images.append((url, name.rsplit("/", 1)[-1]))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_outbound_image_failed",
                    extra={"event": "kakao_outbound_image_failed"},
                )
        return images

    # -- identity + commands --------------------------------------------

    async def _resolve_user(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            return await queries.resolve_user_id(
                conn, platform=PLATFORM, chat_id=chat_id
            )

    async def _resolve_session(
        self, chat_id: str, callback_url: str
    ) -> tuple[str, str] | None:
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._client.post_callback(callback_url, _REGISTER_PROMPT)
            return None
        async with self._pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        session_id = cfg.default_session_id if cfg else None
        if session_id is None:
            await self._client.post_callback(callback_url, _NO_SESSION_TEXT)
            return None
        return user_id, session_id

    async def _handle_command(
        self, chat_id: str, callback_url: str, text: str
    ) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/help", "/start"):
            await self._client.post_callback(callback_url, HELP_TEXT)
        elif cmd == "/register":
            await self._cmd_register(chat_id, callback_url, arg)
        elif cmd == "/new":
            await self._cmd_new(chat_id, callback_url)
        elif cmd == "/stop":
            await self._stop(chat_id, callback_url)
        else:
            # /config, /cron, /delegate, /undelegate, /password land with the
            # registration-polish PR; surface a clear notice until then.
            await self._client.post_callback(callback_url, _UNKNOWN_CMD_TEXT)

    async def _cmd_register(
        self, chat_id: str, callback_url: str, email_arg: str
    ) -> None:
        if await self._resolve_user(chat_id) is not None:
            await self._client.post_callback(callback_url, _ALREADY_REGISTERED_TEXT)
            return
        if self._credentials is None:
            await self._client.post_callback(callback_url, _UNAVAILABLE_TEXT)
            return
        try:
            await self._credentials.submit_registration(
                channel=CHANNEL, external_id=chat_id, requested_email=email_arg or None
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_registration_submit_failed",
                extra={"event": "kakao_registration_submit_failed"},
            )
            await self._client.post_callback(callback_url, _REGISTER_FAILED_TEXT)
            return
        await self._client.post_callback(callback_url, _REGISTER_SUBMITTED_TEXT)

    async def _cmd_new(self, chat_id: str, callback_url: str) -> None:
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._client.post_callback(callback_url, _REGISTER_PROMPT)
            return
        if self._credentials is None:
            await self._client.post_callback(callback_url, _UNAVAILABLE_TEXT)
            return
        # Retire the previous conversation (archive + release its channel flag),
        # best-effort, then open a fresh one — mirrors the Telegram /new path.
        async with self._pool.acquire() as conn:
            cfg = await queries.get_user_config(conn, user_id)
        prev_session = cfg.default_session_id if cfg else None
        if prev_session is not None:
            try:
                await self._credentials.close_session(
                    user_id=user_id, session_id=prev_session
                )
                async with self._pool.acquire() as conn:
                    await queries.update_session_info(conn, prev_session, channel=None)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_new_close_prev_failed",
                    extra={"event": "kakao_new_close_prev_failed"},
                )
        new_session = await self._credentials.open_session(
            user_id=user_id, metadata={"kind": CHANNEL, "external_id": chat_id}
        )
        async with self._pool.acquire() as conn:
            await queries.create_session_info(
                conn, session_id=new_session, user_id=user_id,
                channel=CHANNEL, chat_id=chat_id,
            )
            await queries.set_default_session_id(
                conn, user_id=user_id, session_id=new_session
            )
        await self._registry.clear(chat_id)
        await self._client.post_callback(callback_url, _NEW_STARTED_TEXT)
