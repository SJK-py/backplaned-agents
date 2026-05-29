"""bp_agents.channel.render — shared progress / delegation rendering.

The semantic formatting of a `LoopProgress` frame ([channel.md] §5) and
the delegate tag, in ONE place so every channel frontend presents them
identically. The Telegram gateway sends `VERBOSE_PREFIX + agent_tag +
render_progress_line` as a plain message; the webapp wraps the same line
in a styled activity row. The text is the single source of truth; the
presentation (Telegram message vs HTML row) is the frontend's.
"""

from __future__ import annotations

from bp_agents.channel.core import ORCHESTRATOR_AGENT_ID, pretty_agent
from bp_agents.common.progress import PROGRESS_PRODUCER_KEY

# Leads every verbose/progress line so it's visually distinct from the
# final answer (which carries no marker).
VERBOSE_PREFIX = "💭 "

# Agents that are NOT a delegation target — their output needs no tag (the
# orchestrator is the assistant the user normally talks to; `router` is the
# platform). Any other producer means the session is delegated to it.
UNTAGGED_AGENTS = frozenset({ORCHESTRATOR_AGENT_ID, "router"})

_KIND_LABEL = {"tool_call": "[Tool]", "tool_result": "[Result]"}

# Delegation transition tools read better as plain phrases than as a raw
# `[Tool] hand_off` line (they're terminal tools, not ordinary dispatches).
_TRANSITION_PHRASE = {
    "hand_off": "Delegating to a specialist",
    "end_delegation": "Handing back to the assistant",
}


def agent_tag(agent_id: str | None) -> str:
    """`"[Research Agent] "` for a delegate, `""` otherwise. Prettifies the
    agent_id (underscores → spaces, title case) so the user sees which
    specialist currently holds the session."""
    if not agent_id or agent_id in UNTAGGED_AGENTS:
        return ""
    return f"[{pretty_agent(agent_id)} Agent] "


def progress_producer(pf: object) -> str | None:
    """The agent that actually produced a progress frame. A subagent's frame
    relayed up by a parent carries the original producer in
    `metadata[PROGRESS_PRODUCER_KEY]` (the frame's own `agent_id` is the
    relay); fall back to `agent_id` for direct frames. Use this for the tag."""
    meta = getattr(pf, "metadata", None) or {}
    return meta.get(PROGRESS_PRODUCER_KEY) or getattr(pf, "agent_id", None)


def render_progress_line(lp: dict) -> str:
    """Format one `LoopProgress` payload into a friendly verbose-mode line.

    - `thinking` heartbeat (no detail) → `Thinking…`; with the model's
      reasoning → `(…<reasoning>)`.
    - `tool_call` / `tool_result` → `[Tool]/[Result] <tool> (<detail>)`, the
      `call_` peer-tool prefix stripped for readability.
    - the delegation transition tools (`hand_off` / `end_delegation`) →
      `Delegating to a specialist…` / `Handing back to the assistant…`.
    - anything else falls back to its detail or kind.
    """
    kind = lp.get("kind", "")
    detail = lp.get("detail")
    if kind == "thinking":
        if not detail:
            return "Thinking…"
        lead = "" if detail.startswith("…") else "…"
        return f"({lead}{detail})"
    phrase = _TRANSITION_PHRASE.get(lp.get("tool") or "") if kind == "tool_call" else None
    if phrase:
        return f"{phrase}… ({detail})" if detail else f"{phrase}…"
    label = _KIND_LABEL.get(kind)
    if label:
        name = (lp.get("tool") or "").removeprefix("call_") or "tool"
        head = f"{label} {name}"
        return f"{head} ({detail})" if detail else head
    return detail or kind or "…"
