"""Router-managed session store — `docs/design/router-managed-session-store.md`.

Three layers, because the design's central claim is a *structural* one and
each layer proves a different part of it:

  * **Structural** (no DB): the wire format has nowhere to name another
    agent's thread. If `AppendOp` ever grows an owner field, or the HTTP
    surface grows a message-POST, the ownership rule stops being a property
    of the protocol and becomes a check someone can forget.
  * **Store** (gated on `TEST_DB_URL`): the ops themselves — derived
    ownership, atomic batches, the floor cursor, hand-overs, the FIFO
    lease, and the concurrency guards that make interleaving detectable.
  * **SDK**: the batch builder and the ordered-by-default turn helper.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import asyncpg
import pytest

from bp_protocol.frames import (
    AcquireLeaseOp,
    AppendOp,
    AssertThreadOp,
    ConsumeHandoversOp,
    GetStateOp,
    HandOverOp,
    ListThreadsOp,
    ReadOp,
    RedactOp,
    ReleaseLeaseOp,
    RenewLeaseOp,
    SessionOpFrame,
    SetFloorOp,
    SetStateOp,
    StatThreadOp,
    parse_frame,
)
from bp_router.session_store import (
    SessionStoreError,
    StoreScope,
    execute_batch,
)

# ---------------------------------------------------------------------------
# Structural — the rule is the wire format, not a check
# ---------------------------------------------------------------------------


def test_append_op_cannot_name_a_thread_owner() -> None:
    """The load-bearing property: an agent writes its own threads because
    there is no field in which to name another's. A regression here would
    silently reopen cross-thread authorship."""
    fields = set(AppendOp.model_fields)
    assert "owner_agent_id" not in fields
    assert "author_agent_id" not in fields
    assert "agent_id" not in fields
    # ...and unknown keys are rejected rather than ignored, so a caller
    # can't smuggle one past validation.
    with pytest.raises(Exception):
        AppendOp(role="user", content="x", owner_agent_id="other")


def test_write_ops_have_no_owner_field_at_all() -> None:
    for op in (AppendOp, SetFloorOp, RedactOp):
        assert not {"owner_agent_id", "author_agent_id"} & set(op.model_fields), op


def test_read_ops_do_take_an_owner() -> None:
    """Reads are session-scoped on purpose — a summarizer must read the
    thread it summarizes, and reading cannot fabricate an utterance."""
    for op in (ReadOp, StatThreadOp, AssertThreadOp, ListThreadsOp):
        assert "owner_agent_id" in op.model_fields, op


def test_no_message_post_endpoint_on_the_steward_surface() -> None:
    """A steward with a session JWT must not be able to append anywhere.
    Removing the endpoint — not gating it — is what makes that structural."""
    from bp_router.api import sessions as sessions_api

    src = inspect.getsource(sessions_api)
    assert '@router.post("/{session_id}/messages"' not in src
    assert '@router.get("/{session_id}/messages"' in src
    assert '@router.post("/{session_id}/handovers"' in src


def test_steward_batches_run_with_no_owning_agent() -> None:
    """The HTTP path passes `agent_id=None`, so every thread-write refuses
    inside the store — no endpoint-level allowlist to keep in sync."""
    from bp_router.api import sessions as sessions_api

    src = inspect.getsource(sessions_api._run_steward_batch)
    assert "agent_id=None" in src


def test_session_op_frame_round_trips() -> None:
    frame = SessionOpFrame(
        agent_id="a",
        trace_id="t",
        span_id="s",
        task_id="task_1",
        ops=[AppendOp(role="user", content="hi"), ReadOp(roles=["user"])],
    )
    back = parse_frame(frame.model_dump(mode="json"))
    assert [op.kind for op in back.ops] == ["append", "read"]


def test_welcome_features_is_additive() -> None:
    """Feature advertising must not break an older SDK: the field is
    defaulted, and `capabilities` (the agent's own list) is left alone."""
    from bp_protocol.frames import WelcomeFrame

    w = WelcomeFrame(agent_id="router", trace_id="t", span_id="s", session_id="s1")
    assert w.features == []
    assert w.capabilities == []


# ---------------------------------------------------------------------------
# Store — against a real Postgres
# ---------------------------------------------------------------------------


async def _pool(dsn: str) -> asyncpg.Pool:
    async def init(conn: asyncpg.Connection) -> None:
        for typ in ("jsonb", "json"):
            await conn.set_type_codec(
                typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

    return await asyncpg.create_pool(dsn, init=init)


async def _fresh(conn: asyncpg.Connection, tag: str) -> tuple[str, str]:
    user_id, session_id = f"u_{tag}", f"s_{tag}"
    # Sessions first: `sessions.user_id` has no ON DELETE CASCADE, so the
    # user row can't go while a session references it. Dropping the session
    # takes the store rows with it (that cascade is the design's §10.1).
    await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM session_messages WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM session_threads WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
    await conn.execute(
        "INSERT INTO users (user_id, level, auth_kind) VALUES ($1, 'tier1', 'password')",
        user_id,
    )
    await conn.execute(
        "INSERT INTO sessions (session_id, user_id) VALUES ($1, $2)",
        session_id,
        user_id,
    )
    return user_id, session_id


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_append_and_read_round_trip(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "rt")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                out = await execute_batch(
                    conn,
                    scope,
                    [
                        AppendOp(role="user", content="hello"),
                        AppendOp(role="assistant", content="hi there"),
                        ReadOp(roles=["user", "assistant"]),
                    ],
                    task_id="task_1",
                )
            msgs = out.results[2].messages
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert [m.content for m in msgs] == ["hello", "hi there"]
            # A read reports the cursor an AssertThread will later check.
            assert out.results[2].last_message_id == out.results[1].message_id
        await pool.close()

    _run(go())


def test_idempotent_append_survives_task_redelivery(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "idem")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                first = await execute_batch(
                    conn,
                    scope,
                    [AppendOp(role="user", content="hi", idempotency_key="input")],
                    task_id="task_1",
                )
            async with conn.transaction():
                again = await execute_batch(
                    conn,
                    scope,
                    [AppendOp(role="user", content="hi", idempotency_key="input")],
                    task_id="task_1",
                )
            assert again.results[0].message_id == first.results[0].message_id
            # Without a key the same task may append many rows of one role —
            # a tool_call and its tool_result, two calls in one turn.
            async with conn.transaction():
                await execute_batch(
                    conn,
                    scope,
                    [
                        AppendOp(role="tool_call", content="a"),
                        AppendOp(role="tool_call", content="b"),
                    ],
                    task_id="task_1",
                )
                out = await execute_batch(
                    conn, scope, [ReadOp(roles=["tool_call"])]
                )
            assert len(out.results[0].messages) == 2
        await pool.close()

    _run(go())


def test_steward_cannot_append_but_can_enqueue(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "stew")
            steward = StoreScope(user, session, None)
            owner = StoreScope(user, session, "orchestrator")
            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(
                        conn, steward, [AppendOp(role="assistant", content="forged")]
                    )
            assert exc.value.code == "denied"

            async with conn.transaction():
                await execute_batch(
                    conn,
                    steward,
                    [
                        HandOverOp(
                            target_agent_id="orchestrator",
                            item_kind="input",
                            payload={"text": "next message"},
                        )
                    ],
                )
                drained = await execute_batch(conn, owner, [ConsumeHandoversOp()])
            items = drained.results[0].items
            assert len(items) == 1
            assert items[0].payload["text"] == "next message"
            # An item is consumed once: a second drain is empty, so a
            # redelivered turn can't materialise it twice.
            async with conn.transaction():
                empty = await execute_batch(conn, owner, [ConsumeHandoversOp()])
            assert empty.results[0].items == []
        await pool.close()

    _run(go())


def test_only_the_target_drains_its_queue(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "queue")
            orch = StoreScope(user, session, "orchestrator")
            other = StoreScope(user, session, "research")
            async with conn.transaction():
                await execute_batch(
                    conn,
                    orch,
                    [HandOverOp(target_agent_id="orchestrator", item_kind="recap")],
                )
                stolen = await execute_batch(conn, other, [ConsumeHandoversOp()])
            assert stolen.results[0].items == []
        await pool.close()

    _run(go())


def test_threads_are_isolated_but_readable(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "iso")
            orch = StoreScope(user, session, "orchestrator")
            deleg = StoreScope(user, session, "research")
            async with conn.transaction():
                await execute_batch(conn, orch, [AppendOp(role="assistant", content="mine")])
                await execute_batch(conn, deleg, [AppendOp(role="assistant", content="theirs")])
                own = await execute_batch(conn, orch, [ReadOp(roles=["assistant"])])
                peer = await execute_batch(
                    conn, orch, [ReadOp(owner_agent_id="research", roles=["assistant"])]
                )
            assert [m.content for m in own.results[0].messages] == ["mine"]
            # Cross-thread READS are intentional: reading cannot fabricate.
            assert [m.content for m in peer.results[0].messages] == ["theirs"]
        await pool.close()

    _run(go())


def test_assert_thread_detects_an_interleaved_append(test_db_url: str) -> None:
    """The safety floor for a lock-free suite: a turn that raced another
    turn is refused, not silently woven into the transcript."""

    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "assert")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                out = await execute_batch(conn, scope, [AppendOp(role="user", content="q1")])
            stale_cursor = out.results[0].message_id
            async with conn.transaction():
                await execute_batch(conn, scope, [AppendOp(role="user", content="q2")])

            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(
                        conn,
                        scope,
                        [
                            AssertThreadOp(last_message_id=stale_cursor),
                            AppendOp(role="assistant", content="stale reply"),
                        ],
                    )
            assert exc.value.code == "thread_conflict"
            assert exc.value.index == 0

            async with conn.transaction():
                out = await execute_batch(conn, scope, [ReadOp(roles=["assistant"])])
            # The refused batch rolled back whole — no partial append.
            assert out.results[0].messages == []
        await pool.close()

    _run(go())


def test_summarize_apply_is_atomic_and_cas_guarded(test_db_url: str) -> None:
    """The failure this store exists to make impossible: two overlapping
    summarize passes retiring rows whose surviving summary doesn't cover
    them. The loser's CAS fails, and its floor move rolls back with it."""

    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "summ")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                for i in range(4):
                    await execute_batch(
                        conn, scope, [AppendOp(role="user", content=f"turn {i}")]
                    )
                stat = await execute_batch(conn, scope, [StatThreadOp()])
            cutoff = stat.results[0].stat.last_message_id

            async with conn.transaction():
                await execute_batch(
                    conn,
                    scope,
                    [
                        SetStateOp(key="summary", value="S1", expected_version=0),
                        SetFloorOp(floor_id=cutoff),
                    ],
                )

            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(
                        conn,
                        scope,
                        [
                            SetStateOp(key="summary", value="S2", expected_version=0),
                            SetFloorOp(floor_id=cutoff + 50),
                        ],
                    )
            assert exc.value.code == "version_conflict"

            async with conn.transaction():
                out = await execute_batch(
                    conn, scope, [StatThreadOp(), GetStateOp(keys=["summary"])]
                )
            assert out.results[0].stat.floor_id == cutoff  # loser's floor rolled back
            assert out.results[1].state[0].value == "S1"
            assert out.results[1].state[0].version == 1
        await pool.close()

    _run(go())


