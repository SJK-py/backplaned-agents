"""bp_agents.channel.core — the transport-agnostic channel engine.

See the package docstring. A frontend orchestrates one turn as:

    async with core.session_lock(session_id):
        dest, mode = await core.route(session_id)
        # frontend: persist any inbound files (transport-specific)
        await core.record_user_turn(session_id, dest, text)
        task_id = await core.spawn(user_id, session_id, dest, mode, prompt)
        result = await core.await_result(task_id, on_progress=<frontend>)
        # frontend: relay result.output.content + files (transport-specific)
        ctx_tokens = await core.after_result(session_id, dest, result)
        await core.maybe_summarize(session_id, dest, ctx_tokens)
    core.fire_memory_add(user_id, session_id, text, reply)

and uses `core.delegate` / `core.undelegate` for the slash/button switch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bp_agents.common.payloads import MessagePayload
from bp_agents.db import queries
from bp_agents.session_lock import SessionLockManager
from bp_protocol.types import TaskStatus

if TYPE_CHECKING:
    import asyncpg

    from bp_agents.db.models import SessionInfoRow

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"
MEMORY_AGENT_ID = "memory"
HISTORY_SUMMARIZER_AGENT_ID = "history_summarizer"

# Summarization tuning ([sessions.md] §3): fold the oldest ~70% of the
# incumbent window once a thread crosses the soft limit, but only when
# there's a meaningful number of turns to compress.
_SUMMARIZE_FRACTION = 0.7
_MIN_ROWS_TO_SUMMARIZE = 6
_DEFAULT_CONTEXT_LIMIT = 120_000


def pretty_agent(agent_id: str) -> str:
    """`computer_use` → `Computer Use` — a human-readable specialist name."""
    return agent_id.replace("_", " ").title()


class ChannelCore:
    """Shared, transport-free channel logic. One instance per channel
    process; frontends call into it (see module docstring)."""

    def __init__(
        self,
        *,
        dispatcher: Any,
        pool: asyncpg.Pool,
        delegatable_agents: frozenset[str] = frozenset(),
        result_timeout_s: float = 180.0,
        fire_memory: bool = False,
        redis: Any | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._pool = pool
        self._delegatable = delegatable_agents
        self._result_timeout_s = result_timeout_s
        self._fire_memory = fire_memory
        # Per-`session_id` FIFO serialization ([sessions.md] §4); cross-process
        # when `redis` is set (the prerequisite for a second channel instance).
        self._session_locks = SessionLockManager(redis)
        # Detached fire-and-forget memory.add tasks (tracked for cleanup).
        self._memory_tasks: set[asyncio.Task] = set()
        # Detached fire-and-forget session-name tasks (first-turn titling).
        self._name_tasks: set[asyncio.Task] = set()

    @property
    def delegatable_agents(self) -> frozenset[str]:
        """The agent ids a user may `/delegate` to (the channel's allow-list).
        Exposed so a frontend can render the delegation picker."""
        return self._delegatable

    # -- session serialization + routing --------------------------------

    def session_lock(self, session_id: str):  # noqa: ANN202 — async-ctx guard
        return self._session_locks(session_id)

    async def route(self, session_id: str) -> tuple[str, str]:
        """`(dest, mode)` for the next turn: the delegate during an active
        delegation, else the orchestrator."""
        async with self._pool.acquire() as conn:
            info = await queries.get_session_info(conn, session_id)
        if info and info.delegated_to:
            return info.delegated_to, "delegated_message"
        return ORCHESTRATOR_AGENT_ID, "message"

    async def record_user_turn(self, session_id: str, dest: str, text: str) -> None:
        """Write the user turn verbatim (the channel is the sole writer of
        `user` rows, [sessions.md] §2), into the dispatch target's thread."""
        async with self._pool.acquire() as conn:
            await queries.append_history(
                conn, session_id=session_id, agent_id=dest,
                role="user", message=text,
            )

    # -- task injection (thin wrappers over the SDK dispatcher) ----------

    async def spawn(
        self, user_id: str, session_id: str, dest: str, mode: str, prompt: str
    ) -> str:
        return await self._dispatcher.spawn_root_for_user(
            dest, MessagePayload(prompt=prompt),
            user_id=user_id, session_id=session_id, mode=mode,
        )

    async def await_result(self, task_id: str, *, on_progress: Any = None) -> Any:
        return await self._dispatcher.await_root_result(
            task_id, timeout_s=self._result_timeout_s, on_progress=on_progress,
        )

    async def call_agent(
        self, *, user_id: str, session_id: str, dest: str, mode: str, payload: Any
    ) -> Any:
        """One-shot management dispatch to `dest` (e.g. the Memory / Knowledge
        pages querying their per-user store), returning the terminal result.
        No progress stream, no history write, no summarization. `session_id`
        is only the admit carrier — the target agent works per-user. Returns
        the `ResultFrame`; the caller reads JSON from `output.content`."""
        task_id = await self._dispatcher.spawn_root_for_user(
            dest, payload, user_id=user_id, session_id=session_id, mode=mode,
        )
        return await self._dispatcher.await_root_result(
            task_id, timeout_s=self._result_timeout_s,
        )

    # -- post-turn: delegated_to maintenance + summarization ------------

    async def after_result(self, session_id: str, dest: str, result: Any) -> int | None:
        """Maintain `delegated_to` from the result source, and return the
        agent-measured `context_tokens` (the summarization signal)."""
        await self._update_delegation(session_id, dest, result)
        return (
            result.output.metadata.get("context_tokens") if result.output else None
        )

    async def _update_delegation(self, session_id: str, dest: str, result: Any) -> None:
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

    async def maybe_summarize(
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
            limit = cfg.max_context_token_limit if cfg else _DEFAULT_CONTEXT_LIMIT
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
            from bp_agents.agents.history_summarizer import SummarizeIncumbent  # noqa: PLC0415

            task_id = await self._dispatcher.spawn_root_for_user(
                HISTORY_SUMMARIZER_AGENT_ID,
                SummarizeIncumbent(
                    agent_id=agent_id, up_to=up_to, previous_summary=previous
                ),
                user_id=info.user_id, session_id=session_id,
                mode="summarize_incumbent",
            )
            result = await self.await_result(task_id)
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

    # -- user-driven delegation switch ([delegation.md] §6 path b) -------

    async def delegate(self, user_id: str, session_id: str, target: str) -> str:
        """Switch the session to specialist `target`: summarize the main
        thread into the delegate's seed, set `delegated_to`. Folds back a
        current delegate first. Returns the user-facing message."""
        if target not in self._delegatable:
            avail = ", ".join(sorted(self._delegatable)) or "(none configured)"
            return (
                f"Can't delegate to {target or '(missing agent)'}. "
                f"Available: {avail}."
            )
        async with self.session_lock(session_id):
            async with self._pool.acquire() as conn:
                info = await queries.get_session_info(conn, session_id)
            current = info.delegated_to if info else None
            if current == target:
                return f"Already delegated to {pretty_agent(target)}."
            if current:  # implicit switch — fold the current one back first
                await self._fold_back(session_id, user_id, current, info)
                async with self._pool.acquire() as conn:
                    info = await queries.get_session_info(conn, session_id)
            summary = await self._summarize_thread(
                session_id, user_id, ORCHESTRATOR_AGENT_ID,
                previous=info.history_summary if info else None,
            )
            seed = (
                "## Conversation so far (summarized)\n"
                f"{summary or '(no prior conversation)'}\n\n"
                "The user has delegated this conversation to you; continue "
                "helping them directly."
            )
            async with self._pool.acquire() as conn:
                await queries.append_history(
                    conn, session_id=session_id, agent_id=target,
                    role="user", message=seed, incumbent=True, hidden=True,
                )
                await queries.update_session_info(conn, session_id, delegated_to=target)
        return (
            f"Delegated to {pretty_agent(target)} — it'll handle your messages "
            "until /undelegate."
        )

    async def undelegate(self, user_id: str, session_id: str) -> str:
        """Return the session to the main assistant (summarize the delegate
        thread into a recap, retire the episode). Returns the message."""
        async with self.session_lock(session_id):
            async with self._pool.acquire() as conn:
                info = await queries.get_session_info(conn, session_id)
            current = info.delegated_to if info else None
            if not current:
                return "You're already with the main assistant."
            await self._fold_back(session_id, user_id, current, info)
        return f"Returned to the main assistant (was {pretty_agent(current)})."

    async def _summarize_thread(
        self, session_id: str, user_id: str, agent_id: str, *, previous: str | None
    ) -> str:
        """Best-effort complete summary of one agent's incumbent thread (the
        prior rolling summary folded in). Returns `previous` (or '') on an
        empty thread or a summarizer failure — never blocks the switch."""
        async with self._pool.acquire() as conn:
            rows = await queries.reload_incumbent(
                conn, session_id=session_id, agent_id=agent_id
            )
        if not rows:
            return previous or ""
        from bp_agents.agents.history_summarizer import SummarizeIncumbent  # noqa: PLC0415

        try:
            task_id = await self._dispatcher.spawn_root_for_user(
                HISTORY_SUMMARIZER_AGENT_ID,
                SummarizeIncumbent(
                    agent_id=agent_id, up_to=rows[-1].id, previous_summary=previous
                ),
                user_id=user_id, session_id=session_id, mode="summarize_incumbent",
            )
            result = await self.await_result(task_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "delegation_summarize_failed",
                extra={"event": "delegation_summarize_failed",
                       "bp.session_id": session_id, "agent_id": agent_id},
            )
            return previous or ""
        return (result.output.content if result.output else "") or (previous or "")

    async def _fold_back(
        self, session_id: str, user_id: str, delegate: str, info: SessionInfoRow | None
    ) -> None:
        """End a delegation: summarize the delegate thread into a recap row on
        the main thread, retire the delegate episode, and clear the flags.
        Mirrors `orchestrator.end_delegation` ([delegation.md] Phase 3)."""
        summary = await self._summarize_thread(
            session_id, user_id, delegate,
            previous=info.delegate_summary if info else None,
        )
        recap = f"[Returned from {pretty_agent(delegate)}] {summary or '(no summary)'}"
        async with self._pool.acquire() as conn:
            # Mirror `orchestrator.end_delegation`: a hidden `user` recap (the
            # specialist's results as external input — not the orchestrator's
            # own work) followed by a hidden `assistant` ack that closes the
            # turn, so the reloaded thread alternates. The pre-delegation user
            # turn was already closed by the hand-off marker.
            await queries.append_history(
                conn, session_id=session_id, agent_id=ORCHESTRATOR_AGENT_ID,
                role="user", message=recap, incumbent=True, hidden=True,
            )
            await queries.append_history(
                conn, session_id=session_id, agent_id=ORCHESTRATOR_AGENT_ID,
                role="assistant", message="Acknowledged.", incumbent=True, hidden=True,
            )
            await queries.demote_thread(conn, session_id=session_id, agent_id=delegate)
            await queries.update_session_info(
                conn, session_id, delegate_summary=None, delegated_to=None
            )

    # -- memory ----------------------------------------------------------

    def fire_name_session(
        self, user_id: str, session_id: str, user_prompt: str
    ) -> None:
        """Title the conversation from its first message, fire-and-forget,
        OUTSIDE the session lock. A no-op once the session already has a name,
        so it effectively runs only on the first turn. Best-effort: a failure
        leaves the name unset and a later turn retries."""
        if not user_prompt.strip():
            return
        task = asyncio.create_task(
            self._name_session(user_id, session_id, user_prompt)
        )
        self._name_tasks.add(task)
        task.add_done_callback(self._name_tasks.discard)

    async def _name_session(
        self, user_id: str, session_id: str, user_prompt: str
    ) -> None:
        from bp_agents.agents.history_summarizer import (  # noqa: PLC0415
            NameSession,
        )

        try:
            async with self._pool.acquire() as conn:
                info = await queries.get_session_info(conn, session_id)
            if info is None or info.session_name:
                return  # session gone, or already named — nothing to do
            task_id = await self._dispatcher.spawn_root_for_user(
                HISTORY_SUMMARIZER_AGENT_ID,
                NameSession(user_prompt=user_prompt),
                user_id=user_id, session_id=session_id, mode="session_name",
            )
            result = await self._dispatcher.await_root_result(
                task_id, timeout_s=self._result_timeout_s,
            )
            title = (
                (result.output.content or "").strip()
                if result.output is not None else ""
            )
            if not title:
                return
            async with self._pool.acquire() as conn:
                await queries.update_session_info(
                    conn, session_id, session_name=title
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "session_name_failed", extra={"event": "session_name_failed"}
            )

    def fire_memory_add(
        self, user_id: str, session_id: str, user_prompt: str, reply: str
    ) -> None:
        """Spawn `memory.add` for the turn, fire-and-forget, OUTSIDE the
        session lock ([overview.md] §2.2). No-op unless `fire_memory` and a
        non-empty reply. Detached so the next turn isn't blocked."""
        if not (self._fire_memory and reply):
            return
        task = asyncio.create_task(
            self._memory_add(user_id, session_id, user_prompt, reply)
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    async def _memory_add(
        self, user_id: str, session_id: str, user_prompt: str, reply: str
    ) -> None:
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
