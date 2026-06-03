"""chatbot.kakao_gateway — the KakaoTalk inbound message engine.

A thin transport adapter over the shared `ChannelCore` (per-session lock,
dispatch, result relay) — the same engine the Telegram `ChatbotGateway`
uses. What differs is *delivery*: KakaoTalk gives one single-use
`callbackUrl` per webhook (~1 min TTL), so a turn that outlives the
callback deadline is parked and delivered on the user's next touch
([../../../docs/design/kakao-channel.md] §6–7).

State machine per inbound job:

  * the `[확인]`/`[중지]` buttons     → send `/check` / `/stop` (poll / cancel
    the parked turn); the Korean labels are display-only, so typing the bare
    words isn't mistaken for a poll.
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
from bp_agents.channel import ChannelCore, agent_tag, progress_producer
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries
from bp_protocol.types import TaskStatus

# The root agent — its tool steps are reported without an agent prefix (it's
# the default "assistant" the user is talking to).
ORCHESTRATOR_AGENT_ID = "orchestrator"

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
CONFIG_AGENT_ID = "config"

# Quick-reply buttons: a Korean `label` the user sees, paired with the
# `messageText` it sends — a slash command, NOT the visible word, so a user
# who simply types "확인"/"중지" starts a normal turn instead of being
# hijacked into the poll/stop path.
CHECK_LABEL = "확인"
STOP_LABEL = "중지"
_CHECK_CMD = "/check"
_STOP_CMD = "/stop"

# Safety margin subtracted from the callback's remaining TTL when sizing the
# delivery budget, so we never start a delivery that would land right as the
# callback expires. (The TTL itself is `settings.kakao_callback_ttl_s`.)
_CALLBACK_MARGIN_S = 5.0

# User-facing scaffolding text (Korean, matching the relay's "처리 중…").
_WORKING_TEXT = (
    "⏳ 아직 작업 중이에요. [확인]을 눌러 결과를 "
    "다시 확인하거나 [중지]로 멈출 수 있어요."
)
_STILL_WORKING_TEXT = "⏳ 아직 작업 중이에요. 잠시 후 [확인]을 눌러 주세요."
_STOPPED_TEXT = "🛑 중지했어요."
_NOTHING_RUNNING_TEXT = "지금 진행 중인 작업이 없어요."
_DISPATCH_FAILED_TEXT = "😥 죄송해요, 처리 중 문제가 생겼어요. 다시 시도해 주세요."
_NO_RESPONSE_TEXT = "(응답 없음)"
_PROGRESS_FALLBACK_TEXT = "처리 중이에요."
# Header for non-image produced files — Kakao can't inline documents, so they
# ride as presigned download links in the reply text (short-lived URLs).
_FILE_LINKS_INTRO = "📎 첨부 파일이에요 (링크는 곧 만료돼요):"


def _format_progress(lp: dict, producer: str | None) -> str:
    """Render a `LoopProgress` (a `tool_call` / `tool_result`) as a one-line,
    parenthesised Korean status, so a "still working" reply tells the user WHAT
    the turn is doing. A `call_<agent>` peer tool reads as calling / analysing
    "<agent> 에이전트"; any other tool reads as "<agent> 에이전트 - <tool>…",
    the agent prefix dropped for the orchestrator (the default assistant)."""
    kind = lp.get("kind")
    tool = lp.get("tool") or ""
    if tool.startswith("call_"):
        agent = tool.removeprefix("call_") or "에이전트"
        if kind == "tool_call":
            body = f"{agent} 에이전트를 호출하여 처리 중이에요."
        elif kind == "tool_result":
            body = f"{agent} 에이전트의 결과 보고를 분석 중이에요."
        else:
            body = _PROGRESS_FALLBACK_TEXT
    else:
        # "<agent> 에이전트 - " names the specialist that ran the tool; dropped
        # for the orchestrator (the user's default assistant). The dash keeps
        # the agent clear of the tool without needing a 가/이 particle to agree
        # with the agent_id's final sound.
        prefix = (
            f"{producer} 에이전트 - "
            if producer and producer != ORCHESTRATOR_AGENT_ID
            else ""
        )
        if kind == "tool_call":
            body = f"{prefix}{tool} 도구를 이용하여 처리 중이에요."
        elif kind == "tool_result":
            body = f"{prefix}{tool} 도구를 사용하고 결과를 분석 중이에요."
        else:
            body = _PROGRESS_FALLBACK_TEXT
    return f"({body})"


def _with_progress(base: str, turn: dict | None) -> str:
    """Append the in-flight turn's latest tool-progress line to a status text,
    when one has been recorded. Shared by every "still working" reply — the
    50 s overrun status, the `/check` poll, and the busy-claim status."""
    progress = (turn or {}).get("progress")
    return f"{base}\n{progress}" if progress else base
_REGISTER_PROMPT = (
    "👋 사용자 등록이 필요해요.\n\n"
    "/register 를 보내 접근을 요청하면 관리자가 검토 후 사용 가능해요. (이메일을 함께 보내주세요: /register you@example.com)\n\n"
    "이미 등록된 사용자라면, 등록된 채팅(또는 웹)에서 /password 로 토큰을 받은 다음, "
    "여기에서 /link <토큰> 을 보내서 이 채팅을 기존 계정에 연결할 수 있어요."
)
_LINK_USAGE_TEXT = (
    "/link <토큰> 형식으로 보내 이 채팅을 기존 계정에 연결하세요.\n"
    "이미 등록된 채팅(또는 웹)에서 /password 로 토큰을 받을 수 있어요."
)
_LINK_OK_TEXT = (
    "🔗 연결됐어요! 이제 이 채팅이 기존 계정을 사용해요. (다른 채팅과는 별개의 대화를 가져요.)"
)
_LINK_INVALID_TEXT = (
    "토큰이 올바르지 않거나 만료됐어요. /password 로 새 토큰을 받아 다시 시도해 주세요."
)
_LINK_FAILED_TEXT = "연결에 실패했어요. 잠시 후 다시 시도해 주세요."
_NO_SESSION_TEXT = "활성화된 대화가 없어요. 관리자에게 문의해 주세요."
_UNAVAILABLE_TEXT = "지금은 이 명령을 사용할 수 없어요."
_ALREADY_REGISTERED_TEXT = "🙂 이미 등록되어 있어요. 그냥 메시지를 보내 주세요!"
_REGISTER_SUBMITTED_TEXT = (
    "✅ 등록 요청을 접수했어요. 관리자 승인 후 사용이 가능해요."
)
_REGISTER_FAILED_TEXT = "등록 요청에 실패했어요. 다시 시도해 주세요."
_NEW_STARTED_TEXT = "✨ 새 대화를 시작했어요."
_DONE_TEXT = "✅ 완료했어요."
_UNKNOWN_CMD_TEXT = "지원하지 않는 명령이에요. /help 를 입력해 보세요."
_PASSWORD_INTRO = "🔑 비밀번호 설정용 일회용 토큰이에요 (곧 만료됩니다):"
_SETDEFAULT_OK_TEXT = (
    "🔔 이 채팅의 대화를 기본으로 설정했어요. 예약 알림과 외부 메시지가 여기로 와요."
)
HELP_TEXT = (
    "🤖 AI 에이전트 기반 개인 비서예요. 메시지를 보내 대화를 시작하거나, 아래 명령어를 입력하세요.\n\n"
    "명령어:\n"
    "/register <이메일> — 사용자 등록 요청 (관리자 승인 필요)\n"
    "/link <토큰> — 이 채팅을 기존 계정에 연결\n"
    "/new — 새 대화 시작\n"
    "/check — 진행 상황 확인 / 완료된 답변 받기\n"
    "/stop — 진행 중인 작업 중지\n"
    "/config — 설정 보기/변경\n"
    "/cron — 예약 작업 관리\n"
    "/delegate <에이전트> — 전문 에이전트에게 위임\n"
    "/undelegate — 기본 에이전트로 복귀\n"
    "/password — 웹 비밀번호 설정 링크 받기\n"
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
        self._callback_ttl = settings.kakao_callback_ttl_s
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
        """Cancel in-flight compute/parked turns and await their teardown, so
        the caller can safely close Redis/the pool afterwards (a park task
        mid-write would otherwise hit a closed client). Best-effort."""
        tasks = list(self._turns)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            if utterance.startswith("/"):
                await self._handle_command(chat_id, callback_url, utterance, body)
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
        # (label, messageText): Korean face, slash-command payload.
        return [(CHECK_LABEL, _CHECK_CMD), (STOP_LABEL, _STOP_CMD)]

    @staticmethod
    def _decode_images(images_json: str) -> list[tuple[str, str]]:
        if not images_json:
            return []
        try:
            return [(u, a) for u, a in json.loads(images_json)]
        except Exception:  # noqa: BLE001
            logger.warning(
                "kakao_parked_images_corrupt",
                extra={"event": "kakao_parked_images_corrupt"},
            )
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

    async def _cmd_check(self, chat_id: str, callback_url: str) -> None:
        """`/check` (the `[확인]` button): deliver a parked answer if ready,
        else report still-working (with the latest progress) / idle. A pure
        poll — never claims or starts a turn."""
        if await self._deliver_ready(chat_id, callback_url):
            return
        turn = await self._registry.get_turn(chat_id)
        if turn and turn.get("state") == "pending":
            await self._client.post_callback(
                callback_url, _with_progress(_STILL_WORKING_TEXT, turn),
                quick_replies=self._poll_buttons(),
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

    async def _claim_or_handle(self, chat_id: str, callback_url: str) -> bool:
        """Atomically gate "one turn per chat". Returns True when the request
        was already handled (a parked answer was delivered, or a turn is in
        flight → "still working") and the caller must stop. Returns False
        when this call CLAIMED the chat (an atomic `try_begin`); the caller
        must then run a turn and eventually clear/park it — releasing the
        claim with `clear` if it bails before dispatch."""
        if await self._deliver_ready(chat_id, callback_url):
            return True
        if not await self._registry.try_begin(chat_id):
            turn = await self._registry.get_turn(chat_id)
            await self._client.post_callback(
                callback_url, _with_progress(_STILL_WORKING_TEXT, turn),
                quick_replies=self._poll_buttons(),
            )
            return True
        return False

    async def _handle_message(
        self, chat_id: str, callback_url: str, text: str, body: dict
    ) -> None:
        if await self._claim_or_handle(chat_id, callback_url):
            return
        resolved = await self._resolve_session(chat_id, callback_url)
        if resolved is None:
            await self._registry.clear(chat_id)  # release the claim
            return  # register / no-session prompt already sent
        user_id, session_id = resolved
        await self._run_compute(
            chat_id, callback_url, body,
            self._compute_turn(chat_id, user_id, session_id, text, body),
        )

    async def _run_compute(
        self, chat_id: str, callback_url: str, body: dict, coro: Any
    ) -> None:
        """Run a TurnReply-producing coroutine, racing it against the callback
        budget: deliver in-time, otherwise post a status + park for the next
        touch. Shared by normal turns and dispatching commands."""
        compute = asyncio.create_task(coro)
        self._turns.add(compute)
        compute.add_done_callback(self._turns.discard)

        budget = self._callback_budget(body)
        if budget > 0:
            done, _ = await asyncio.wait({compute}, timeout=budget)
        else:
            done = set()  # callback already dead → park-only, no race

        if compute in done:
            try:
                reply = compute.result()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_turn_error", extra={"event": "kakao_turn_error"}
                )
                await self._registry.clear(chat_id)
                await self._client.post_callback(callback_url, _DISPATCH_FAILED_TEXT)
                return
            # Deliver on the (single-use) callback; if that POST fails, the
            # callback is spent and can't be retried — PARK the answer so the
            # user's next touch still gets it, rather than losing it.
            try:
                await self._deliver(callback_url, reply)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_deliver_failed", extra={"event": "kakao_deliver_failed"}
                )
                await self._registry.store_ready_unless_stopped(
                    chat_id, reply.text, json.dumps(reply.images)
                )
                return
            await self._registry.clear(chat_id)
            return

        # Overran (or stale callback) — park the rest, status if we still can.
        # By the deadline (~50 s) the turn has usually emitted tool steps, so
        # surface the latest one alongside the "still working" status.
        if budget > 0:
            turn = await self._registry.get_turn(chat_id)
            with contextlib.suppress(Exception):
                await self._client.post_callback(
                    callback_url, _with_progress(_WORKING_TEXT, turn),
                    quick_replies=self._poll_buttons(),
                )
        compute.add_done_callback(lambda t: self._schedule_park(chat_id, t))

    def _callback_budget(self, body: dict) -> float:
        """Seconds we may spend before the callback dies: the configured
        deadline, capped by the callback's remaining TTL given when the relay
        enqueued it (`received_at`, epoch ms). ≤0 means the callback is
        already (near) dead → park-only. A missing `received_at` (the relay
        always sets it) is treated as already-stale, the safe default."""
        received_at = body.get("received_at")
        if not received_at:
            return 0.0
        age = max(0.0, time.time() - float(received_at) / 1000.0)
        return min(self._deadline, self._callback_ttl - _CALLBACK_MARGIN_S - age)

    def _schedule_park(self, chat_id: str, task: asyncio.Task) -> None:
        """Done-callback (sync) → schedule the async park of a finished turn."""
        t = asyncio.create_task(self._park(chat_id, task))
        self._turns.add(t)
        t.add_done_callback(self._turns.discard)

    async def _park(self, chat_id: str, task: asyncio.Task) -> None:
        try:
            reply = task.result()
        except asyncio.CancelledError:
            return  # process shutting down — leave state for next touch
        except Exception:  # noqa: BLE001
            logger.exception("kakao_turn_error", extra={"event": "kakao_turn_error"})
            reply = TurnReply(_DISPATCH_FAILED_TEXT, [])
        # Park atomically: a concurrent /stop (mark_stopped) or /new (clear)
        # landing now must win — store_ready_unless_stopped returns False and
        # we drop the stale answer instead of resurrecting an abandoned turn.
        parked = await self._registry.store_ready_unless_stopped(
            chat_id, reply.text, json.dumps(reply.images)
        )
        if not parked:
            await self._registry.clear(chat_id)  # stopped/cleared → leave clean

    async def _compute_turn(
        self, chat_id: str, user_id: str, session_id: str, text: str, body: dict
    ) -> TurnReply:
        """Run one turn through ChannelCore under the session lock and return
        the user-facing reply (+ any outbound images). Delivery (now vs
        parked) is the caller's call."""
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
            await self._registry.set_task(chat_id, user_id, task_id)

            async def _on_progress(pf: Any) -> None:
                """Record the latest tool step so the 'still working' status can
                show it on the user's next touch. Best-effort — a Redis hiccup
                here must never affect the turn."""
                lp = (getattr(pf, "metadata", None) or {}).get(LOOP_PROGRESS_KEY)
                if not lp or lp.get("kind") not in ("tool_call", "tool_result"):
                    return
                try:
                    await self._registry.set_progress(
                        chat_id, _format_progress(lp, progress_producer(pf))
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "kakao_progress_store_failed",
                        extra={"event": "kakao_progress_store_failed"},
                        exc_info=True,
                    )

            result = await self._core.await_result(task_id, on_progress=_on_progress)

            reply = (result.output.content if result.output else "") or ""
            out_names = list(result.output.files) if result.output else []
            images, files = await self._upload_outbound(user_id, session_id, out_names)
            reply_text = f"{agent_tag(result.agent_id)}{reply}" if reply else ""
            if files:
                links = "\n".join(f"• {fn}\n{url}" for fn, url in files)
                block = f"{_FILE_LINKS_INTRO}\n{links}"
                reply_text = f"{reply_text}\n\n{block}" if reply_text else block
            if not reply_text:
                reply_text = _NO_RESPONSE_TEXT
            context_tokens = await self._core.after_result(session_id, dest, result)
            await self._core.maybe_summarize(session_id, dest, context_tokens)

        self._core.fire_memory_add(user_id, session_id, text, reply)
        self._core.fire_name_session(user_id, session_id, text)
        return TurnReply(reply_text, images)

    @staticmethod
    async def _reply_only(coro: Any) -> TurnReply:
        """Wrap a coroutine that returns a plain message string as a
        (text, no-images) TurnReply, so command messages reuse the same
        deadline/park delivery path."""
        return TurnReply(await coro, [])

    async def _compute_command(
        self, chat_id: str, user_id: str, session_id: str,
        dest: str, mode: str, prompt: str,
    ) -> TurnReply:
        """Dispatch a slash command to an agent (config / cron), bypassing the
        orchestrator and the conversation thread (no history, no summarize,
        no memory) — mirrors ChatbotGateway._cmd_agent but returns a
        TurnReply for the shared deadline/park path."""
        task_id = await self._core.spawn(user_id, session_id, dest, mode, prompt)
        await self._registry.set_task(chat_id, user_id, task_id)
        result = await self._core.await_result(task_id)
        if result.status != TaskStatus.SUCCEEDED:
            logger.warning(
                "kakao_command_task_failed",
                extra={"event": "kakao_command_task_failed", "mode": mode},
            )
            return TurnReply(_DISPATCH_FAILED_TEXT, [])
        reply = (result.output.content if result.output else "") or _DONE_TEXT
        return TurnReply(reply, [])

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
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Resolve produced file names → bytes → R2 presigned urls. Returns
        `(images, files)`: `images` (url, alt) render inline as Kakao
        simpleImage bubbles; `files` (filename, url) are non-image downloads
        surfaced as links in the reply text (Kakao can't inline documents).
        Needs R2 configured; a file that can't be uploaded is skipped (logged)."""
        if self._egress is None or self._credentials is None or not names:
            return [], []
        images: list[tuple[str, str]] = []
        files: list[tuple[str, str]] = []
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
                url = await self._egress.put_file(
                    data, content_type=mime, key=egress_key(session_id, name)
                )
                short = name.rsplit("/", 1)[-1]
                if mime.startswith("image/"):
                    images.append((url, short))
                else:
                    files.append((short, url))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "kakao_outbound_file_failed",
                    extra={"event": "kakao_outbound_file_failed"},
                )
        return images, files

    # -- identity + commands --------------------------------------------

    async def _resolve_user(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            return await queries.resolve_user_id(
                conn, platform=PLATFORM, chat_id=chat_id
            )

    async def _resolve_session(
        self, chat_id: str, callback_url: str
    ) -> tuple[str, str] | None:
        """`(user_id, session_id)` for a registered chat — `session_id` is the
        chat's OWN current session (`mapping.session_id`), falling back to the
        user's `default_session_id` (the cron fallback) only until the chat has
        one of its own. None after sending the appropriate prompt."""
        async with self._pool.acquire() as conn:
            mapping = await queries.get_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id
            )
            if mapping is None:
                await self._client.post_callback(callback_url, _REGISTER_PROMPT)
                return None
            cfg = await queries.get_user_config(conn, mapping.user_id)
        session_id = mapping.session_id or (cfg.default_session_id if cfg else None)
        if session_id is None:
            await self._client.post_callback(callback_url, _NO_SESSION_TEXT)
            return None
        return mapping.user_id, session_id

    async def _handle_command(
        self, chat_id: str, callback_url: str, text: str, body: dict
    ) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/help", "/start"):
            await self._client.post_callback(callback_url, HELP_TEXT)
        elif cmd == "/register":
            await self._cmd_register(chat_id, callback_url, arg)
        elif cmd == "/link":
            await self._cmd_link(chat_id, callback_url, arg)
        elif cmd == "/new":
            await self._cmd_new(chat_id, callback_url)
        elif cmd == _CHECK_CMD:
            await self._cmd_check(chat_id, callback_url)
        elif cmd == _STOP_CMD:
            await self._stop(chat_id, callback_url)
        elif cmd == "/config":
            await self._cmd_agent(
                chat_id, callback_url, body, CONFIG_AGENT_ID, "message",
                arg or "Show my current settings.",
            )
        elif cmd == "/cron":
            await self._cmd_agent(
                chat_id, callback_url, body, CONFIG_AGENT_ID, "cron",
                arg or "List my scheduled jobs.",
            )
        elif cmd == "/delegate":
            await self._cmd_delegate(chat_id, callback_url, body, arg)
        elif cmd == "/undelegate":
            await self._cmd_undelegate(chat_id, callback_url, body)
        elif cmd == "/setdefault":
            await self._cmd_setdefault(chat_id, callback_url)
        elif cmd == "/password":
            await self._cmd_password(chat_id, callback_url)
        else:
            await self._client.post_callback(callback_url, _UNKNOWN_CMD_TEXT)

    async def _cmd_agent(
        self, chat_id: str, callback_url: str, body: dict,
        dest: str, mode: str, prompt: str,
    ) -> None:
        """Route /config·/cron to an agent and relay its reply (deadline/park
        aware). Bypasses the orchestrator + conversation thread."""
        if await self._claim_or_handle(chat_id, callback_url):
            return
        resolved = await self._resolve_session(chat_id, callback_url)
        if resolved is None:
            await self._registry.clear(chat_id)  # release the claim
            return
        user_id, session_id = resolved
        await self._run_compute(
            chat_id, callback_url, body,
            self._compute_command(chat_id, user_id, session_id, dest, mode, prompt),
        )

    async def _cmd_delegate(
        self, chat_id: str, callback_url: str, body: dict, arg: str
    ) -> None:
        if await self._claim_or_handle(chat_id, callback_url):
            return
        resolved = await self._resolve_session(chat_id, callback_url)
        if resolved is None:
            await self._registry.clear(chat_id)  # release the claim
            return
        user_id, session_id = resolved
        parts = arg.split(maxsplit=1)
        target = parts[0] if parts else ""
        await self._run_compute(
            chat_id, callback_url, body,
            self._reply_only(self._core.delegate(user_id, session_id, target)),
        )

    async def _cmd_undelegate(
        self, chat_id: str, callback_url: str, body: dict
    ) -> None:
        if await self._claim_or_handle(chat_id, callback_url):
            return
        resolved = await self._resolve_session(chat_id, callback_url)
        if resolved is None:
            await self._registry.clear(chat_id)  # release the claim
            return
        user_id, session_id = resolved
        await self._run_compute(
            chat_id, callback_url, body,
            self._reply_only(self._core.undelegate(user_id, session_id)),
        )

    async def _cmd_setdefault(self, chat_id: str, callback_url: str) -> None:
        """Point default_session_id (the cron fallback / async delivery target)
        at THIS chat's current session (fast — no dispatch). For a
        multi-channel user, picks which channel's conversation reminders land
        in."""
        async with self._pool.acquire() as conn:
            mapping = await queries.get_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id
            )
            if mapping is None:
                await self._client.post_callback(callback_url, _REGISTER_PROMPT)
                return
            if mapping.session_id is None:
                await self._client.post_callback(callback_url, _NO_SESSION_TEXT)
                return
            await queries.set_default_session_id(
                conn, user_id=mapping.user_id, session_id=mapping.session_id
            )
        await self._client.post_callback(callback_url, _SETDEFAULT_OK_TEXT)

    async def _cmd_password(self, chat_id: str, callback_url: str) -> None:
        """Mint a one-time password-setup token (fast — no dispatch)."""
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._client.post_callback(callback_url, _REGISTER_PROMPT)
            return
        if self._credentials is None:
            await self._client.post_callback(callback_url, _UNAVAILABLE_TEXT)
            return
        try:
            token = await self._credentials.mint_password_reset_token(user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_password_mint_failed",
                extra={"event": "kakao_password_mint_failed"},
            )
            await self._client.post_callback(
                callback_url, "비밀번호 설정 링크 생성에 실패했어요. 다시 시도해 주세요."
            )
            return
        await self._client.post_callback(
            callback_url, f"{_PASSWORD_INTRO}\n{token}"
        )

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

    async def _cmd_link(
        self, chat_id: str, callback_url: str, arg: str
    ) -> None:
        """Bind this (unmapped) chat to a pre-existing account by verifying a
        password-reset token minted on a channel the user is already on
        ([channel.md] §6). On success, map (PLATFORM, chat_id) → the returned
        user_id and open a fresh session for THIS chat, so it shares the
        account (memory/files) but keeps its own conversation instead of
        interleaving into another channel's thread."""
        if await self._resolve_user(chat_id) is not None:
            await self._client.post_callback(callback_url, _ALREADY_REGISTERED_TEXT)
            return
        token = arg.split(maxsplit=1)[0] if arg else ""
        if not token:
            await self._client.post_callback(callback_url, _LINK_USAGE_TEXT)
            return
        if self._credentials is None:
            await self._client.post_callback(callback_url, _UNAVAILABLE_TEXT)
            return
        try:
            user_id = await self._credentials.verify_link_token(token=token)
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_link_verify_failed",
                extra={"event": "kakao_link_verify_failed"},
            )
            await self._client.post_callback(callback_url, _LINK_FAILED_TEXT)
            return
        if user_id is None:
            await self._client.post_callback(callback_url, _LINK_INVALID_TEXT)
            return
        async with self._pool.acquire() as conn:
            await queries.upsert_platform_mapping(
                conn, platform=PLATFORM, chat_id=chat_id, user_id=user_id
            )
        # Open this chat's OWN session so it doesn't land in the account's
        # other-channel conversation. Best-effort: on failure the mapping
        # stands and the chat falls back to the default until its first /new.
        try:
            new_session = await self._credentials.open_session(
                user_id=user_id, metadata={"kind": CHANNEL, "external_id": chat_id}
            )
            async with self._pool.acquire() as conn:
                await queries.create_session_info(
                    conn, session_id=new_session, user_id=user_id,
                    channel=CHANNEL, chat_id=chat_id,
                )
                await queries.set_mapping_session_id(
                    conn, platform=PLATFORM, chat_id=chat_id, session_id=new_session,
                )
                cfg = await queries.get_user_config(conn, user_id)
                if cfg is None or cfg.default_session_id is None:
                    await queries.set_default_session_id(
                        conn, user_id=user_id, session_id=new_session,
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "kakao_link_open_session_failed",
                extra={"event": "kakao_link_open_session_failed"},
            )
        await self._client.post_callback(callback_url, _LINK_OK_TEXT)

    async def _cmd_new(self, chat_id: str, callback_url: str) -> None:
        user_id = await self._resolve_user(chat_id)
        if user_id is None:
            await self._client.post_callback(callback_url, _REGISTER_PROMPT)
            return
        if self._credentials is None:
            await self._client.post_callback(callback_url, _UNAVAILABLE_TEXT)
            return
        # Retire THIS chat's previous conversation (its own session, NOT the
        # shared default — another channel may be using that), best-effort,
        # then open a fresh one — mirrors the Telegram /new path.
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
            # This chat rides the fresh session; it also becomes the cron
            # fallback (the re-pointing rule — newest conversation wins).
            await queries.set_mapping_session_id(
                conn, platform=PLATFORM, chat_id=chat_id, session_id=new_session,
            )
            await queries.set_default_session_id(
                conn, user_id=user_id, session_id=new_session
            )
        await self._registry.clear(chat_id)
        await self._client.post_callback(callback_url, _NEW_STARTED_TEXT)