def test_floor_retires_the_window_but_keeps_the_record(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "floor")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                for i in range(3):
                    await execute_batch(
                        conn, scope, [AppendOp(role="user", content=f"m{i}")]
                    )
                stat = await execute_batch(conn, scope, [StatThreadOp()])
                await execute_batch(
                    conn, scope, [SetFloorOp(floor_id=stat.results[0].stat.last_message_id)]
                )
                active = await execute_batch(conn, scope, [ReadOp()])
                full = await execute_batch(conn, scope, [ReadOp(include_retired=True)])
                after = await execute_batch(conn, scope, [StatThreadOp()])
            assert active.results[0].messages == []
            assert len(full.results[0].messages) == 3
            # Counters follow the active window, so folding reclaims quota.
            assert after.results[0].stat.message_count == 0
            assert after.results[0].stat.content_bytes == 0
        await pool.close()

    _run(go())


def test_floor_regression_is_refused(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "floorreg")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                out = await execute_batch(conn, scope, [AppendOp(role="user", content="m")])
                await execute_batch(
                    conn, scope, [SetFloorOp(floor_id=out.results[0].message_id)]
                )
            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(conn, scope, [SetFloorOp(floor_id=0)])
            assert exc.value.code == "floor_regression"
        await pool.close()

    _run(go())


