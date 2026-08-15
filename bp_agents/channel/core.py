"""bp_agents.channel.core — the transport-agnostic channel engine.

See the package docstring. A frontend orchestrates one turn as:

    async with core.turn(user_id, session_id):
        dest, mode = await core.route(user_id, session_id)
        # frontend: persist any inbound files (transport-specific)
        task_id = await core.spawn(user_id, session_id, dest, mode, prompt)
        result = await core.await_result(task_id, on_progress=<frontend>)
        # frontend: relay result.output.content + files (transport-specific)
        await core.after_result(user_id, session_id, dest, result)
    core.fire_memory_add(user_id, session_id, text, reply)

and uses `core.delegate` / `core.undelegate` for the slash/button switch.

**The channel no longer writes anyone's history.** Conversation lives in the
router's session store ([../../docs/design/router-managed-session-store.md]),
where a thread can only be written by the agent that owns it — so the channel
holds a *steward* view: it may read any thread, drive session state, enqueue
hand-overs, and take the turn lease, and it may not append. Three things
follow, and they are the whole shape of this module:

  * **The user's words arrive as the task payload**, and the executing agent
    appends them to its own thread as its opening act. `[shipped]` §13 spec'd
    a `HandOver(kind="input")` instead; the payload already carries the same
    text, so a hand-over would duplicate it and add a synchronous round trip
    to the user-visible path. The property §13 was protecting — that nobody
    writes another agent's thread — holds either way.
  * **Summarization is the thread owner's job**, done at the start of its own
    turn. `[shipped]` §13 had the steward decide via `StatThread`; but the
    apply must be owner-written regardless, and the owner already measures its
    context, so routing the decision through the channel only bought a
    hand-over hop and a one-turn delay before the fold took effect.
  * **The hand-over queue carries what has no task to ride on**: the
    `/delegate` seed and the hand-back recap/retire, both of which happen
    between turns.

Ordering is the router's FIFO turn lease (§6.4) rather than a suite lock, so
running a second channel instance no longer needs Valkey.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from bp_agents.channel.store import StoreError
from bp_agents.common.payloads import MessagePayload
from bp_protocol.frames import (
    GetStateOp,
    HandOverOp,
    SetStateOp,
    StatThreadOp,
)
from bp_protocol.types import TaskStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bp_agents.channel.store import SessionStore

logger = logging.getLogger(__name__)

ORCHESTRATOR_AGENT_ID = "orchestrator"
MEMORY_AGENT_ID = "memory"
HISTORY_SUMMARIZER_AGENT_ID = "history_summarizer"

# Session-scoped state keys the channel owns. Session state is a flat
# `(session_id, key)` namespace any agent in the session can read, so the keys
# are prefixed by intent rather than by writer.
DELEGATED_TO = "delegated_to"

# Hand-over item kinds. Opaque to the router; this is the suite's vocabulary.
SEED_ITEM = "seed"        # channel → delegate: start here, with this context
RECAP_ITEM = "recap"      # channel → orchestrator: what the delegate did
RETIRE_ITEM = "retire"    # channel → delegate: your episode is over

# Turn-lease tuning. The TTL is what a dead holder costs the next waiter, so
# it wants to be comfortably longer than a slow turn but not so long that a
# crashed channel wedges the session for minutes.
_LEASE_TTL_MS = 300_000
_LEASE_WAIT_TIMEOUT_S = 300.0


def pretty_agent(agent_id: str) -> str:
    """`computer_use` → `Computer Use` — a human-readable specialist name."""
    return agent_id.replace("_", " ").title()


class SessionBusy(RuntimeError):
    """The turn lease could not be taken before the wait timeout. The session
    is genuinely occupied — a frontend should tell the user, not retry
    silently, because the queue is already FIFO and a retry goes to the back."""


class ChannelCore:
    """Shared, transport-free channel logic. One instance per channel
    process; frontends call into it (see module docstring)."""

    def __init__(
        self,
        *,
        dispatcher: Any,
        store: SessionStore,
        delegatable_agents: frozenset[str] = frozenset(),
        result_timeout_s: float = 600.0,
        fire_memory: bool = False,
        lease_wait_timeout_s: float = _LEASE_WAIT_TIMEOUT_S,
    ) -> None:
        self._dispatcher = dispatcher
        self._store = store
        self._delegatable = delegatable_agents
        self._result_timeout_s = result_timeout_s
        self._fire_memory = fire_memory
        self._lease_wait_timeout_s = lease_wait_timeout_s
        # Detached fire-and-forget memory.add tasks (tracked for cleanup).
        self._memory_tasks: set[asyncio.Task] = set()
        # Detached fire-and-forget session-name tasks (first-turn titling).
        self._name_tasks: set[asyncio.Task] = set()

    @property
    def delegatable_agents(self) -> frozenset[str]:
        """The agent ids a user may `/delegate` to (the channel's allow-list).
        Exposed so a frontend can render the delegation picker."""
        return self._delegatable

    @property
    def store(self) -> SessionStore:
        """The steward view of the session store, for frontends that need to
        render a transcript or read session metadata."""
        return self._store

    # -- session serialization ------------------------------------------

    @contextlib.asynccontextmanager
    async def turn(
        self, user_id: str, session_id: str
    ) -> AsyncIterator[str]:
        """Hold the session's turn lease for one turn.

        The router queues waiters FIFO by ticket, so polling here does not
        cost fairness — the ticket, not the retry timing, decides who runs
        next. A steward has no socket for the router to push a promotion to,
        which is why this polls at all; each 409 carries the current holder's
        remaining TTL as `Retry-After`.

        Yields the holder id. A fresh one per turn: two turns in one process
        must not be able to release each other's lease."""
        holder = f"channel:{uuid.uuid4().hex[:12]}"
        deadline = asyncio.get_running_loop().time() + self._lease_wait_timeout_s
        while True:
            granted, retry_after = await self._store.acquire_lease(
                user_id=user_id, session_id=session_id,
                holder_id=holder, ttl_ms=_LEASE_TTL_MS,
            )
            if granted:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SessionBusy(session_id)
            await asyncio.sleep(min(retry_after, remaining))
        try:
            yield holder
        finally:
            # Release even on failure: holding the lease through a crashed
            # turn would wedge the session until its TTL, and the next waiter
            # is already queued behind us.
            with contextlib.suppress(Exception):
                await self._store.release_lease(
                    user_id=user_id, session_id=session_id, holder_id=holder
                )

    # -- routing ---------------------------------------------------------

    async def route(self, user_id: str, session_id: str) -> tuple[str, str]:
        """`(dest, mode)` for the next turn: the delegate during an active
        delegation, else the orchestrator."""
        delegate = await self._session_state(user_id, session_id, DELEGATED_TO)
        if delegate:
            return delegate, "delegated_message"
        return ORCHESTRATOR_AGENT_ID, "message"

    async def _session_state(
        self, user_id: str, session_id: str, key: str
    ) -> str | None:
        try:
            results = await self._store.ops(
                user_id=user_id, session_id=session_id,
                ops=[GetStateOp(session_scoped=True, keys=[key])],
            )
        except StoreError:
            logger.warning(
                "channel_state_read_failed",
                extra={"event": "channel_state_read_failed",
                       "bp.session_id": session_id, "key": key},
            )
            return None
        for entry in results[0].state or []:
            if entry.key == key:
                return entry.value
        return None

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

    # -- post-turn: delegated_to maintenance -----------------------------

    async def after_result(
        self, user_id: str, session_id: str, dest: str, result: Any
    ) -> None:
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
        if update is None:
            return
        with contextlib.suppress(StoreError):
            await self._store.ops(
                user_id=user_id, session_id=session_id,
                ops=[SetStateOp(
                    session_scoped=True, key=DELEGATED_TO, value=update[0]
                )],
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
        async with self.turn(user_id, session_id):
            current = await self._session_state(user_id, session_id, DELEGATED_TO)
            if current == target:
                return f"Already delegated to {pretty_agent(target)}."
            if current:  # implicit switch — fold the current one back first
                await self._fold_back(user_id, session_id, current)
            summary = await self._summarize_thread(
                user_id, session_id, ORCHESTRATOR_AGENT_ID
            )
            seed = (
                "## Conversation so far (summarized)\n"
                f"{summary or '(no prior conversation)'}\n\n"
                "The user has delegated this conversation to you; continue "
                "helping them directly."
            )
            # One batch: the seed and the routing flag commit together, or a
            # crash leaves a seed nobody will be routed to (or a delegation
            # flag with no context behind it).
            await self._store.ops(
                user_id=user_id, session_id=session_id,
                ops=[
                    HandOverOp(
                        target_agent_id=target, item_kind=SEED_ITEM,
                        payload={"text": seed},
                    ),
                    SetStateOp(
                        session_scoped=True, key=DELEGATED_TO, value=target
                    ),
                ],
            )
        return (
            f"Delegated to {pretty_agent(target)} — it'll handle your messages "
            "until /undelegate."
        )

    async def undelegate(self, user_id: str, session_id: str) -> str:
        """Return the session to the main assistant (summarize the delegate
        thread into a recap, retire the episode). Returns the message."""
        async with self.turn(user_id, session_id):
            current = await self._session_state(user_id, session_id, DELEGATED_TO)
            if not current:
                return "You're already with the main assistant."
            await self._fold_back(user_id, session_id, current)
        return f"Returned to the main assistant (was {pretty_agent(current)})."

    async def _summarize_thread(
        self, user_id: str, session_id: str, agent_id: str
    ) -> str:
        """Best-effort summary of one agent's active thread, produced by the
        summarizer agent reading that thread itself.

        Returns '' on an empty thread or a summarizer failure — a switch the
        user asked for must never be blocked by a background LLM call."""
        from bp_agents.agents.history_summarizer import SummarizeThread  # noqa: PLC0415

        try:
            task_id = await self._dispatcher.spawn_root_for_user(
                HISTORY_SUMMARIZER_AGENT_ID,
                SummarizeThread(agent_id=agent_id),
                user_id=user_id, session_id=session_id, mode="summarize_thread",
            )
            result = await self.await_result(task_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "delegation_summarize_failed",
                extra={"event": "delegation_summarize_failed",
                       "bp.session_id": session_id, "agent_id": agent_id},
            )
            return ""
        return (result.output.content if result.output else "") or ""

    async def _fold_back(
        self, user_id: str, session_id: str, delegate: str
    ) -> None:
        """End a delegation: recap the delegate's work to the orchestrator,
        retire the delegate's episode, and clear the routing flag.

        Both messages are hand-overs, not writes: the channel reaches neither
        thread. The orchestrator materialises the recap as its own hidden
        `user` row on its next turn (the specialist's results are external
        input, not the orchestrator's work); the delegate applies the retire
        as a floor on its own thread the next time it runs. A delegate that
        never runs again keeps a stale window it will never read — harmless,
        and the alternative is letting the channel move another agent's
        floor."""
        summary = await self._summarize_thread(user_id, session_id, delegate)
        # The retire cutoff: everything in the delegate's thread as of now.
        stat = await self._store.ops(
            user_id=user_id, session_id=session_id,
            ops=[StatThreadOp(owner_agent_id=delegate)],
        )
        through_id = stat[0].stat.last_message_id if stat[0].stat else 0
        recap = f"[Returned from {pretty_agent(delegate)}] {summary or '(no summary)'}"
        await self._store.ops(
            user_id=user_id, session_id=session_id,
            ops=[
                HandOverOp(
                    target_agent_id=ORCHESTRATOR_AGENT_ID, item_kind=RECAP_ITEM,
                    payload={"delegate": delegate, "text": recap},
                ),
                HandOverOp(
                    target_agent_id=delegate, item_kind=RETIRE_ITEM,
                    payload={"through_id": through_id},
                ),
                SetStateOp(session_scoped=True, key=DELEGATED_TO, value=None),
            ],
        )

    # -- session titling + memory ----------------------------------------

    def fire_name_session(
        self, user_id: str, session_id: str, user_prompt: str
    ) -> None:
        """Title the conversation from its first message, fire-and-forget,
        OUTSIDE the turn lease. A no-op once the session already has a title,
        so it effectively runs only on the first turn. Best-effort: a failure
        leaves the title unset and a later turn retries."""
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
        from bp_agents.agents.history_summarizer import NameSession  # noqa: PLC0415

        try:
            # `patch_metadata` with an empty patch reads the session back
            # without changing it — cheaper than a list call and it 404s on a
            # session that is gone, which is the other thing we care about.
            metadata = await self._store.patch_metadata(
                user_id=user_id, session_id=session_id, patch={}
            )
            if metadata.get("title"):
                return  # already named — nothing to do
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
            await self._store.patch_metadata(
                user_id=user_id, session_id=session_id, patch={"title": title}
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "session_name_failed", extra={"event": "session_name_failed"}
            )

    def fire_memory_add(
        self, user_id: str, session_id: str, user_prompt: str, reply: str
    ) -> None:
        """Spawn `memory.add` for the turn, fire-and-forget, OUTSIDE the
        turn lease ([overview.md] §2.2). No-op unless `fire_memory` and a
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
