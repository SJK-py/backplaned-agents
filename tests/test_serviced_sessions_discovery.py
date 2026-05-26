"""Service-principal discovery query — `list_serviced_sessions`.

The scoping is security-critical: a service principal must see ONLY the
sessions of users it services, never the whole table. Exercised against
a live router DB (constructing TestRouter also validates the new
`/v1/admin/serviced-sessions` route wires + imports cleanly).
"""

from __future__ import annotations

import asyncio

from bp_router.db import queries
from bp_sdk.testing import TestRouter


def test_list_serviced_sessions_scopes_to_caller(test_db_url: str) -> None:
    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            pool = router._app.state.bp.db_pool
            async with pool.acquire() as conn:
                svc = await queries.insert_user(
                    conn, level="service", auth_kind="api_key",
                    auth_secret_hash=None, email=None,
                )
                other_svc = await queries.insert_user(
                    conn, level="service", auth_kind="api_key",
                    auth_secret_hash=None, email=None,
                )
                serviced = await queries.insert_user(
                    conn, level="tier0", auth_kind="password",
                    auth_secret_hash=None, email=None, serviced_by=[svc.user_id],
                )
                not_serviced = await queries.insert_user(
                    conn, level="tier0", auth_kind="password",
                    auth_secret_hash=None, email=None, serviced_by=[other_svc.user_id],
                )

                s_serviced = await queries.Scope.user(
                    conn, serviced.user_id
                ).open_session(
                    metadata={"kind": "chatbot_telegram", "external_id": "tg42"}
                )
                # A second channel's session for the same serviced user.
                await queries.Scope.user(conn, serviced.user_id).open_session(
                    metadata={"kind": "webapp", "external_id": "web1"}
                )
                # A session for a user this principal does NOT service.
                await queries.Scope.user(conn, not_serviced.user_id).open_session(
                    metadata={"kind": "chatbot_telegram", "external_id": "tg99"}
                )

                # Scoped to svc + channel filter → only the serviced
                # user's telegram session.
                rows = await queries.list_serviced_sessions(
                    conn, service_user_id=svc.user_id, channel="chatbot_telegram"
                )
                assert [r["session_id"] for r in rows] == [s_serviced.session_id]
                assert rows[0]["user_id"] == serviced.user_id
                assert rows[0]["metadata"]["external_id"] == "tg42"

                # No channel filter → both of the serviced user's sessions,
                # never the un-serviced user's.
                all_rows = await queries.list_serviced_sessions(
                    conn, service_user_id=svc.user_id
                )
                assert len(all_rows) == 2
                assert all(r["user_id"] == serviced.user_id for r in all_rows)

                # `since` cursor excludes already-seen sessions.
                cursor = all_rows[-1]["opened_at"]
                assert (
                    await queries.list_serviced_sessions(
                        conn, service_user_id=svc.user_id, since=cursor
                    )
                    == []
                )

    asyncio.run(_drive())