def test_redaction_blanks_content_and_keeps_the_id(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "redact")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                out = await execute_batch(
                    conn, scope, [AppendOp(role="user", content="secret")]
                )
                mid = out.results[0].message_id
                await execute_batch(conn, scope, [RedactOp(message_ids=[mid])])
                full = await execute_batch(conn, scope, [ReadOp(include_retired=True)])
            row = next(m for m in full.results[0].messages if m.id == mid)
            assert row.content == ""
            assert row.redacted is True
            stored = await conn.fetchval(
                "SELECT content FROM session_messages WHERE id = $1", mid
            )
            assert stored == ""  # blanked at rest, not merely filtered
        await pool.close()

    _run(go())


def test_read_budget_truncates_newest_first_and_pages(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "budget")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                for _ in range(10):
                    await execute_batch(
                        conn, scope, [AppendOp(role="page", content="x" * 500)]
                    )
                page1 = await execute_batch(
                    conn, scope, [ReadOp(roles=["page"], max_bytes=1200)]
                )
            first = page1.results[0]
            assert len(first.messages) == 2  # the two NEWEST fit the budget
            assert first.truncated_before_id == min(m.id for m in first.messages)
            async with conn.transaction():
                page2 = await execute_batch(
                    conn,
                    scope,
                    [
                        ReadOp(
                            roles=["page"],
                            before_id=first.truncated_before_id,
                            max_bytes=1200,
                        )
                    ],
                )
            assert max(m.id for m in page2.results[0].messages) < first.truncated_before_id
        await pool.close()

    _run(go())


