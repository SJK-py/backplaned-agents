"""bp_agents.acl — the suite's firewall ACL rule set ([agent-suite/acl.md] §3).

Deny-by-default; this is the allow-list (order-independent — no deny
rules). Apply it to a running router with `python -m bp_agents.load_acl`
(admin `PUT /v1/admin/acl/rules`). Each entry is a router
`CreateRuleRequest` payload.
"""

from __future__ import annotations

from typing import Any

# (effect, user_level, caller_pattern, callee_pattern, name)
_RULES: list[tuple[str, str, str, str, str]] = [
    # Orchestration spine
    ("allow", "*", "l0/*", "l1/*", "orchestrator→l1 (subagent + hand-off)"),
    ("allow", "*", "l1/agent.orchestration", "l1/*", "deep_reasoning→l1"),
    ("allow", "*", "l1/*", "l0/agent.orchestration", "l1→orchestrator (execute_step / end_delegation)"),
    # Channel ↔ agents
    ("allow", "*", "channel/*", "l0/*", "user message → orchestrator"),
    ("allow", "*", "channel/*", "l1/*", "user message → delegate"),
    ("allow", "*", "l0/*", "channel/*", "orchestrator → channel push"),
    ("allow", "*", "l1/*", "channel/*", "delegate → channel push"),
    ("allow", "*", "channel/*", "channel/*", "channel self-dispatch (/cron mode)"),
    # Memory
    ("allow", "*", "*/assistant.*", "l3/memory.retrieval", "assistant recall"),
    ("allow", "*", "channel/*", "l3/memory.add", "channel post-turn add"),
    # Summarization
    ("allow", "*", "channel/*", "l3/summarize.history", "channel summarizer"),
    # User config
    ("allow", "*", "l0/*", "l2/user.config", "orchestrator config changes"),
    ("allow", "*", "channel/*", "l2/user.config", "webapp config UI (v2)"),
    # Infra + converters
    ("allow", "*", "*/computer.*", "infra/computer.*", "computer_use → sandbox"),
    ("allow", "*", "*/database.*", "l3/database.*", "research → knowledge_base"),
    ("allow", "*", "*/document.*", "*/document.*", "→ md_converter (file→md)"),
    ("allow", "*", "*/web.fetch", "*/web.convert", "research webpage → md_converter"),
]


def suite_acl_rules() -> list[dict[str, Any]]:
    """The suite rule set as router `CreateRuleRequest` payloads."""
    return [
        {
            "ord": i,
            "name": name,
            "effect": effect,
            "user_level": level,
            "caller_pattern": caller,
            "callee_pattern": callee,
        }
        for i, (effect, level, caller, callee, name) in enumerate(_RULES)
    ]


def acl_replace_payload() -> dict[str, Any]:
    """Body for `PUT /v1/admin/acl/rules` (bulk replace)."""
    return {"rules": suite_acl_rules()}
