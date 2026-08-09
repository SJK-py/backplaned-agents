"""Router-resolved preset slots — `docs/design/router-resolved-preset-slots.md`.

The design joins two halves that previously never met: the router's tier
gate (a per-user ceiling) and the user's stored model choice (a per-user
default). The tests are organised around the three defects that split
caused, because those are what a regression would restore:

  * the menu could offer a model the user was not entitled to (§2.1);
  * refusal landed mid-conversation and un-retriably (§2.2);
  * a demotion turned a stored choice into a landmine (§2.4).

Plus the property that keeps the platform honest: the router never
interprets a slot's NAME.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import asyncpg
import pytest

from bp_protocol.frames import LlmRequestFrame, LlmResultFrame, parse_frame
from bp_router.llm.presets import (
    Preset,
    PresetNotAllowedError,
    PresetSlotUnknownError,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_slot_and_preset_are_mutually_exclusive() -> None:
    """They name the model two different ways with DIFFERENT refusal
    semantics — a slot degrades, a named preset does not. Silently
    preferring one would hide a caller bug."""
    kw = {"agent_id": "a", "trace_id": "t", "span_id": "s"}
    assert LlmRequestFrame(preset_slot="balanced", **kw).preset_slot == "balanced"
    assert LlmRequestFrame(preset="claude", **kw).preset == "claude"
    with pytest.raises(Exception):
        LlmRequestFrame(preset_slot="balanced", preset="claude", **kw)


def test_result_reports_what_actually_ran() -> None:
    """With a slot the agent named a preference, not a model — without this
    it cannot know which model answered."""
    kw = {"agent_id": "router", "trace_id": "t", "span_id": "s"}
    r = LlmResultFrame(ref_correlation_id="c", **kw)
    assert r.resolved_preset is None and r.preset_downgraded is False
    round_tripped = parse_frame(
        LlmResultFrame(
            ref_correlation_id="c",
            resolved_preset="claude-sonnet",
            preset_downgraded=True,
            **kw,
        ).model_dump(mode="json")
    )
    assert round_tripped.resolved_preset == "claude-sonnet"
    assert round_tripped.preset_downgraded is True


def test_slot_field_is_additive_for_old_callers() -> None:
    """An agent that never sends `preset_slot` must behave exactly as
    before."""
    kw = {"agent_id": "a", "trace_id": "t", "span_id": "s"}
    assert LlmRequestFrame(preset="x", **kw).preset_slot is None
    assert LlmRequestFrame(**kw).preset_slot is None


# ---------------------------------------------------------------------------
# Resolution — the heart of the design
# ---------------------------------------------------------------------------


def _service(defaults: dict[str, str], presets: dict[str, str]) -> Any:
    """An LlmService with a stub settings object and a hand-built preset map.

    `presets` maps name → min_user_level."""
    from bp_router.llm.service import LlmService

    class _Settings:
        llm_default_presets = defaults
        llm_preset_catalog_path = None
        llm_preset_overlay_path = None

    svc = LlmService.__new__(LlmService)
    svc.settings = _Settings()
    svc._presets = {
        name: Preset(name=name, provider="p", concrete_model="m", min_user_level=lvl)
        for name, lvl in presets.items()
    }
    return svc


def test_preference_is_used_when_the_gate_allows_it() -> None:
    svc = _service({"balanced": "cheap"}, {"cheap": "*", "fancy": "tier1"})
    name, downgraded = svc.resolve_slot(
        "balanced", user_level="tier1", slots={"balanced": "fancy"}
    )
    assert (name, downgraded) == ("fancy", False)


def test_no_preference_falls_back_to_the_operator_default() -> None:
    svc = _service({"balanced": "cheap"}, {"cheap": "*", "fancy": "tier1"})
    assert svc.resolve_slot("balanced", user_level="tier3", slots={}) == ("cheap", False)


def test_demotion_degrades_instead_of_wedging_every_turn() -> None:
    """§2.4 — the defect this design exists to remove. A user demoted after
    choosing keeps working on the operator default, flagged, rather than
    failing on every turn with no path that re-resolves."""
    svc = _service({"pro": "cheap"}, {"cheap": "*", "fancy": "tier1"})
    name, downgraded = svc.resolve_slot(
        "pro", user_level="tier3", slots={"pro": "fancy"}
    )
    assert name == "cheap"
    assert downgraded is True, "a downgrade must be reported, not silent"


def test_a_preference_naming_a_deleted_preset_also_degrades() -> None:
    """An operator removing a preset must not wedge everyone who chose it."""
    svc = _service({"balanced": "cheap"}, {"cheap": "*"})
    assert svc.resolve_slot(
        "balanced", user_level="tier1", slots={"balanced": "gone"}
    ) == ("cheap", True)


def test_unreachable_slot_default_fails_closed() -> None:
    """An operator configured a default this user cannot reach — a config
    error worth surfacing, not something to paper over."""
    svc = _service({"pro": "fancy"}, {"fancy": "tier1"})
    with pytest.raises(PresetNotAllowedError):
        svc.resolve_slot("pro", user_level="tier3", slots={})


def test_unknown_slot_is_refused() -> None:
    svc = _service({"balanced": "cheap"}, {"cheap": "*"})
    with pytest.raises(PresetSlotUnknownError):
        svc.resolve_slot("nonexistent", user_level="tier1", slots={})


def test_router_never_interprets_a_slot_name() -> None:
    """The compatibility property: "balanced" means whatever operator config
    says. A slot the router had never heard of works identically."""
    svc = _service({"wibble": "cheap"}, {"cheap": "*", "fancy": "*"})
    assert svc.resolve_slot("wibble", user_level=None, slots={}) == ("cheap", False)
    assert svc.resolve_slot(
        "wibble", user_level=None, slots={"wibble": "fancy"}
    ) == ("fancy", False)
    # ...and no slot name is special-cased anywhere in the resolver.
    src = inspect.getsource(type(svc).resolve_slot)
    for suite_word in ("balanced", "pro", "lite", "embedding"):
        assert f'"{suite_word}"' not in src, suite_word


def test_embedding_is_not_a_slot_by_default() -> None:
    """Changing an embedding model invalidates every stored vector, with no
    error and no migration path — so it is never user-resolvable."""
    from bp_router.settings import Settings

    s = Settings(
        db_url="postgresql://x/y",
        public_url="http://x",
        jwt_secret="k" * 32,
        serve_admin_ui=False,
    )
    assert "embedding" not in s.llm_default_presets


def test_slot_defaults_are_validated_against_loaded_presets() -> None:
    svc = _service({"balanced": "cheap", "pro": "typo"}, {"cheap": "*"})
    assert svc.validate_slot_defaults() == ["pro -> typo"]
    svc2 = _service({"balanced": "cheap"}, {"cheap": "*"})
    assert svc2.validate_slot_defaults() == []


# ---------------------------------------------------------------------------
# Storage + the trust boundary
# ---------------------------------------------------------------------------


async def _pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_preferences_round_trip_and_clear(test_db_url: str) -> None:
    async def go() -> None:
        from bp_router.db import queries

        pool = await _pool(test_db_url)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE user_id = 'u_pref'")
            await conn.execute(
                "INSERT INTO users (user_id, level, auth_kind) "
                "VALUES ('u_pref','tier1','password')"
            )
            assert await queries.get_user_llm_preferences(conn, "u_pref") == {}
            await queries.set_user_llm_preference(conn, "u_pref", "balanced", "claude")
            await queries.set_user_llm_preference(conn, "u_pref", "pro", "opus")
            assert await queries.get_user_llm_preferences(conn, "u_pref") == {
                "balanced": "claude",
                "pro": "opus",
            }
            # Re-setting one slot replaces rather than duplicates.
            await queries.set_user_llm_preference(conn, "u_pref", "balanced", "gemini")
            prefs = await queries.get_user_llm_preferences(conn, "u_pref")
            assert prefs["balanced"] == "gemini"
            # Clearing returns the slot to the operator default.
            await queries.set_user_llm_preference(conn, "u_pref", "balanced", None)
            assert "balanced" not in await queries.get_user_llm_preferences(
                conn, "u_pref"
            )
            # Erasing the user takes their preferences with them.
            await conn.execute("DELETE FROM users WHERE user_id = 'u_pref'")
            left = await conn.fetchval(
                "SELECT count(*) FROM user_llm_preferences WHERE user_id = 'u_pref'"
            )
            assert left == 0
        await pool.close()

    _run(go())


def test_preferences_have_no_agent_facing_write_path() -> None:
    """The trust boundary (§4): the router ACTS on this value — it picks a
    model, at a cost, under a tier gate — so it must not live where any
    agent in the session can overwrite it. Writes come only from the
    session-JWT endpoints."""
    from bp_router import dispatch, session_store

    assert "set_user_llm_preference" not in inspect.getsource(dispatch)
    assert "user_llm_preferences" not in inspect.getsource(session_store)

    from bp_router.api import llm as llm_api

    src = inspect.getsource(llm_api)
    assert "require_authenticated" in src  # session JWT, not an agent token
    assert "set_user_llm_preference" in src


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _app() -> Any:
    import os

    os.environ.setdefault("ROUTER_DB_URL", "postgresql://unused/unused")
    os.environ.setdefault("ROUTER_PUBLIC_URL", "http://localhost:8000")
    os.environ.setdefault("ROUTER_JWT_SECRET", "k" * 32)
    os.environ.setdefault("ROUTER_SERVE_ADMIN_UI", "false")
    pytest.importorskip("fastapi")
    from bp_router.app import create_app

    return create_app()


def test_user_facing_preset_endpoints_exist() -> None:
    paths = _app().openapi()["paths"]
    assert set(paths["/v1/llm/presets"]) == {"get"}
    assert set(paths["/v1/llm/preferences"]) == {"get", "put"}


def test_preset_listing_never_exposes_credentials() -> None:
    """A user-facing projection: name, description, slot defaults. The admin
    view keeps provider detail and key refs."""
    from bp_router.api.llm import PresetChoice

    fields = set(PresetChoice.model_fields)
    assert fields == {"name", "description", "default_for"}
    for leaked in ("api_key", "api_key_ref", "base_url", "provider", "concrete_model"):
        assert leaked not in fields


def test_preference_write_gates_and_invalidates_the_cache() -> None:
    """Two properties in one place: the refusal moved to selection time, and
    a write must invalidate the cache the resolution path reads — otherwise
    the old model keeps running for up to the TTL."""
    from bp_router.api import llm as llm_api

    src = inspect.getsource(llm_api.set_preference)
    assert "user_level_satisfies" in src
    assert "preset_not_allowed" in src
    assert "required_level" in src  # tell the user what it needs
    assert "invalidate_user_level" in src


# ---------------------------------------------------------------------------
# Hot path
# ---------------------------------------------------------------------------


def test_slot_resolution_rides_the_existing_user_level_cache() -> None:
    """Efficiency claim from §7: one fetch serves both the gate and the
    preference, so a slot costs no extra round trip."""
    from bp_router.llm.service import _UserLevelCacheEntry

    assert "slots" in _UserLevelCacheEntry.__dataclass_fields__

    from bp_router.llm import service as service_mod

    src = inspect.getsource(service_mod.LlmService.resolve_user_level)
    assert "get_user_llm_preferences" in src, "preferences must load WITH the level"


def test_handler_derives_identity_before_reading_a_preference() -> None:
    """A preference must be read for the TRUSTED user. Reading it for an
    asserted one would let an agent borrow another tenant's entitlement."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    slot_at = src.index("frame.preset_slot is not None")
    derive_at = src.index("_derive_task_scope", slot_at)
    resolve_at = src.index("resolve_slot(", slot_at)
    assert derive_at < resolve_at, "identity derived before the preference is read"
    assert "frame.user_id" not in src[slot_at:resolve_at]


def test_handler_does_not_derive_the_task_scope_twice() -> None:
    """The gate reuses the level slot resolution already resolved."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    assert "slot_level_resolved" in src
    assert "if first_preset_gated and not slot_level_resolved:" in src


def test_sdk_exposes_slot_and_surfaces_the_downgrade() -> None:
    from bp_sdk.llm import LlmResponse, LlmServiceClient

    params = inspect.signature(LlmServiceClient.generate).parameters
    assert "slot" in params
    assert params["slot"].default is None
    assert "resolved_preset" in LlmResponse.__dataclass_fields__
    assert "preset_downgraded" in LlmResponse.__dataclass_fields__