def test_oversized_message_is_refused(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "big")
            scope = StoreScope(user, session, "orchestrator")
            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(
                        conn, scope, [AppendOp(role="user", content="y" * (256 * 1024 + 1))]
                    )
            assert exc.value.code == "content_too_large"
        await pool.close()

    _run(go())


def test_quota_is_enforced_on_append(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "quota")
            scope = StoreScope(user, session, "orchestrator")
            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(
                        conn, scope, [AppendOp(role="user", content="z" * 50)],
                        quota_ceiling=10,
                    )
            assert exc.value.code == "quota_exceeded"
        await pool.close()

    _run(go())


def test_user_scope_is_separate_and_survives_session_purge(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "scope")
            session_scope = StoreScope(user, session, "orchestrator")
            user_scope = StoreScope(user, None, "orchestrator")
            async with conn.transaction():
                await execute_batch(
                    conn, session_scope, [AppendOp(role="user", content="in session")]
                )
                await execute_batch(
                    conn, user_scope, [AppendOp(role="note", content="standing")]
                )
                leak = await execute_batch(conn, session_scope, [ReadOp(roles=["note"])])
            assert leak.results[0].messages == []

            await conn.execute("DELETE FROM sessions WHERE session_id = $1", session)
            for table in (
                "session_messages",
                "session_threads",
                "session_state",
                "session_handovers",
                "session_turn_queue",
            ):
                left = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE session_id = $1",  # noqa: S608
                    session,
                )
                assert left == 0, table
            async with conn.transaction():
                survived = await execute_batch(conn, user_scope, [ReadOp(roles=["note"])])
            assert [m.content for m in survived.results[0].messages] == ["standing"]
        await pool.close()

    _run(go())


def test_lease_is_fifo_and_promotes_on_release(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "lease")
            a = StoreScope(user, session, "orchestrator")
            b = StoreScope(user, session, "research")
            async with conn.transaction():
                first = await execute_batch(conn, a, [AcquireLeaseOp(holder_id="turn_1")])
            assert first.results[0].lease.granted

            async with conn.transaction():
                second = await execute_batch(conn, b, [AcquireLeaseOp(holder_id="turn_2")])
            lease = second.results[0].lease
            assert lease.granted is False
            # The busy reply carries the deadline a waiter arms its fallback
            # timer on, for a holder that dies without releasing.
            assert lease.holder_expires_at is not None
            ticket = lease.ticket

            async with conn.transaction():
                released = await execute_batch(conn, a, [ReleaseLeaseOp(holder_id="turn_1")])
            assert len(released.promotions) == 1
            assert released.promotions[0].agent_id == "research"
            assert released.promotions[0].ticket == ticket

            async with conn.transaction():
                retry = await execute_batch(conn, b, [AcquireLeaseOp(holder_id="turn_2")])
            # Same ticket: a retry never loses its place in line, which is
            # why backoff timing cannot reorder two in-order messages.
            assert retry.results[0].lease.granted
            assert retry.results[0].lease.ticket == ticket
        await pool.close()

    _run(go())


