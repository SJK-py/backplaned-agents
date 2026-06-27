"""bp_agents.agents.webapp.turns — detached chat-turn runner.

A chat turn runs as a background task owned by the app, NOT inside the SSE
request handler, so navigating away (closing the stream) no longer kills it.
The runner buffers each rendered progress row plus the final answer, so a
(re)connecting SSE subscriber replays the backlog and then follows live —
which is what lets `chat_view` rebuild the in-flight bubble (Stop button +
progress) when the user navigates back mid-turn.

Single-process: the `active_turns` registry is in-memory, matching the
webapp's single-instance assumption (running a second webapp instance would
need a shared bus, like the Valkey-backed session lock).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bp_agents.channel import agent_tag, progress_producer, render_progress_line
from bp_agents.common.progress import LOOP_PROGRESS_KEY
from bp_protocol.types import TaskStatus

logger = logging.getLogger(__name__)

# Cap the per-session runner registry; evict completed runners first so a long
# uptime with many sessions can't grow it without bound.
_MAX_ACTIVE_TURNS = 256


class TurnRunner:
    """One chat turn, detached from any HTTP connection. `run()` executes it
    (under the session lock) and publishes rendered SSE events; subscribers
    attach via `subscribe()` and replay the backlog before following live."""

    def __init__(
        self, *, session_id: str, turn_id: str, user_id: str, text: str,
        core: Any, env: Any,
    ) -> None:
        self.session_id = session_id
        # A per-turn id: the pending bubble streams `/chat/{sid}/stream/{turn_id}`
        # and chat_stream only serves a matching runner. So a PREVIOUS bubble's
        # EventSource reconnect can't latch onto a newer turn (which would feed
        # the old activity box the new turn's progress).
        self.turn_id = turn_id
        self.user_id = user_id
        self.text = text
        self._core = core
        self._env = env
        # The router task id, set once spawned — read by POST /stop to cancel.
        self.task_id: str | None = None
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None
        # Replayable `(seq, event, data)` log (progress rows + the result) and
        # the set of live subscriber queues. Each event carries a monotonic
        # `seq` emitted as the SSE `id:`, so a reconnecting EventSource (which
        # sends `Last-Event-ID`) replays only events it hasn't seen — otherwise
        # every reconnect re-appends the whole backlog (duplicate messages).
        self._seq = 0
        self._backlog: list[tuple[int, str, str]] = []
        self._subs: set[asyncio.Queue[tuple[int | None, str, str]]] = set()

    # -- rendering -------------------------------------------------------

    def _row(self, agent_id: str | None, lp: dict) -> str:
        return self._env.get_template("chat/_progress_row.html").render(
            line=f"{agent_tag(agent_id)}{render_progress_line(lp)}"
        )

    def _answer(self, agent_id: str | None, content: str, files: list[str]) -> str:
        return self._env.get_template("chat/_message.html").render(
            role="assistant", content=content or "(no response)",
            tag=agent_tag(agent_id), files=files, session_id=self.session_id,
        )

    # -- pub/sub ---------------------------------------------------------

    def subscribe(self, *, after: int = 0) -> asyncio.Queue[tuple[int | None, str, str]]:
        """A new subscriber queue, pre-loaded with backlog events newer than
        `after` (the client's `Last-Event-ID`; 0 = a fresh connection replays
        everything), and a closing `done` if the turn already finished."""
        q: asyncio.Queue[tuple[int | None, str, str]] = asyncio.Queue()
        for seq, event, data in self._backlog:
            if seq > after:
                q.put_nowait((seq, event, data))
        if self.done.is_set():
            q.put_nowait((None, "done", ""))
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[int | None, str, str]]) -> None:
        self._subs.discard(q)

    def _publish(self, event: str, data: str) -> None:
        # `done` isn't backlogged — it's derived from `self.done` on subscribe.
        self._seq += 1
        item = (self._seq, event, data)
        self._backlog.append(item)
        for q in self._subs:
            q.put_nowait(item)

    def _finish(self) -> None:
        # `done` is a control event — no `id:`, so it never advances the
        # client's Last-Event-ID (and isn't replayed on reconnect).
        self.done.set()
        for q in self._subs:
            q.put_nowait((None, "done", ""))

    # -- execution -------------------------------------------------------

    async def run(self) -> None:
        core = self._core

        async def on_progress(pf: Any) -> None:
            lp = (getattr(pf, "metadata", None) or {}).get(LOOP_PROGRESS_KEY)
            if lp:
                self._publish("progress", self._row(progress_producer(pf), lp))

        reply = ""
        try:
            async with core.session_lock(self.session_id):
                dest, mode = await core.route(self.session_id)
                await core.record_user_turn(self.session_id, dest, self.text)
                self.task_id = await core.spawn(
                    self.user_id, self.session_id, dest, mode, self.text
                )
                result = await core.await_result(self.task_id, on_progress=on_progress)
                # A Stop (router cancel) returns a terminal CANCELLED result —
                # acknowledge it without recording an answer turn (the user
                # message stays; matches the /stop command).
                if getattr(result, "status", None) is TaskStatus.CANCELLED:
                    self._publish("result", self._answer(None, "_Stopped._", []))
                    return
                reply = (result.output.content if result.output else "") or ""
                files = list(result.output.files) if result.output else []
                ctx = await core.after_result(self.session_id, dest, result)
                await core.maybe_summarize(self.session_id, dest, ctx)
            core.fire_memory_add(self.user_id, self.session_id, self.text, reply)
            core.fire_name_session(self.user_id, self.session_id, self.text)
            self._publish("result", self._answer(result.agent_id, reply, files))
        except Exception:  # noqa: BLE001
            logger.exception(
                "webapp_turn_failed",
                extra={"event": "webapp_turn_failed", "bp.session_id": self.session_id},
            )
            self._publish(
                "result", self._answer(None, "Sorry — something went wrong handling that.", [])
            )
        finally:
            self._finish()


def register_turn(active: dict[str, TurnRunner], runner: TurnRunner) -> None:
    """Register `runner` for its session (replacing any prior), bounding the
    registry by evicting completed runners when it grows too large."""
    active[runner.session_id] = runner
    if len(active) > _MAX_ACTIVE_TURNS:
        for sid, r in list(active.items()):
            if r is not runner and r.done.is_set():
                active.pop(sid, None)
                if len(active) <= _MAX_ACTIVE_TURNS:
                    break
