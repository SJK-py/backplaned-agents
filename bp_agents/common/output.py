"""bp_agents.common.output — AgentOutput builders + token estimation.

Every mode returns an `AgentOutput` ([agent-suite/overview.md] §6). The
channel reads `metadata.context_tokens` as a soft summarization trigger
([sessions.md] §3) — `estimate_context_tokens` produces that number
cheaply (no `count_tokens` round-trip per turn).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bp_protocol.types import AgentOutput

if TYPE_CHECKING:
    from bp_sdk import Message


# Rough chars-per-token heuristic. The channel uses `context_tokens`
# only as a *soft* trigger with headroom below the provider's real
# window ([sessions.md] §3.2), so an estimate is fine and avoids a
# per-turn `ctx.llm.count_tokens` round-trip. ~4 chars/token is the
# usual English approximation across BPE tokenizers.
_CHARS_PER_TOKEN = 4


def text_output(
    content: str | None = None,
    *,
    files: list[str] | None = None,
    context_tokens: int | None = None,
    **metadata: Any,
) -> AgentOutput:
    """Build the standard `AgentOutput`. `context_tokens` (when given)
    is stamped into `metadata` for the channel's summarization check."""
    meta: dict[str, Any] = dict(metadata)
    if context_tokens is not None:
        meta["context_tokens"] = context_tokens
    return AgentOutput(content=content, files=files or [], metadata=meta)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate for a string (~4 chars/token)."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def estimate_context_tokens(messages: list[Message]) -> int:
    """Estimate the token footprint of a built context. Text content
    (str, or the `text` field of multi-part content) is counted;
    `file_ref` / `image` / `document` parts are skipped — bytes are
    resolved at the router and don't ride this estimate."""
    total = 0
    for m in messages:
        content = m.content
        if isinstance(content, str):
            total += estimate_tokens(content)
            continue
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += estimate_tokens(text)
    return total