def test_lease_renew_after_loss_reports_lease_lost(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "leaselost")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                await execute_batch(conn, scope, [AcquireLeaseOp(holder_id="t")])
                await execute_batch(conn, scope, [ReleaseLeaseOp(holder_id="t")])
            with pytest.raises(SessionStoreError) as exc:
                async with conn.transaction():
                    await execute_batch(conn, scope, [RenewLeaseOp(holder_id="t")])
            assert exc.value.code == "lease_lost"
        await pool.close()

    _run(go())


def test_expired_holder_is_promoted_past(test_db_url: str) -> None:
    """A holder that dies without releasing must not wedge the session: the
    next acquire expires it and promotes the waiting ticket."""

    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "leaseexp")
            a = StoreScope(user, session, "orchestrator")
            b = StoreScope(user, session, "research")
            async with conn.transaction():
                await execute_batch(conn, a, [AcquireLeaseOp(holder_id="dead", ttl_ms=1_000)])
                await execute_batch(conn, b, [AcquireLeaseOp(holder_id="waiter")])
            # Simulate the holder's TTL running out with nobody to release it.
            await conn.execute(
                "UPDATE session_turn_queue SET expires_at = now() - interval '1 second' "
                "WHERE holder_id = 'dead'"
            )
            async with conn.transaction():
                out = await execute_batch(conn, b, [AcquireLeaseOp(holder_id="waiter")])
            assert out.results[0].lease.granted
        await pool.close()

    _run(go())


def test_batch_read_sees_writes_before_it(test_db_url: str) -> None:
    async def go() -> None:
        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            user, session = await _fresh(conn, "order")
            scope = StoreScope(user, session, "orchestrator")
            async with conn.transaction():
                out = await execute_batch(
                    conn,
                    scope,
                    [
                        AppendOp(role="user", content="just written"),
                        ReadOp(roles=["user"]),
                    ],
                )
            assert [m.content for m in out.results[1].messages] == ["just written"]
        await pool.close()

    _run(go())


# ---------------------------------------------------------------------------
# SDK
# ---------------------------------------------------------------------------


class _FakeAgent:
    class info:  # noqa: N801
        agent_id = "orchestrator"


class _FakeDispatcher:
    """Captures frames and replies with canned results, so the builder and
    the turn helper can be exercised without a router.

    The future is registered BEFORE the frame is sent (the real SDK's
    order), so the reply is resolved in `send` — where the ops are finally
    visible and a per-op result can be synthesised.
    """

    def __init__(self, replies: list[Any] | None = None) -> None:
        self.agent = _FakeAgent()
        self.sent: list[Any] = []
        self._replies = replies or []
        self._pending: list[asyncio.Future] = []
        self.pending_acks = object()

        class _T:
            def __init__(self, outer: _FakeDispatcher) -> None:
                self._outer = outer

            async def send(self, frame: Any) -> None:
                self._outer._deliver(frame)

        self.transport = _T(self)

    def _deliver(self, frame: Any) -> None:
        from bp_protocol.frames import SessionOpResult, SessionResultFrame

        self.sent.append(frame)
        reply = self._replies.pop(0) if self._replies else None
        if reply is None:
            # Results are positional and the SDK checks the count, so a fake
            # must answer one per op — the router's contract.
            reply = [SessionOpResult(kind=op.kind) for op in frame.ops]
        fut, cid = self._pending.pop(0)
        if not fut.done():
            fut.set_result(
                SessionResultFrame(
                    agent_id="router",
                    trace_id="t",
                    span_id="s",
                    ref_correlation_id=cid,
                    results=reply,
                )
            )

    def register_for_task(self, _pmap: Any, correlation_id: str, _task_id: str) -> Any:
        fut = asyncio.get_event_loop().create_future()
        self._pending.append((fut, correlation_id))
        return fut


