"""Suite Postgres layer — round-trips over `bp_agents.db.queries`.

Assumes the suite schema is applied to `SUITE_DATABASE_URL`
(`alembic -c alembic_suite.ini upgrade head`); truncates the suite
tables at the start of each test. Driven via `asyncio.run` so the file
works on CI matrices without pytest-asyncio (matches test_smoke_e2e).
"""

from __future__ import annotations

import asyncio

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


async def _truncate(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE session_history, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY"
        )


def test_suite_db_round_trips(suite_db_url: str) -> None:
    async def _drive() -> None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _truncate(pool)
            async with pool.acquire() as conn:
                # --- user_config: create + defaults + allowlist update ---
                cfg = await queries.create_user_config(
                    conn, user_id="usr_a", full_name="Ada",
                    timezone="Europe/London", preset_balanced="balanced",
                )
                assert cfg.full_name == "Ada"
                assert cfg.timezone == "Europe/London"
                assert cfg.preset_balanced == "balanced"
                assert cfg.verbose_default is False
                assert cfg.max_context_token_limit == 120_000

                # Idempotent — second create returns the existing row.
                again = await queries.create_user_config(
                    conn, user_id="usr_a", full_name="SHOULD-NOT-OVERWRITE",
                )
                assert again.full_name == "Ada"

                await queries.update_user_config(
                    conn, "usr_a", verbose_default=True, custom_note="be terse",
                )
                cfg2 = await queries.get_user_config(conn, "usr_a")
                assert cfg2 is not None
                assert cfg2.verbose_default is True
                assert cfg2.custom_note == "be terse"

                # Non-mutable / unknown column is rejected, not silently dropped.
                rejected = False
                try:
                    await queries.update_user_config(conn, "usr_a", bogus_col=1)
                except ValueError:
                    rejected = True
                assert rejected

                # --- session_info: create + patch + clear-to-NULL ---
                si = await queries.create_session_info(
                    conn, session_id="ses_1", user_id="usr_a",
                    channel="chatbot_telegram", chat_id="tg-42",
                )
                assert si.delegated_to is None
                await queries.update_session_info(
                    conn, "ses_1", delegated_to="computer_use",
                    history_summary="…summary…",
                )
                si2 = await queries.get_session_info(conn, "ses_1")
                assert si2 is not None
                assert si2.delegated_to == "computer_use"
                assert si2.history_summary == "…summary…"
                # Hand-back clears the delegate (None → SQL NULL).
                await queries.update_session_info(conn, "ses_1", delegated_to=None)
                si3 = await queries.get_session_info(conn, "ses_1")
                assert si3 is not None and si3.delegated_to is None

                await queries.set_default_session_id(
                    conn, user_id="usr_a", session_id="ses_1"
                )
                assert (await queries.get_user_config(conn, "usr_a")).default_session_id == "ses_1"  # noqa: E501

                # --- session_history: append, reload, demote ---
                u1 = await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="user", message="hi",
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="assistant", message="hello!",
                )
                # tool rows are never reloaded.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="orchestrator",
                    role="tool_call", message="call x", incumbent=True, hidden=True,
                )
                # a different thread (delegate) shouldn't leak into reload.
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="computer_use",
                    role="user", message="other thread",
                )

                reload = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                assert [r.role for r in reload] == ["user", "assistant"]
                assert [r.message for r in reload] == ["hi", "hello!"]

                # Demote the first turn out of the incumbent window.
                demoted = await queries.demote_incumbent_through(
                    conn, session_id="ses_1", agent_id="orchestrator", up_to_id=u1,
                )
                assert demoted == 1
                reload2 = await queries.reload_incumbent(
                    conn, session_id="ses_1", agent_id="orchestrator"
                )
                assert [r.role for r in reload2] == ["assistant"]

                # --- recent_tool_exchanges: paging + thread scoping ---
                for k in range(1, 4):  # 3 exchanges on the orchestrator thread
                    await queries.append_history(
                        conn, session_id="ses_1", agent_id="orchestrator",
                        role="tool_call", message=f'{{"name": "t{k}"}}',
                        incumbent=False, hidden=True,
                    )
                    await queries.append_history(
                        conn, session_id="ses_1", agent_id="orchestrator",
                        role="tool_result", message=f"res-{k}",
                        incumbent=False, hidden=True,
                    )
                # a different thread's exchange must never leak in
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="computer_use",
                    role="tool_call", message='{"name": "leak"}',
                    incumbent=False, hidden=True,
                )
                await queries.append_history(
                    conn, session_id="ses_1", agent_id="computer_use",
                    role="tool_result", message="LEAK", incumbent=False, hidden=True,
                )
                # newest first; newest is exchange 3
                page = await queries.recent_tool_exchanges(
                    conn, session_id="ses_1", agent_id="orchestrator", limit=1
                )
                assert len(page) == 1 and page[0][1].message == "res-3"
                # skip pages to the next-older with no overlap
                page = await queries.recent_tool_exchanges(
                    conn, session_id="ses_1", agent_id="orchestrator", limit=2, skip=1
                )
                assert [p[1].message for p in page] == ["res-1", "res-2"]
                # scoped to the thread — the computer_use LEAK never appears
                page = await queries.recent_tool_exchanges(
                    conn, session_id="ses_1", agent_id="orchestrator", limit=10
                )
                assert all("LEAK" not in p[1].message for p in page)
                assert len(page) == 3

                # --- platform mappings: upsert + resolve + re-bind ---
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-42", user_id="usr_a"
                )
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg-42"
                ) == "usr_a"
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="unmapped"
                ) is None
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-42", user_id="usr_b"
                )
                assert await queries.resolve_user_id(
                    conn, platform="telegram", chat_id="tg-42"
                ) == "usr_b"

                # --- per-chat current session (session_id) ---
                # Seeded on first insert; re-bind without a session_id keeps it
                # (COALESCE), so a later reconcile can't clobber a /new move.
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99",
                    user_id="usr_a", session_id="ses_seed",
                )
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m is not None and m.session_id == "ses_seed"
                await queries.upsert_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99", user_id="usr_a"
                )  # no session_id -> COALESCE preserves the existing one
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m.session_id == "ses_seed"
                # set_mapping_session_id moves it unconditionally (/new path).
                await queries.set_mapping_session_id(
                    conn, platform="telegram", chat_id="tg-99",
                    session_id="ses_moved",
                )
                m = await queries.get_platform_mapping(
                    conn, platform="telegram", chat_id="tg-99"
                )
                assert m.session_id == "ses_moved"
        finally:
            await pool.close()

    asyncio.run(_drive())
