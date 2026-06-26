"""bp_agents.agents.webapp.pages.chat — the chat pane + SSE progress.

The webapp as a real `ChannelCore` frontend ([webapp.md] §4):

  - `GET  /chat/{sid}`        — history + input; if a turn is in flight, also
                                the live pending bubble (Stop + reconnecting
                                SSE) so it RESUMES when the user navigates back.
  - `POST /chat/{sid}`        — record a turn, start it as a DETACHED background
                                runner, and return the user bubble + a pending
                                bubble that SSE-subscribes to that runner.
  - `GET  /chat/{sid}/stream` — subscribe to the session's in-flight turn:
                                replay buffered progress, follow live, then the
                                answer. Closing the stream does NOT cancel it.
  - `POST /chat/{sid}/stop`   — cancel the in-flight turn (parity with /stop).
  - `GET  /files/{sid}/{name}`— resolve a produced file NAME → bytes.

The turn runs detached from any single connection (`webapp.turns`), so a
navigation that drops the SSE leaves it running; the agent persists the answer
regardless, and the view rebuilds the pending bubble while it runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.pages._common import (
    KAKAO_CHANNEL,
    TELEGRAM_CHANNEL,
    ensure_user_config,
)
from bp_agents.agents.webapp.pages._common import owned_session as _owned_session
from bp_agents.agents.webapp.turns import TurnRunner, register_turn
from bp_agents.channel import ORCHESTRATOR_AGENT_ID, agent_tag
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_view(session_id: str, request: Request) -> HTMLResponse:
    info = await _owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    dest = info.delegated_to or ORCHESTRATOR_AGENT_ID

    history: list[dict[str, Any]] = []
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await queries.reload_incumbent(
            conn, session_id=session_id, agent_id=dest
        )
    tag = agent_tag(dest)
    for r in rows:
        if r.hidden:  # delegate seed / fold-back recap — internal, not shown
            continue
        history.append({
            "role": r.role,
            "content": r.message,
            "tag": tag if r.role == "assistant" else "",
        })

    core = request.app.state.core
    delegatable = sorted(core.delegatable_agents) if core is not None else []
    # If a turn is still running for this session, render the live pending
    # bubble (Stop + reconnecting SSE) so it resumes after a navigation.
    runner = request.app.state.active_turns.get(session_id)
    in_flight = runner is not None and not runner.done.is_set()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "chat/view.html",
        {
            "session_id": session_id,
            "history": history,
            "in_flight": in_flight,
            "delegated_to": info.delegated_to,
            "delegatable": delegatable,
            "chat_channel_label": (
                "Telegram" if info.channel == TELEGRAM_CHANNEL
                else "KakaoTalk" if info.channel == KAKAO_CHANNEL
                else None
            ),
            "active_section": "sessions",
        },
    )


@router.post("/chat/{session_id}/delegate")
async def chat_delegate(
    session_id: str, request: Request, agent: str = Form(...)
) -> Response:
    """Hand the session to specialist `agent` (the deterministic path,
    [delegation.md] §6b). Reloads the chat so the new active thread + badge
    render."""
    info = await _owned_session(request, session_id)
    core = request.app.state.core
    if info is None or core is None:
        raise HTTPException(status_code=404)
    await core.delegate(session_user_id(request), session_id, agent.strip())
    return Response(status_code=204, headers={"HX-Redirect": f"/chat/{session_id}"})


@router.post("/chat/{session_id}/undelegate")
async def chat_undelegate(session_id: str, request: Request) -> Response:
    """Return the session to the main assistant."""
    info = await _owned_session(request, session_id)
    core = request.app.state.core
    if info is None or core is None:
        raise HTTPException(status_code=404)
    await core.undelegate(session_user_id(request), session_id)
    return Response(status_code=204, headers={"HX-Redirect": f"/chat/{session_id}"})


@router.post("/chat/{session_id}/stop")
async def chat_stop(session_id: str, request: Request) -> Response:
    """Cancel the session's in-flight turn — the webapp's Stop button, parity
    with the chatbot `/stop` command. The router cancel surfaces to the
    running turn as a terminal CANCELLED result (rendered "Stopped")."""
    info = await _owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    runner = request.app.state.active_turns.get(session_id)
    if runner is not None and runner.task_id:
        # Best-effort: a race where the turn just finished is a no-op.
        with contextlib.suppress(Exception):
            await request.app.state.upstream.cancel_task(
                access_token=request.session["access_token"], task_id=runner.task_id
            )
    return Response(status_code=204)


@router.post("/chat/{session_id}", response_class=HTMLResponse)
async def chat_send(
    session_id: str, request: Request, message: str = Form(...)
) -> HTMLResponse:
    info = await _owned_session(request, session_id)
    core = request.app.state.core
    if info is None or core is None:
        raise HTTPException(status_code=404)
    text = message.strip()
    if not text:
        return HTMLResponse("")
    # Web/OIDC accounts aren't seeded by the chatbot reconcile; make sure their
    # user_config exists before the turn runs so the orchestrator/config agent
    # read real presets instead of a missing row.
    await ensure_user_config(request)

    env = request.app.state.templates.env
    active = request.app.state.active_turns
    pending_html = env.get_template("chat/_pending.html").render(session_id=session_id)

    existing = active.get(session_id)
    if existing is not None and not existing.done.is_set():
        # A turn is already running for this session (the UI replaces the input
        # with the pending bubble, so this is rare). Re-attach rather than
        # starting a duplicate; the typed text is dropped.
        logger.info(
            "webapp_turn_already_active",
            extra={"event": "webapp_turn_already_active", "bp.session_id": session_id},
        )
        return HTMLResponse(pending_html)

    # Start the turn DETACHED from this request, so closing the SSE (e.g.
    # navigating away) doesn't kill it. The runner records the user turn under
    # the session lock; the optimistic user bubble below covers the live page.
    runner = TurnRunner(
        session_id=session_id, user_id=session_user_id(request),
        text=text, core=core, env=env,
    )
    register_turn(active, runner)
    runner.task = asyncio.create_task(runner.run())

    user_html = env.get_template("chat/_message.html").render(
        role="user", content=text, tag="", files=[]
    )
    return HTMLResponse(user_html + pending_html)


@router.get("/chat/{session_id}/stream")
async def chat_stream(session_id: str, request: Request) -> Response:
    info = await _owned_session(request, session_id)
    if info is None:
        return Response(status_code=404)
    runner = request.app.state.active_turns.get(session_id)

    async def _gen():  # noqa: ANN202
        if runner is None:
            # No in-flight turn (already finished + cleaned up). Close at once;
            # the answer is in history and renders on the page itself.
            yield _sse("done", "")
            return
        # Subscribe: replay buffered progress + the result, then follow live.
        # Unsubscribing on disconnect does NOT cancel the turn.
        queue = runner.subscribe()
        try:
            while True:
                kind, data = await queue.get()
                yield _sse(kind, data)
                if kind == "done":
                    break
        finally:
            runner.unsubscribe(queue)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: str) -> str:
    """One SSE event. Each line of `data` becomes its own `data:` field
    (SSE concatenates them with newlines), so multi-line HTML is safe."""
    out = f"event: {event}\n"
    for line in data.split("\n"):
        out += f"data: {line}\n"
    return out + "\n"
