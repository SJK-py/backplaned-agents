"""Suite ACL rule set — validity against the router's own validators.

Catches a typo'd pattern / level / effect before it ever hits a live
router, by round-tripping every rule through the router's
CreateRuleRequest model + pattern/level validators.
"""

from __future__ import annotations

from bp_agents.acl import acl_replace_payload, suite_acl_rules
from bp_router.acl import (
    Rule,
    is_allowed_for,
    is_valid_pattern,
    is_valid_rule_user_level,
)
from bp_router.api.admin import CreateRuleRequest


def test_every_rule_is_valid() -> None:
    rules = suite_acl_rules()
    assert rules, "rule set is empty"
    for r in rules:
        # The router's request model enforces effect / level / patterns.
        CreateRuleRequest(**r)
        assert is_valid_pattern(r["caller_pattern"])
        assert is_valid_pattern(r["callee_pattern"])
        assert is_valid_rule_user_level(r["user_level"])
        assert r["effect"] in ("allow", "deny")


def test_ords_are_unique_and_dense() -> None:
    ords = [r["ord"] for r in suite_acl_rules()]
    assert ords == list(range(len(ords)))


def test_replace_payload_shape() -> None:
    payload = acl_replace_payload()
    assert set(payload) == {"rules"}
    assert payload["rules"] == suite_acl_rules()


def test_core_flows_present() -> None:
    pairs = {(r["caller_pattern"], r["callee_pattern"]) for r in suite_acl_rules()}
    # A few load-bearing edges from acl.md §3.
    assert ("channel/*", "l0/*") in pairs            # user msg → orchestrator
    assert ("*/assistant.*", "l3/memory.retrieval") in pairs
    assert ("channel/*", "l3/memory.add") in pairs
    assert ("channel/*", "l3/summarize.history") in pairs
    assert ("*/database.*", "l3/database.*") in pairs
    # No broad channel→database grant: the webapp reaches the KB via its own
    # database.* capability, so the chatbot (channel, no database cap) can't.
    assert ("channel/*", "l3/database.*") not in pairs


# Agent capability/group sets the webapp Memory/Knowledge pages depend on.
_WEBAPP = {
    "caller_id": "webapp",
    "caller_groups": ["channel", "inbound"],
    "caller_capabilities": [
        "channel.webapp", "user.auth", "file.full", "session.history",
        "session.management", "database.retrieval", "database.manage",
        "memory.retrieval", "memory.add",
    ],
}
_CHATBOT_CAPS = [
    "channel.telegram", "user.auth", "user.registration", "user.cron",
    "file.full", "session.history", "session.management",
]
_KB = {
    "callee_id": "knowledge_base", "callee_groups": ["l3"],
    "callee_capabilities": [
        "database.manage", "database.retrieval", "file.full", "document.convert",
    ],
}
_MEMORY = {
    "callee_id": "memory", "callee_groups": ["l3"],
    "callee_capabilities": ["memory.add", "memory.retrieval"],
}


def test_webapp_reaches_kb_and_memory_least_privilege() -> None:
    rules = [Rule(**r) for r in suite_acl_rules()]
    # webapp → KB via its own database.* capability (the */database.* rule).
    assert is_allowed_for(rules, **_WEBAPP, **_KB, user_level="tier0").allow
    # webapp → memory via the channel→memory.add rule it already holds.
    assert is_allowed_for(rules, **_WEBAPP, **_MEMORY, user_level="tier0").allow
    # The chatbot (channel, but no database.* capability) CANNOT reach the KB.
    assert not is_allowed_for(
        rules, caller_id="chatbot", caller_groups=["channel", "inbound"],
        caller_capabilities=_CHATBOT_CAPS, **_KB, user_level="tier0",
    ).allow


# ---------------------------------------------------------------------------
# Non-destructive merge — load_acl preserves admin-added rules across reboots
# (regression: run-suite.sh re-applies the suite ACL on every boot, and a
# destructive replace wiped custom MCP grants).
# ---------------------------------------------------------------------------


def test_merge_preserves_custom_rules() -> None:
    from bp_agents.acl import merge_preserving_custom, suite_rule_names

    # Simulate the router's current rules: the full suite set (as RuleView
    # dicts) + two admin-added MCP grants.
    existing = [
        dict(r, rule_id=f"rule_{i}", created_at="t")
        for i, r in enumerate(suite_acl_rules())
    ]
    existing += [
        {
            "rule_id": "rule_mcp1", "ord": 99, "name": "channel→mcp_minimax",
            "description": "let the bot call minimax",
            "effect": "allow", "user_level": "*",
            "caller_pattern": "channel/*", "callee_pattern": "@mcp_minimax",
        },
        {
            "rule_id": "rule_mcp2", "ord": 100, "name": "l0→mcp_minimax",
            "description": None, "effect": "allow", "user_level": "*",
            "caller_pattern": "l0/*", "callee_pattern": "@mcp_minimax",
        },
    ]

    body = merge_preserving_custom(existing)
    rules = body["rules"]
    names = [r["name"] for r in rules]

    # Both custom rules survive; the suite set is fully present.
    assert "channel→mcp_minimax" in names
    assert "l0→mcp_minimax" in names
    assert suite_rule_names() <= set(names)
    # No duplication of the suite rules (the existing copies were dropped +
    # re-emitted, not stacked).
    assert len(rules) == len(suite_acl_rules()) + 2

    # Custom rules keep HIGHER priority (lower ord) than the suite allows.
    custom_ords = [r["ord"] for r in rules if r["name"] in
                   {"channel→mcp_minimax", "l0→mcp_minimax"}]
    suite_ords = [r["ord"] for r in rules if r["name"] in suite_rule_names()]
    assert max(custom_ords) < min(suite_ords)
    # Ords are unique + dense (the router requires uniqueness).
    ords = [r["ord"] for r in rules]
    assert sorted(ords) == list(range(len(ords)))


def test_merge_on_empty_equals_suite() -> None:
    """First boot (no rules yet) — the merge is just the suite set, same as
    the old destructive replace."""
    from bp_agents.acl import merge_preserving_custom

    body = merge_preserving_custom([])
    assert [r["name"] for r in body["rules"]] == [
        r["name"] for r in suite_acl_rules()
    ]


def test_merge_refreshes_suite_rules_not_stale_copy() -> None:
    """A suite-owned rule in the DB is replaced by the current suite
    definition, not preserved as a custom rule."""
    from bp_agents.acl import merge_preserving_custom, suite_rule_names

    a_suite_name = next(iter(suite_rule_names()))
    existing = [{
        "rule_id": "rule_stale", "ord": 0, "name": a_suite_name,
        "description": "STALE", "effect": "deny", "user_level": "*",
        "caller_pattern": "*/*", "callee_pattern": "*/*",
    }]
    body = merge_preserving_custom(existing)
    # Exactly one rule with that name, and it carries the suite definition
    # (effect allow), not the stale deny.
    matches = [r for r in body["rules"] if r["name"] == a_suite_name]
    assert len(matches) == 1
    assert matches[0]["effect"] == "allow"


def test_merged_rules_validate_against_router_model() -> None:
    from bp_agents.acl import merge_preserving_custom

    existing = [{
        "rule_id": "rule_x", "ord": 5, "name": "custom",
        "description": None, "effect": "deny", "user_level": "tier2",
        "caller_pattern": "@evil", "callee_pattern": "l3/*",
    }]
    for r in merge_preserving_custom(existing)["rules"]:
        CreateRuleRequest(**r)
