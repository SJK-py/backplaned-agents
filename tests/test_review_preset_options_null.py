"""`default_provider_options` must never become SQL NULL.

Second-pass (data-model): the `llm_presets.default_provider_options` column is
jsonb and `LlmPresetRow.default_provider_options` is a non-Optional dict. A
PATCH carrying explicit JSON `null` wrote SQL NULL via `update_llm_preset`;
the `UPDATE … RETURNING` row then failed `LlmPresetRow` validation (None is
not a dict) → 500, AND the row stayed NULL so every subsequent
list/get/load_presets for it also failed — poisoning the preset read until
fixed by hand. Fix: COALESCE on the write path + None→{} coercion on read.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import asyncpg

from bp_router.db import queries
from bp_router.db.models import LlmPresetRow


def test_model_coerces_null_options_to_empty_dict() -> None:
    """Read path: a row whose default_provider_options is None validates to
    {} instead of raising."""
    row = LlmPresetRow.model_validate({
        "name": "p",
        "description": None,
        "provider": "gemini",
        "concrete_model": "gemini-2.5-flash",
        "api_key_ref": "env://X",
        "api_key": None,
        "base_url": None,
        "min_user_level": "*",
        "default_temperature": None,
        "default_max_tokens": None,
        "default_provider_options": None,  # the poison value
        "fallback_preset": None,
        "max_retries": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "created_by": None,
    })
    assert row.default_provider_options == {}


def test_update_with_null_options_does_not_poison_preset(test_db_url: str) -> None:
    """Write path round-trip: PATCH default_provider_options→null writes {}
    (not NULL), the RETURNING row validates, and a later get still reads."""

    async def _drive() -> None:
        conn = await asyncpg.connect(test_db_url)
        try:
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads,
                schema="pg_catalog",
            )
            name = f"test_preset_{uuid.uuid4().hex[:8]}"
            await queries.insert_llm_preset(
                conn,
                name=name,
                description=None,
                provider="gemini",
                concrete_model="gemini-2.5-flash",
                api_key_ref="env://X",
                min_user_level="*",
                default_temperature=None,
                default_max_tokens=None,
                default_provider_options={"a": 1},
                created_by=None,
            )
            try:
                # The poison PATCH: explicit null for the jsonb options.
                updated = await queries.update_llm_preset(
                    conn, name, fields={"default_provider_options": None}
                )
                assert updated is not None
                assert updated.default_provider_options == {}, (
                    "COALESCE should have written {} not NULL"
                )
                # And a fresh read of the same row is not poisoned.
                fetched = await queries.get_llm_preset(conn, name)
                assert fetched is not None
                assert fetched.default_provider_options == {}
                # The column is genuinely an object in the DB (not NULL).
                raw = await conn.fetchval(
                    "SELECT default_provider_options IS NULL FROM llm_presets "
                    "WHERE name = $1", name,
                )
                assert raw is False
            finally:
                await queries.delete_llm_preset(conn, name)
        finally:
            await conn.close()

    asyncio.run(_drive())
