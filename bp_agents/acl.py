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
    # Memory
    ("allow", "*", "*/assistant.*", "l3/memory.retrieval", "assistant recall"),
    ("allow", "*", "channel/*", "l3/memory.add", "channel post-turn add + webapp Memory page"),
    # (Knowledge base: the webapp reaches it via its own `database.*`
    # capability through the `*/database.* -> l3/database.*` rule below — no
    # broad channel grant, so the chatbot can't reach the KB.)
    # Summarization
    ("allow", "*", "channel/*", "l3/summarize.history", "channel summarizer"),
    # User config + cron management (both hosted on the config agent)
    ("allow", "*", "l0/*", "l2/user.config", "orchestrator config changes"),
    ("allow", "*", "channel/*", "l2/user.config", "channel /config + /cron commands"),
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


def suite_rule_names() -> set[str]:
    """Names of the rules the suite OWNS. `merge_preserving_custom` refreshes
    only these and leaves every other (admin-added) rule alone."""
    return {name for *_rest, name in _RULES}


def _rule_payload(r: dict[str, Any]) -> dict[str, Any]:
    """Project a rule dict (suite payload or a router RuleView) onto the
    `CreateRuleRequest` shape, dropping server-assigned `rule_id`/`ord`/
    `created_at` (ord is reassigned by position on merge)."""
    return {
        "name": r.get("name"),
        "description": r.get("description"),
        "effect": r["effect"],
        "user_level": r["user_level"],
        "caller_pattern": r["caller_pattern"],
        "callee_pattern": r["callee_pattern"],
    }


def merge_preserving_custom(existing: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the `PUT /v1/admin/acl/rules` body that REFRESHES the suite's own
    rules while PRESERVING every other rule an admin added (e.g. MCP grants).

    `existing` is the current rule list (router `RuleView` dicts). Rules whose
    `name` is in `suite_rule_names()` are dropped and re-emitted from the
    canonical suite set; all others are kept verbatim.

    Custom rules are placed FIRST (lower `ord` = higher priority) so an admin's
    `deny` isn't shadowed by one of the suite's allow-only rules; the suite set
    follows in its canonical order. Ords are reassigned contiguously by
    position (they must be unique). An empty `existing` (first boot) yields just
    the suite set — same result as the old destructive replace."""
    owned = suite_rule_names()
    custom = sorted(
        (r for r in existing if r.get("name") not in owned),
        key=lambda r: r.get("ord", 0),
    )
    merged = [_rule_payload(r) for r in custom]
    merged.extend(_rule_payload(s) for s in suite_acl_rules())
    for i, r in enumerate(merged):
        r["ord"] = i
    return {"rules": merged}

