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
