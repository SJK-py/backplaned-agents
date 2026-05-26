"""Regression: `@agent.handler(tool=False)` is actually enforced in
`build_tools` — including the all-control-plane agent.

Bug (audit of #235): `_tool_specs` conflated "no per-mode schema
map" with "every mode filtered out by non_tool_modes". An agent
whose modes are ALL `tool=False` fell into the permissive fallback
and was advertised to LLMs as `call_<agent_id>`; for a single
control-only mode the router would even admit `mode=None` and run
the hidden handler — fully defeating `tool=False`.
"""

from __future__ import annotations

import pytest


def _cat(agent_id, accepts_schema, non_tool_modes):  # type: ignore[no-untyped-def]
    return {agent_id: {
        "description": "d",
        "accepts_schema": accepts_schema,
        "non_tool_modes": non_tool_modes,
    }}


def _names(tools, provider):  # type: ignore[no-untyped-def]
    if provider == "anthropic":
        return {t["name"] for t in tools}
    if provider == "openai":
        return {t["function"]["name"] for t in tools}
    return {
        d["name"]
        for blk in tools
        for d in blk.get("function_declarations", [])
    }


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_all_control_plane_agent_emits_zero_tools(provider: str) -> None:
    """Every mode is tool=False → the agent has NO tool-visible
    surface → build_tools emits NOTHING for it (no permissive
    `call_<agent>` leak)."""
    from bp_sdk.tools import build_tools

    cat = _cat(
        "ctl",
        {"clear_history": {"type": "object", "properties": {}},
         "set_persona": {"type": "object", "properties": {}}},
        ["clear_history", "set_persona"],
    )
    tools = build_tools(cat, provider=provider)
    assert _names(tools, provider) == set()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_single_control_only_mode_emits_zero_tools(provider: str) -> None:
    """The dangerous edge: ONE mode, tool=False. Pre-fix this leaked
    `call_<agent>` and `mode=None` would admit + run it."""
    from bp_sdk.tools import build_tools

    cat = _cat("solo", {"cmd": {"type": "object", "properties": {}}},
               ["cmd"])
    assert _names(build_tools(cat, provider=provider), provider) == set()


def test_hidden_agent_not_resolvable_via_tool_name() -> None:
    """A fully-hidden agent can't round-trip through
    spawn_from_tool_call's catalog resolution either."""
    from bp_sdk.tools import resolve_tool_name

    cat = _cat("ctl", {"cmd": {"type": "object", "properties": {}}},
               ["cmd"])
    assert resolve_tool_name(cat, "call_ctl") is None
    assert resolve_tool_name(cat, "call_ctl_cmd") is None


def test_mixed_agent_still_excludes_only_the_control_modes() -> None:
    from bp_sdk.tools import build_tools

    cat = _cat(
        "orch",
        {"UserMessage": {"type": "object", "properties": {}},
         "ClearHistory": {"type": "object", "properties": {}}},
        ["ClearHistory"],
    )
    # One visible mode left → back-compat bare name, no clear-history.
    assert _names(build_tools(cat, provider="anthropic"), "anthropic") == {
        "call_orch"
    }


def test_legacy_no_schema_map_still_gets_permissive_tool() -> None:
    """The genuine fallback must survive the fix: an agent with no
    per-mode map (None / {} / non-dict) stays callable as a single
    permissive `call_<agent>`."""
    from bp_sdk.tools import build_tools

    for accepts in (None, {}, "legacy"):
        cat = _cat("svc", accepts, [])
        assert _names(build_tools(cat, provider="anthropic"),
                      "anthropic") == {"call_svc"}
