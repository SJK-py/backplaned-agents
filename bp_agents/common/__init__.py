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
  - `thread` — the three-beat turn against the router session store
    (`open_turn` → `maybe_fold` → `close_turn`) every conversational agent
    shares, including hand-over materialisation and summary folding.
"""

from bp_agents.common.chunking import chunk_markdown
from bp_agents.common.loop import run_llm_loop
from bp_agents.common.output import (
    estimate_context_tokens,
    estimate_tokens,
    text_output,
)
from bp_agents.common.progress import LoopProgress, emit_loop_progress
from bp_agents.common.prompts import (
    FILE_DELIVERY_NOTE,
    INCOMING_FILE_NOTE,
    SUBAGENT_FILE_NOTE,
    compose_system_prompt,
    user_config_note,
)
from bp_agents.common.thread import (
    CONTEXT_ROLES,
    ThreadTurn,
    append_rows,
    close_turn,
    context_tokens_of,
    maybe_fold,
    open_turn,
    redact_rows,
)
from bp_agents.common.tool_history import make_recall_tool_history_tool
from bp_agents.common.tools import (
    LocalTool,
    LocalToolset,
    make_current_time_tool,
    make_send_file_tool,
    peer_tool_specs,
)

__all__ = [
    "CONTEXT_ROLES",
    "LocalTool",
    "LocalToolset",
    "LoopProgress",
    "ThreadTurn",
    "FILE_DELIVERY_NOTE",
    "INCOMING_FILE_NOTE",
    "SUBAGENT_FILE_NOTE",
    "append_rows",
    "chunk_markdown",
    "close_turn",
    "compose_system_prompt",
    "context_tokens_of",
    "emit_loop_progress",
    "estimate_context_tokens",
    "estimate_tokens",
    "make_current_time_tool",
    "make_recall_tool_history_tool",
    "make_send_file_tool",
    "maybe_fold",
    "open_turn",
    "peer_tool_specs",
    "redact_rows",
    "run_llm_loop",
    "text_output",
    "user_config_note",
]
