"""bp_agents.agents.webapp.pages.chat — the chat pane + SSE progress.

The webapp as a real `ChannelCore` frontend ([webapp.md] §4):

  - `GET  /chat/{sid}`              — history (active thread) + input.
  - `POST /chat/{sid}`             — record a turn, return the user bubble +
                                      a pending bubble that SSE-connects to…
  - `GET  /chat/{sid}/stream/{tid}` — …this stream: run the turn under the
                                      session lock, forwarding each
                                      `LoopProgress` as an SSE `progress`
                                      event and the answer as `result`.
  - `GET  /files/{sid}/{name}`     — resolve a produced file NAME → bytes.

The turn runs as a task while the SSE generator drains a progress queue,
so rows stream as they happen. Delegation/summarization/memory all live
in `ChannelCore` — identical to the Telegram bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from bp_agents.agents.webapp.auth import session_user_id
from bp_agents.agents.webapp.pages._common import owned_session as _owned_session
from bp_agents.channel import ORCHESTRATOR_AGENT_ID, agent_tag, render_progress_line
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_agents.db import queries

logger = logging.getLogger(__name__)
router = APIRouter()

# Cap the pending-turn registry so a tab that POSTs but never opens the
# stream can't grow it without bound (oldest dropped).
_MAX_PENDING_TURNS = 512


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
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "chat/view.html",
        {
            "session_id": session_id,
            "history": history,
            "delegated_to": info.delegated_to,
            "delegatable": delegatable,
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


@router.post("/chat/{session_id}", response_class=HTMLResponse)
async def chat_send(
    session_id: str, request: Request, message: str = Form(...)
) -> HTMLResponse:
    info = await _owned_session(request, session_id)
    if info is None:
        raise HTTPException(status_code=404)
    text = message.strip()
    if not text:
        return HTMLResponse("")

    turns: dict[str, dict] = request.app.state.turns
    if len(turns) >= _MAX_PENDING_TURNS:
        # Drop the oldest pending entry (insertion-ordered dict).
        turns.pop(next(iter(turns)), None)
    turn_id = secrets.token_urlsafe(8)
    turns[turn_id] = {
        "session_id": session_id,
        "user_id": session_user_id(request),
        "text": text,
    }

    env = request.app.state.templates.env
    user_html = env.get_template("chat/_message.html").render(
        role="user", content=text, tag="", files=[]
    )
    pending_html = env.get_template("chat/_pending.html").render(
        session_id=session_id, turn_id=turn_id
    )
    return HTMLResponse(user_html + pending_html)


@router.get("/chat/{session_id}/stream/{turn_id}")
async def chat_stream(
    session_id: str, turn_id: str, request: Request
) -> Response:
    core = request.app.state.core
    pending = request.app.state.turns.pop(turn_id, None)
    if core is None or pending is None or pending["session_id"] != session_id:
        return Response(status_code=404)
    user_id: str = pending["user_id"]
    text: str = pending["text"]
    env = request.app.state.templates.env

    def _row(agent_id: str | None, lp: dict) -> str:
        return env.get_template("chat/_progress_row.html").render(
            line=f"{agent_tag(agent_id)}{render_progress_line(lp)}"
        )

    def _answer(agent_id: str | None, content: str, files: list[str]) -> str:
        return env.get_template("chat/_message.html").render(
            role="assistant", content=content or "(no response)",
            tag=agent_tag(agent_id), files=files, session_id=session_id,
        )

    async def _gen():  # noqa: ANN202
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        async def on_progress(pf: Any) -> None:
            lp = (getattr(pf, "metadata", None) or {}).get(LOOP_PROGRESS_KEY)
            if lp:
                await queue.put(("progress", _row(getattr(pf, "agent_id", None), lp)))

        async def _run() -> None:
            reply = ""
            try:
                async with core.session_lock(session_id):
                    dest, mode = await core.route(session_id)
                    await core.record_user_turn(session_id, dest, text)
                    task_id = await core.spawn(user_id, session_id, dest, mode, text)
                    result = await core.await_result(task_id, on_progress=on_progress)
                    reply = (result.output.content if result.output else "") or ""
                    files = list(result.output.files) if result.output else []
                    ctx = await core.after_result(session_id, dest, result)
                    await core.maybe_summarize(session_id, dest, ctx)
                core.fire_memory_add(user_id, session_id, text, reply)
                await queue.put(("result", _answer(result.agent_id, reply, files)))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "webapp_turn_failed",
                    extra={"event": "webapp_turn_failed", "bp.session_id": session_id},
                )
                await queue.put((
                    "result",
                    _answer(None, "Sorry — something went wrong handling that.", []),
                ))
            finally:
                await queue.put(("done", ""))

        task = asyncio.create_task(_run())
        try:
            while True:
                kind, data = await queue.get()
                yield _sse(kind, data)
                if kind == "done":
                    break
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