def _ctx() -> Any:
    class _Ctx:
        task_id = "task_1"
        trace_id = "t"
        span_id = "s"

    return _Ctx()


def test_sdk_batch_builds_one_frame_in_order() -> None:
    from bp_sdk.history import SessionHistory

    async def go() -> None:
        d = _FakeDispatcher()
        hist = SessionHistory(_ctx(), dispatcher=d)
        async with hist.batch() as b:
            b.consume_handovers()
            b.append("user", "hi", idempotency_key="input")
            b.read(roles=["user", "assistant"])
        assert len(d.sent) == 1  # ONE round trip for the whole turn-open
        assert [op.kind for op in d.sent[0].ops] == [
            "consume_handovers",
            "append",
            "read",
        ]

    _run(go())


def test_sdk_append_has_no_owner_parameter() -> None:
    """The SDK must not reintroduce what the wire format removed."""
    from bp_sdk.history import SessionBatch, SessionHistory

    for fn in (SessionHistory.append, SessionBatch.append):
        params = set(inspect.signature(fn).parameters)
        assert not {"owner", "owner_agent_id", "agent_id"} & params, fn
    # Reads keep it — reads are session-scoped by design.
    assert "owner" in inspect.signature(SessionHistory.read).parameters


def test_sdk_turn_is_ordered_by_default() -> None:
    from bp_sdk.history import SessionHistory

    sig = inspect.signature(SessionHistory.turn)
    assert sig.parameters["ordered"].default is True


def test_sdk_turn_acquires_and_releases_the_lease() -> None:
    from bp_protocol.frames import LeaseStatus, SessionOpResult
    from bp_sdk.history import SessionHistory

    async def go() -> None:
        granted = [
            SessionOpResult(kind="acquire_lease", lease=LeaseStatus(granted=True, ticket=1))
        ]
        d = _FakeDispatcher(replies=[granted])
        hist = SessionHistory(_ctx(), dispatcher=d)
        async with hist.turn(holder_id="turn_1") as t:
            async with t.batch() as b:
                b.append("assistant", "done")
        kinds = [[op.kind for op in f.ops] for f in d.sent]
        assert kinds[0] == ["acquire_lease"]
        assert kinds[1] == ["append"]
        assert kinds[-1] == ["release_lease"]

    _run(go())


def test_sdk_unordered_turn_takes_no_lease() -> None:
    from bp_sdk.history import SessionHistory

    async def go() -> None:
        d = _FakeDispatcher()
        hist = SessionHistory(_ctx(), dispatcher=d)
        async with hist.turn(ordered=False) as t:
            async with t.batch() as b:
                b.append("assistant", "done")
        kinds = [op.kind for f in d.sent for op in f.ops]
        assert "acquire_lease" not in kinds
        assert kinds == ["append"]

    _run(go())


def test_sdk_raises_typed_error_with_op_index() -> None:
    """A refused batch surfaces as a typed error naming the op — the caller
    needs `thread_conflict` at op 0 to know a retry is the right response."""
    from bp_protocol.frames import SessionResultFrame
    from bp_sdk.history import SessionHistory
    from bp_sdk.history import SessionStoreError as SdkError

    class _Refusing(_FakeDispatcher):
        def _deliver(self, frame: Any) -> None:
            self.sent.append(frame)
            fut, cid = self._pending.pop(0)
            fut.set_result(
                SessionResultFrame(
                    agent_id="router",
                    trace_id="t",
                    span_id="s",
                    ref_correlation_id=cid,
                    error="thread_conflict",
                    error_index=0,
                )
            )

    async def go() -> None:
        hist = SessionHistory(_ctx(), dispatcher=_Refusing())
        with pytest.raises(SdkError) as exc:
            await hist.append("assistant", "x")
        assert exc.value.code == "thread_conflict"
        assert exc.value.op_index == 0

    _run(go())


def test_sdk_user_scope_switches_the_frame_scope() -> None:
    from bp_sdk.history import SessionHistory

    async def go() -> None:
        d = _FakeDispatcher()
        hist = SessionHistory(_ctx(), dispatcher=d)
        await hist.append("note", "standing")
        await hist.user_scope.append("note", "standing")
        assert d.sent[0].scope == "session"
        assert d.sent[1].scope == "user"

    _run(go())
