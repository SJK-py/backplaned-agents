"""bp_agents.common.prompts — system-prompt composition.

The orchestrator's `message` prompt is `general instruction + user-config
note + history_summary` ([sessions.md] §5); l1 delegation prompts add the
agent-specific instruction + delegate seed + delegate summary. These
helpers assemble the shared pieces; per-agent instructions live with each
agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bp_agents.db.models import UserConfigRow


def user_config_note(cfg: UserConfigRow) -> str:
    """Render the per-user context block injected into system prompts:
    name, timezone, language preference, and the user's custom note.
    Empty fields are omitted; returns "" when nothing is set."""
    lines: list[str] = []
    if cfg.full_name:
        lines.append(f"User's name: {cfg.full_name}")
    if cfg.timezone:
        lines.append(f"User's timezone: {cfg.timezone}")
    if cfg.language:
        lines.append(f"Preferred language: {cfg.language}")
    if cfg.custom_note:
        lines.append(f"User note: {cfg.custom_note}")
    if not lines:
        return ""
    return "## About the user\n" + "\n".join(lines)


def compose_system_prompt(
    general: str,
    *,
    config_note: str | None = None,
    summary: str | None = None,
) -> str:
    """Assemble a system prompt from the general instruction, the
    user-config note, and the active rolling summary. Sections present
    only when non-empty; joined with blank lines."""
    sections = [general.strip()]
    if config_note:
        sections.append(config_note.strip())
    if summary:
        sections.append("## Conversation so far\n" + summary.strip())
    return "\n\n".join(s for s in sections if s)
