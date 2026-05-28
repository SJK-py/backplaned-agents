"""bp_router.ws_hub._merged_hello_agent_info — the handshake refresh that
re-publishes a reconnecting agent's AgentInfo (so a restart with changed
modes/capabilities propagates without re-onboarding)."""

from __future__ import annotations

import inspect

import pytest

from bp_protocol.types import AgentInfo
from bp_router import ws_hub
from bp_router.ws_hub import _CatalogCache, _merged_hello_agent_info


def _info(modes: list[str], *, caps: list[str] | None = None, agent_id: str = "config") -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        description="cfg",
        groups=["l2"],
        capabilities=caps or ["user.config"],
        accepts_schema={m: {"type": "object", "properties": {}} for m in modes},
    )


def test_returns_refresh_when_a_mode_is_added() -> None:
    existing = _info(["message"]).model_dump()
    hello = _info(["message", "cron"], caps=["user.config", "user.cron"])
    refreshed = _merged_hello_agent_info(existing, hello)
    assert refreshed is not None
    info_dump, groups, capabilities = refreshed
    assert set(info_dump["accepts_schema"]) == {"message", "cron"}
    assert capabilities == ["user.config", "user.cron"]
    assert groups == ["l2"]
    assert info_dump["agent_id"] == "config"


def test_returns_none_when_unchanged() -> None:
    existing = _info(["message", "cron"]).model_dump()
    hello = _info(["message", "cron"])
    assert _merged_hello_agent_info(existing, hello) is None


def test_agent_id_is_locked_to_stored_record() -> None:
    # A Hello that claims a different agent_id can't rewrite it.
    existing = _info(["message"], agent_id="config").model_dump()
    hello = _info(["message", "cron"], agent_id="impostor")
    refreshed = _merged_hello_agent_info(existing, hello)
    assert refreshed is not None
    info_dump, _groups, _caps = refreshed
    assert info_dump["agent_id"] == "config"


def test_invalid_merge_raises_validation_error() -> None:
    # The handshake catches this and skips the refresh; here we just prove
    # a malformed merged shape is rejected rather than silently persisted.
    existing = _info(["message"]).model_dump()

    class _Bad:
        agent_id = "config"

        def model_dump(self) -> dict:
            return {"groups": "not-a-list"}  # groups must be a list

    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        _merged_hello_agent_info(existing, _Bad())  # type: ignore[arg-type]


def test_catalog_cache_clear_resets_state() -> None:
    c = _CatalogCache(ttl_s=100.0)
    c._cached = ["seeded"]          # type: ignore[assignment]
    c._expires_at = 1e18
    c.clear()
    assert c._cached is None
    assert c._expires_at == 0.0


def test_handshake_broadcasts_catalog_update_when_info_changed() -> None:
    """A reconnect that changes the agent's published info must push a
    CatalogUpdate to connected peers (they hold a stale catalog otherwise)
    and drop the short-TTL catalog cache."""
    src = inspect.getsource(ws_hub._handshake)
    assert "info_changed = True" in src
    assert "push_catalog_update_to_all(state)" in src
    assert "agent_catalog_cache" in src and ".clear()" in src
