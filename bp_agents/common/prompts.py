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


# Reusable file-delivery instruction for delegated turns. File tools are an
# agent-by-agent capability (NOT part of the shared delegation harness), so an
# agent that should hand the user files composes this into its OWN
# delegation_system. computer_use writes its own variant (it juggles a separate
# sandbox workspace alongside the shared stash).
FILE_DELIVERY_NOTE = """\
Files live in a shared stash. To hand the user an actual file, call \
`send_file` with its stash name and write your reply in the same turn — it is \
delivered as an attachment with that reply (a file is never sent on its own). \
A stash file name you're given can be `read_file`'d or passed to another \
agent — the stash is shared, so the name is enough.\
"""

# Companion to FILE_DELIVERY_NOTE for the SUBAGENT face. A subagent's reply
# goes to the CALLING agent, not the user, and it has no `send_file` — so it
# returns a file by NAME for the caller to deliver. Deliberately tool-agnostic:
# a file can reach the stash any number of ways (a download, a tool result),
# so we instruct the shared stash + by-reference return, not a specific tool.
SUBAGENT_FILE_NOTE = """\
To return a file, make sure it's in the shared file stash and include its \
reference in your reply — its name `<name>`, or `persist/<name>` for the \
persistent stash. The stash is shared, so the caller can deliver or use the \
file by reference. You can't send files to the user yourself.\
"""

# Incoming-file mechanic for user-facing agents (the orchestrator, and l1
# agents on subsequent delegated turns where the user messages them directly).
# Tool-agnostic on the inbound side too — names a `read_file` only as the way
# to inspect contents when needed.
INCOMING_FILE_NOTE = """\
When the user sends a file it's saved to the shared stash and you'll see a \
note like `user-attached file saved as <name>` — the file genuinely arrived. \
Call `read_file` with that name when you need its contents; you don't have to \
read every file, since its reference alone is enough to pass it on.\
"""


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
