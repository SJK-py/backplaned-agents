"""Suite ACL rule set — validity against the router's own validators.

Catches a typo'd pattern / level / effect before it ever hits a live
router, by round-tripping every rule through the router's
CreateRuleRequest model + pattern/level validators.
"""

from __future__ import annotations

from bp_agents.acl import acl_replace_payload, suite_acl_rules
from bp_router.acl import is_valid_pattern, is_valid_rule_user_level
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
    assert ("channel/*", "l3/database.*") in pairs    # webapp Knowledge base page
