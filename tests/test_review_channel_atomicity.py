"""ChannelCore multi-write sequences are crash-atomic.

Pre-release review: `maybe_summarize`, `delegate`, and `_fold_back` each
perform several dependent writes on a single acquired connection but without
`conn.transaction()`, so every statement autocommits independently. A crash
between them leaves inconsistent state (a summary written but the folded rows
not demoted; a delegate seed with no `delegated_to`; a cleared delegation
whose recap rows never landed; etc.).

Fix: each sequence is wrapped in `async with conn.transaction():`.

The behavioural test drives the real `ChannelCore.delegate` against the suite
DB and forces the second write to fail, asserting the first is rolled back.
Source pins cover the other two where `SUITE_DATABASE_URL` is unset.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from bp_agents.channel.core import ChannelCore
from bp_agents.db import queries


def _txn_wraps_writes(fn) -> bool:  # type: ignore[no-untyped-def]
    src = inspect.getsource(fn)
    return "async with conn.transaction():" in src


def test_maybe_summarize_is_transactional() -> None:
    assert _txn_wraps_writes(ChannelCore.maybe_summarize)


def test_delegate_is_transactional() -> None:
    assert _txn_wraps_writes(ChannelCore.delegate)


def test_fold_back_is_transactional() -> None:
    assert _txn_wraps_writes(ChannelCore._fold_back)


class _SummDispatcher:
    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):  # type: ignore[no-untyped-def]
        return "tsk"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):  # type: ignore[no-untyped-def]
        from bp_protocol.types import AgentOutput, ResultFrame, TaskStatus

        return ResultFrame(
            agent_id="history_summarizer", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="summarized"),
        )


def test_delegate_rolls_back_on_mid_sequence_failure(
    suite_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delegate` writes the delegate seed row and then sets `delegated_to`
    in one transaction. If the second write fails, the seed row must NOT
    persist (and `delegated_to` stays clear)."""
    from bp_agents.db.connection import open_pool
    from bp_agents.settings import SuiteSettings

    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE TABLE session_history, session_info, user_config, "
                    "suite_platform_mappings RESTART IDENTITY CASCADE"
                )
                await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a", channel="webapp"
                )

            core = ChannelCore(
                dispatcher=_SummDispatcher(), pool=pool,
                delegatable_agents=frozenset({"research"}),
            )

            # Make the SECOND write in delegate's transaction fail. The seed
            # `append_history` (first write) must roll back with it.
            monkeypatch.setattr(
                queries, "update_session_info",
                AsyncMock(side_effect=RuntimeError("boom")),
            )

            with pytest.raises(RuntimeError):
                await core.delegate("usr_a", "ses_1", "research")

            # Rollback proof: no delegate seed row landed, delegation not set.
            async with pool.acquire() as conn:
                seed_rows = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="research"
                )
                # get_session_info is read with the REAL query (we only
                # patched the writer), so this reflects committed state.
                info = await queries.get_session_info(conn, "ses_1")
            assert seed_rows == [], "delegate seed row must roll back with the failed flag write"
            assert info is not None and info.delegated_to is None
        finally:
            await pool.close()

    asyncio.run(_drive())
