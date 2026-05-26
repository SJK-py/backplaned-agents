"""Capstone regression for R-MEDIUM #1 — order-dependent
multi-handler dispatch.

The original bug: `_resolve_handler_for` returned the FIRST handler
whose Pydantic model `model_validate`'d the payload, so two
structurally-overlapping models routed by *registration order*; and
the auto-`oneOf` `accepts_schema` made the router *reject* a valid
payload for handler B because it also matched A's (lenient,
no-additionalProperties) schema.

The fix replaces structural first-match with explicit mode keys.
This file pins the headline guarantees end to end so neither
failure mode can return.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext


# The exact overlapping pair from the #1 analysis: a payload like
# {"text": "..."} structurally validates as BOTH (model_validate is
# lenient; TranslateText.target_lang has a default).
class TranslateText(BaseModel):
    text: str
    target_lang: str = "en"


class DetectLanguage(BaseModel):
    text: str


def _two_handler_agent(order: str) -> tuple[Agent, dict]:
    a = Agent(info=AgentInfo(agent_id="ling", description="d"))
    seen: dict[str, str] = {}

    if order == "translate_first":
        @a.handler
        async def translate(ctx: TaskContext, p: TranslateText) -> AgentOutput:
            seen["who"] = "translate"
            return AgentOutput()

        @a.handler
        async def detect(ctx: TaskContext, p: DetectLanguage) -> AgentOutput:
            seen["who"] = "detect"
            return AgentOutput()
    else:
        @a.handler
        async def detect(ctx: TaskContext, p: DetectLanguage) -> AgentOutput:
            seen["who"] = "detect"
            return AgentOutput()

        @a.handler
        async def translate(ctx: TaskContext, p: TranslateText) -> AgentOutput:
            seen["who"] = "translate"
            return AgentOutput()

    return a, seen


@pytest.mark.parametrize("order", ["translate_first", "detect_first"])
def test_resolution_is_registration_order_independent(order: str) -> None:
    """The bug: `{"text": "hola"}` validates as BOTH models, so the
    old resolver returned whichever was registered first. Now the
    caller names the mode and gets exactly that handler — same
    result for either registration order."""
    pytest.importorskip("fastapi")
    a, _ = _two_handler_agent(order)

    assert a.resolve_handler(mode="DetectLanguage").fn.__name__ == "detect"
    assert a.resolve_handler(mode="TranslateText").fn.__name__ == "translate"
    # The ambiguous payload no longer "wins" a handler by order:
    # mode=None with >1 handler is a clean no_handler, not a guess.
    assert a.resolve_handler(mode=None) is None


def test_accepts_schema_is_a_per_mode_map_not_oneof() -> None:
    """The over-reject failure mode is structurally gone: there is
    no `oneOf` for the router's jsonschema to reject a B-payload that
    also matches A. Each mode has its own tightened object schema."""
    pytest.importorskip("fastapi")
    a, _ = _two_handler_agent("translate_first")

    schema = a.info.accepts_schema
    assert set(schema) == {"TranslateText", "DetectLanguage"}
    assert "oneOf" not in schema
    # Tightened so a typo'd key is a clean schema_mismatch, not a
    # silently-accepted ambiguous payload.
    assert schema["DetectLanguage"]["additionalProperties"] is False


def test_router_admit_rejects_ambiguous_and_unknown_mode() -> None:
    """Fail-fast at admit (no task row) — the source contract that
    replaces the silent mis-route / oneOf over-reject."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router import tasks

    src = inspect.getsource(tasks)
    assert "unknown input_mode" in src
    assert "NewTaskFrame.input_mode is" in src
    assert "accepts_control_schema" not in src


def test_per_mode_tool_round_trip_end_to_end() -> None:
    """build_tools → per-mode tool names → resolve_tool_name back to
    the exact (agent, mode). The LLM picks an operation explicitly;
    no structural guessing anywhere in the loop."""
    pytest.importorskip("fastapi")
    from bp_sdk.tools import build_tools, resolve_tool_name

    a, _ = _two_handler_agent("translate_first")
    catalog = {
        "ling": {
            "description": "d",
            "accepts_schema": a.info.accepts_schema,
            "non_tool_modes": a.info.non_tool_modes,
        }
    }
    tools = build_tools(catalog, provider="anthropic")
    names = sorted(t["name"] for t in tools)
    assert names == ["call_ling_DetectLanguage", "call_ling_TranslateText"]

    assert resolve_tool_name(catalog, "call_ling_DetectLanguage") == (
        "ling", "DetectLanguage",
    )
    assert resolve_tool_name(catalog, "call_ling_TranslateText") == (
        "ling", "TranslateText",
    )


def test_spawn_from_tool_call_threads_resolved_mode() -> None:
    """The LLM-dispatch loop carries the mode through: a per-mode
    tool name resolves to `spawn(..., mode=<that mode>)` — so the
    overlapping-payload ambiguity can't re-enter via the tool loop."""
    pytest.importorskip("fastapi")
    from unittest.mock import MagicMock

    from bp_sdk.peers import PeerClient

    a, _ = _two_handler_agent("translate_first")
    catalog = {
        "ling": {
            "description": "d",
            "accepts_schema": a.info.accepts_schema,
            "non_tool_modes": a.info.non_tool_modes,
        }
    }

    pc = PeerClient.__new__(PeerClient)
    pc.visible = lambda **_kw: catalog  # type: ignore[attr-defined]

    captured: dict[str, object] = {}

    async def fake_spawn(dest, payload, **kw):  # type: ignore[no-untyped-def]
        captured["dest"] = dest
        captured["mode"] = kw.get("mode")
        return "tsk_1"

    pc.spawn = fake_spawn  # type: ignore[attr-defined]

    tc = MagicMock()
    tc.name = "call_ling_DetectLanguage"
    tc.args = {"text": "hola"}

    asyncio.run(pc.spawn_from_tool_call(tc))
    assert captured == {"dest": "ling", "mode": "DetectLanguage"}
