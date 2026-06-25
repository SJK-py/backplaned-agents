"""Boot-time preset catalogue re-sync (every boot, not just first boot).

`upsert_managed_preset` + `delete_stale_managed_presets` keep the
catalogue-owned (`managed = TRUE`) rows in lock-step with the JSONC
catalogue on every startup, while leaving admin-created presets
(`managed = FALSE`) untouched. The one carve-out is `min_user_level`,
which is operator-owned and preserved across re-syncs. These tests pin
that contract end-to-end against a real Postgres (skip when no DB)."""
from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg

from bp_router.db import queries


async def _connect(url: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(url)
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    return conn


def test_upsert_marks_managed_and_overwrites_on_resync(test_db_url: str) -> None:
    """A catalogue upsert creates the row as managed, and a second upsert
    with changed fields overwrites it (the every-boot re-sync)."""

    async def _drive() -> None:
        conn = await _connect(test_db_url)
        name = f"mgd_{uuid.uuid4().hex[:8]}"
        try:
            await queries.upsert_managed_preset(
                conn, name=name, description="v1", provider="gemini",
                concrete_model="gemini-2.5-flash", api_key_ref="env://X",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
            )
            row = await queries.get_llm_preset(conn, name)
            assert row is not None
            assert row.managed is True
            assert row.created_by is None
            assert row.description == "v1"
            assert row.concrete_model == "gemini-2.5-flash"

            # Re-sync with changed fields → overwrite in place.
            await queries.upsert_managed_preset(
                conn, name=name, description="v2", provider="gemini",
                concrete_model="gemini-3.5-flash", api_key_ref="env://X",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
            )
            row = await queries.get_llm_preset(conn, name)
            assert row is not None
            assert row.description == "v2"
            assert row.concrete_model == "gemini-3.5-flash"
            assert row.managed is True
        finally:
            await conn.execute("DELETE FROM llm_presets WHERE name = $1", name)
            await conn.close()

    asyncio.run(_drive())


def test_resync_preserves_operator_min_user_level(test_db_url: str) -> None:
    """`min_user_level` is operator-owned: a re-sync overwrites the other
    catalogue fields of a managed preset but leaves an operator-set tier gate
    intact, so the gate survives across boots."""

    async def _drive() -> None:
        conn = await _connect(test_db_url)
        name = f"gate_{uuid.uuid4().hex[:8]}"
        try:
            # Seed the managed preset open to all tiers (catalogue value).
            await queries.upsert_managed_preset(
                conn, name=name, description="v1", provider="anthropic",
                concrete_model="claude-sonnet-4-6", api_key_ref="env://X",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
            )
            # Operator tightens the gate via the admin path (row stays managed).
            await queries.update_llm_preset(
                conn, name, fields={"min_user_level": "tier1"}
            )

            # Re-sync: catalogue still says "*" and bumps the model. The model
            # is overwritten; the operator's tier gate is preserved.
            await queries.upsert_managed_preset(
                conn, name=name, description="v2", provider="anthropic",
                concrete_model="claude-opus-4-8", api_key_ref="env://X",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
            )
            row = await queries.get_llm_preset(conn, name)
            assert row is not None
            assert row.managed is True
            assert row.concrete_model == "claude-opus-4-8"  # catalogue-owned
            assert row.description == "v2"                   # catalogue-owned
            assert row.min_user_level == "tier1"             # operator-owned
        finally:
            await conn.execute("DELETE FROM llm_presets WHERE name = $1", name)
            await conn.close()

    asyncio.run(_drive())


def test_upsert_does_not_clobber_admin_created_preset(test_db_url: str) -> None:
    """If an admin-created preset (managed = FALSE) shares a name with a
    catalogue entry, the upsert is a no-op for it — the admin row wins and
    keeps its created_by / managed = FALSE."""

    async def _drive() -> None:
        conn = await _connect(test_db_url)
        name = f"adm_{uuid.uuid4().hex[:8]}"
        user_id = f"usr_{uuid.uuid4().hex[:8]}"
        try:
            # created_by is an FK to users; create a throwaway admin user.
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ($1, 'admin', 'password') ON CONFLICT DO NOTHING",
                user_id,
            )
            await queries.insert_llm_preset(
                conn, name=name, description="admin's own", provider="gemini",
                concrete_model="gemini-2.5-flash", api_key_ref="env://A",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
                created_by=user_id,
            )
            # Catalogue tries to claim the same name.
            await queries.upsert_managed_preset(
                conn, name=name, description="catalogue version",
                provider="anthropic", concrete_model="claude-x",
                api_key_ref="env://C", min_user_level="*",
                default_temperature=None, default_max_tokens=None,
                default_provider_options=None,
            )
            row = await queries.get_llm_preset(conn, name)
            assert row is not None
            # Untouched: still the admin row.
            assert row.managed is False
            assert row.created_by == user_id
            assert row.description == "admin's own"
            assert row.provider == "gemini"
        finally:
            await conn.execute("DELETE FROM llm_presets WHERE name = $1", name)
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            await conn.close()

    asyncio.run(_drive())


def test_prune_removes_only_stale_managed_presets(test_db_url: str) -> None:
    """`delete_stale_managed_presets` drops managed rows absent from the
    catalogue, keeps managed rows still in it, and never touches an
    admin-created preset even when it's absent from `keep`."""

    async def _drive() -> None:
        conn = await _connect(test_db_url)
        keep_name = f"keep_{uuid.uuid4().hex[:8]}"
        stale_name = f"stale_{uuid.uuid4().hex[:8]}"
        admin_name = f"admin_{uuid.uuid4().hex[:8]}"
        user_id = f"usr_{uuid.uuid4().hex[:8]}"
        try:
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ($1, 'admin', 'password') ON CONFLICT DO NOTHING",
                user_id,
            )
            for nm in (keep_name, stale_name):
                await queries.upsert_managed_preset(
                    conn, name=nm, description=None, provider="gemini",
                    concrete_model="gemini-2.5-flash", api_key_ref="env://X",
                    min_user_level="*", default_temperature=None,
                    default_max_tokens=None, default_provider_options=None,
                )
            await queries.insert_llm_preset(
                conn, name=admin_name, description=None, provider="gemini",
                concrete_model="gemini-2.5-flash", api_key_ref="env://A",
                min_user_level="*", default_temperature=None,
                default_max_tokens=None, default_provider_options=None,
                created_by=user_id,
            )

            # Catalogue now contains only keep_name (admin_name is absent
            # too, but it's not managed → must survive).
            deleted = await queries.delete_stale_managed_presets(
                conn, keep=[keep_name]
            )
            assert deleted == 1

            assert await queries.get_llm_preset(conn, keep_name) is not None
            assert await queries.get_llm_preset(conn, stale_name) is None
            assert await queries.get_llm_preset(conn, admin_name) is not None
        finally:
            await conn.execute(
                "DELETE FROM llm_presets WHERE name = ANY($1::text[])",
                [keep_name, stale_name, admin_name],
            )
            await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            await conn.close()

    asyncio.run(_drive())
