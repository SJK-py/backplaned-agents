"""bp_agents.common — building blocks shared across suite agents.

The pieces every l0/l1 agent reuses:

  - `run_llm_loop` — the generic multi-turn tool-calling loop
    (LLM generate → round-trip assistant → dispatch tool calls → repeat),
    handling both peer-agent tools and an agent's own local tools.
  - `LocalTool` / `LocalToolset` — the local (in-process) tool surface,
    plus `peer_tool_specs` for the ACL-filtered peer catalog and the
    `current_time` tool every l0/l1 agent carries.
  - `LoopProgress` + `emit_loop_progress` — structured progress in
    `ProgressFrame.metadata` ([data-model.md] §3).
  - prompt composition (`compose_system_prompt`, `user_config_note`)
    and output helpers (`text_output`, `estimate_context_tokens`).
"""

from bp_agents.common.loop import run_llm_loop
from bp_agents.common.output import (
    estimate_context_tokens,
    estimate_tokens,
    text_output,
)
from bp_agents.common.progress import LoopProgress, emit_loop_progress
from bp_agents.common.prompts import compose_system_prompt, user_config_note
from bp_agents.common.tools import (
    LocalTool,
    LocalToolset,
    make_current_time_tool,
    peer_tool_specs,
)

__all__ = [
    "LocalTool",
    "LocalToolset",
    "LoopProgress",
    "compose_system_prompt",
    "emit_loop_progress",
    "estimate_context_tokens",
    "estimate_tokens",
    "make_current_time_tool",
    "peer_tool_specs",
    "run_llm_loop",
    "text_output",
    "user_config_note",
]
