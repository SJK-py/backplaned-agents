"""Tests for the firewall-style ACL evaluator (`bp_router.acl`).

The review (C8) flagged this module as untested. The functions covered:

  - `is_valid_pattern` — grammar for `<group>/<cap>` and `@<agent_id>`
  - `_matches` — pattern → (agent_id, groups, capabilities)
  - `is_allowed` — full evaluation: order, default deny, self-call,
    user-level gating, trace recording

Pure-function tests; no DB / network. The only complication is that
`bp_router.acl` imports `bp_router.principals` directly, NOT via
`bp_router.security`, so we don't trigger the cryptography panic
that `security/__init__.py` causes in this CI sandbox.
"""

from __future__ import annotations

import pytest

from bp_router.acl import (
    Decision,
    Rule,
    RuleSet,
    TraceStep,
    _matches,
    deployment_levels,
    is_allowed,
    is_valid_pattern,
    is_valid_rule_user_level,
)

# ---------------------------------------------------------------------------
# Pattern grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pat,ok", [
    # Slash form — both halves valid
    ("group_a/cap.read", True),
    ("group/cap.write_v2", True),
    ("a/b.c", True),
    # Wildcards on either half
    ("*/cap.read", True),
    ("group/*", True),
    ("*/*", True),
    # Prefix-glob shapes on the capability half (Phase: prefix globs)
    ("*/llm.*", True),
    ("*/llm.generation.*", True),
    ("*/llm.generation.text.*", True),
    ("group_a/llm.*", True),
    # @<agent_id> form
    ("@gemini_main", True),
    ("@A", True),
    ("@a-b_c", True),
    # Bare names with no slash → invalid
    ("nogroupcap", False),
    ("@", False),
    ("@bad..id", False),
    # Two slashes → invalid (only one allowed)
    ("group/cap/extra", False),
    # Capability-half grammar (must contain a dot)
    ("group/cap_no_dot", False),
    # Group-half grammar (lowercase, can't start with digit)
    ("9group/cap.x", False),
    ("UPPER/cap.x", False),
    # Empty
    ("", False),
    # Prefix-glob — rejected shapes
    ("*/llm*", False),          # missing dot before *
    ("*/*.text", False),         # leading-glob; suffix not supported
    ("*/llm.*.text", False),     # middle-glob
    ("*/llm.**", False),         # double-star
    ("*/.*", False),             # bare ".*" — no prefix
    ("*/llm.*.gen.*", False),    # two globs
    ("group.*/cap.x", False),    # globs on group half NOT supported
])
def test_is_valid_pattern(pat: str, ok: bool) -> None:
    assert is_valid_pattern(pat) is ok


# ---------------------------------------------------------------------------
# Capability prefix-glob matcher behaviour
# ---------------------------------------------------------------------------


def test_prefix_glob_matches_one_or_more_segments_under_prefix() -> None:
    """`llm.*` matches `llm.generation`, `llm.generation.text`,
    `llm.generation.text.detailed`. Anything deeper than the
    prefix counts."""
    from bp_router.acl import _matches

    caps = ["llm.generation.text"]
    assert _matches("*/llm.*", "agt", [], caps) is True
    assert _matches("*/llm.generation.*", "agt", [], caps) is True


def test_prefix_glob_does_not_match_bare_prefix() -> None:
    """`llm.*` does NOT match the literal capability `llm`. The
    trailing dot in `llm.` is required — semantic of `.*` is
    "one or more additional segments"."""
    from bp_router.acl import _matches

    # Note: a bare `llm` isn't a valid capability per
    # CAPABILITY_PATTERN, but the matcher must still refuse.
    assert _matches("*/llm.*", "agt", [], ["llm"]) is False


def test_prefix_glob_does_not_match_unrelated_namespace() -> None:
    """`llm.*` must NOT match `llmpy.foo` — the prefix is
    `llm.` (with the dot), so `llmp` doesn't qualify."""
    from bp_router.acl import _matches

    assert _matches("*/llm.*", "agt", [], ["llmpy.foo"]) is False
    assert _matches("*/llm.*", "agt", [], ["other.llm.foo"]) is False


def test_prefix_glob_at_deeper_segment_is_strict() -> None:
    """`llm.generation.*` matches `llm.generation.text` but NOT
    `llm.generation` (bare prefix) or `llm.generation_x.text`."""
    from bp_router.acl import _matches

    caps_exact = ["llm.generation.text"]
    assert _matches("*/llm.generation.*", "agt", [], caps_exact) is True

    caps_bare = ["llm.generation"]
    assert _matches("*/llm.generation.*", "agt", [], caps_bare) is False

    caps_sibling = ["llm.generation_x.text"]
    assert _matches("*/llm.generation.*", "agt", [], caps_sibling) is False


def test_prefix_glob_with_group_pattern_composes_with_AND() -> None:
    """Group half AND cap half — both must match. Prefix glob on
    one side does NOT relax the other side's requirement."""
    from bp_router.acl import _matches

    # Right group, right cap-prefix → match.
    assert _matches(
        "myteam/llm.*", "agt", ["myteam"], ["llm.gen.text"]
    ) is True
    # Wrong group, right cap-prefix → no match.
    assert _matches(
        "myteam/llm.*", "agt", ["otherteam"], ["llm.gen.text"]
    ) is False
    # Right group, wrong cap → no match.
    assert _matches(
        "myteam/llm.*", "agt", ["myteam"], ["vision.detect"]
    ) is False


def test_exact_capability_still_matches_when_prefix_globs_exist_in_other_rules() -> None:
    """Backwards compat: an exact-match pattern keeps working
    identically. Prefix globs are strictly additive."""
    from bp_router.acl import _matches

    assert _matches(
        "*/llm.generation.text", "agt", [], ["llm.generation.text"]
    ) is True
    assert _matches(
        "*/llm.generation.text", "agt", [], ["llm.generation.image"]
    ) is False


def test_prefix_glob_matches_when_any_capability_satisfies() -> None:
    """An agent with multiple capabilities passes the glob when
    AT LEAST ONE matches — `_matches` is `any(...)`."""
    from bp_router.acl import _matches

    caps = ["vision.detect", "llm.generation.text", "audio.synthesize"]
    assert _matches("*/llm.*", "agt", [], caps) is True


@pytest.mark.parametrize("level,ok", [
    ("*", True),
    ("admin", True),
    ("service", True),
    ("tier0", True),
    ("tier99", True),
    # Invalid
    ("Admin", False),     # case-sensitive
    ("user", False),
    ("tier", False),
    ("tier-1", False),
    ("", False),
])
def test_is_valid_rule_user_level(level: str, ok: bool) -> None:
    assert is_valid_rule_user_level(level) is ok


# ---------------------------------------------------------------------------
# _matches — pattern × (agent_id, groups, capabilities)
# ---------------------------------------------------------------------------


def test_matches_at_id_literal_only_matches_exact_agent() -> None:
    """`@x` matches the literal agent_id `x`. Group/cap membership of
    the agent are irrelevant — `@x` is opt-out from the group system."""
    assert _matches("@gemini_main", "gemini_main", ["llm"], ["llm.chat"])
    assert not _matches("@gemini_main", "gemini_pro", ["llm"], ["llm.chat"])
    # A literal `@` pattern doesn't accidentally match via group/cap
    # equality on the agent_id string itself.
    assert not _matches("@llm", "anything", ["llm"], [])


def test_matches_group_and_cap_both_required() -> None:
    """Both halves must satisfy. A matching group with a non-matching
    cap (or vice versa) does NOT match."""
    assert _matches("llm/llm.chat", "any", ["llm"], ["llm.chat"])
    # Group matches, cap doesn't.
    assert not _matches("llm/llm.embed", "any", ["llm"], ["llm.chat"])
    # Cap matches, group doesn't.
    assert not _matches("vision/llm.chat", "any", ["llm"], ["llm.chat"])
    # Neither matches.
    assert not _matches("vision/llm.embed", "any", ["llm"], ["llm.chat"])


def test_matches_wildcard_group() -> None:
    assert _matches("*/llm.chat", "any", ["any_group"], ["llm.chat"])
    # Group wildcard still requires the cap to match.
    assert not _matches("*/llm.chat", "any", ["any_group"], ["llm.embed"])


def test_matches_wildcard_cap() -> None:
    assert _matches("llm/*", "any", ["llm"], ["any.cap"])
    # Cap wildcard still requires the group to match.
    assert not _matches("llm/*", "any", ["other"], ["any.cap"])


def test_matches_double_wildcard_admits_any_member() -> None:
    """`*/*` admits every agent that has at least the right shape —
    used by allow-all rules."""
    assert _matches("*/*", "any_id", ["g"], ["c.x"])
    # An agent with NO groups/caps at all? Both halves are `*` so the
    # rule still passes — wildcards don't require non-empty membership.
    assert _matches("*/*", "any_id", [], [])


def test_matches_iterable_membership_works_with_frozensets() -> None:
    """`groups` / `capabilities` are passed as `frozenset` from the
    `_AgentView`. Membership check must work."""
    assert _matches(
        "vision/llm.chat",
        "any",
        frozenset({"vision", "extra"}),
        frozenset({"llm.chat"}),
    )


# ---------------------------------------------------------------------------
# is_allowed — full evaluator
# ---------------------------------------------------------------------------


def _rule(
    *,
    ord: int,
    effect: str = "allow",
    user_level: str = "*",
    caller_pattern: str = "*/*",
    callee_pattern: str = "*/*",
    name: str | None = None,
) -> Rule:
    return Rule(
        ord=ord,
        effect=effect,  # type: ignore[arg-type]
        user_level=user_level,
        caller_pattern=caller_pattern,
        callee_pattern=callee_pattern,
        name=name or f"r{ord}",
    )


def _view(agent_id: str, groups=(), capabilities=()):
    from bp_router.acl import _AgentView

    return _AgentView(
        agent_id=agent_id,
        groups=frozenset(groups),
        capabilities=frozenset(capabilities),
    )


def test_default_deny_when_no_rules() -> None:
    decision = is_allowed(
        rules=[],
        caller=_view("a"),
        callee=_view("b"),
        user_level="admin",
    )
    assert decision.allow is False
    assert decision.rule_name == "<default>"


def test_default_deny_when_no_rule_matches() -> None:
    """A rule list that doesn't match falls through to default deny —
    not the LAST rule's effect (a common bug in firewall-style code)."""
    rules = [_rule(ord=0, effect="allow", caller_pattern="@only_this_agent")]
    decision = is_allowed(
        rules=rules,
        caller=_view("not_this_one"),
        callee=_view("b"),
        user_level="admin",
    )
    assert decision.allow is False
    assert decision.rule_name == "<default>"


def test_self_call_always_denied_even_when_rule_matches() -> None:
    """A rule that allows `*/*` MUST NOT permit an agent to call
    itself. Self-call denial is hard-wired."""
    rules = [_rule(ord=0, effect="allow")]
    decision = is_allowed(
        rules=rules,
        caller=_view("agent_x"),
        callee=_view("agent_x"),
        user_level="admin",
    )
    assert decision.allow is False
    assert decision.rule_name == "<self_call>"


def test_first_match_wins_allow_over_deny() -> None:
    """An allow rule earlier in the list beats a later deny."""
    rules = [
        _rule(ord=0, effect="allow", name="permit"),
        _rule(ord=1, effect="deny", name="block"),
    ]
    decision = is_allowed(
        rules=rules, caller=_view("a"), callee=_view("b"), user_level="admin",
    )
    assert decision.allow is True
    assert decision.rule_name == "permit"


def test_first_match_wins_deny_over_allow() -> None:
    """And vice versa — order alone determines outcome."""
    rules = [
        _rule(ord=0, effect="deny", name="block"),
        _rule(ord=1, effect="allow", name="permit"),
    ]
    decision = is_allowed(
        rules=rules, caller=_view("a"), callee=_view("b"), user_level="admin",
    )
    assert decision.allow is False
    assert decision.rule_name == "block"


def test_user_level_gate_skips_rule_when_caller_too_weak() -> None:
    """A rule requiring `tier0` is skipped (not denying) when the
    caller is `tier3`. Skipping lets later rules apply."""
    rules = [
        _rule(ord=0, effect="allow", user_level="tier0", name="strict"),
        _rule(ord=1, effect="allow", user_level="*", name="open"),
    ]
    decision = is_allowed(
        rules=rules, caller=_view("a"), callee=_view("b"), user_level="tier3",
    )
    assert decision.allow is True
    assert decision.rule_name == "open"


def test_admin_service_exact_match_in_evaluator() -> None:
    """The doc fix in PR E spelled this out: `admin` rules don't admit
    `service` and vice versa. Verify against the live evaluator."""
    rules = [
        _rule(ord=0, effect="allow", user_level="admin", name="admin_only"),
    ]
    # admin caller passes.
    assert is_allowed(
        rules=rules, caller=_view("a"), callee=_view("b"), user_level="admin",
    ).allow
    # service caller does NOT match the admin gate → fall through to deny.
    decision = is_allowed(
        rules=rules,
        caller=_view("a"),
        callee=_view("b"),
        user_level="service",
    )
    assert decision.allow is False
    assert decision.rule_name == "<default>"


def test_pattern_match_walks_caller_then_callee() -> None:
    """Caller match must succeed BEFORE callee is even checked. Used to
    make sure trace records the right `skipped_reason`."""
    rules = [
        _rule(
            ord=0,
            effect="allow",
            caller_pattern="@only_caller",
            callee_pattern="*/*",
            name="caller-pinned",
        ),
    ]
    trace: list[TraceStep] = []
    is_allowed(
        rules=rules,
        caller=_view("not_caller"),
        callee=_view("anything"),
        user_level="admin",
        trace=trace,
    )
    assert len(trace) == 1
    assert trace[0].matched is False
    assert trace[0].skipped_reason == "caller"


def test_trace_records_user_level_skip_separately_from_pattern_skip() -> None:
    """Three distinct skipped_reason values: user_level | caller | callee."""
    rules = [
        _rule(ord=0, effect="allow", user_level="tier0", name="tier-skip"),
        _rule(ord=1, effect="allow", caller_pattern="@nope", name="caller-skip"),
        _rule(ord=2, effect="allow", callee_pattern="@nope", name="callee-skip"),
    ]
    trace: list[TraceStep] = []
    is_allowed(
        rules=rules,
        caller=_view("a"),
        callee=_view("b"),
        user_level="tier3",
        trace=trace,
    )
    assert [t.skipped_reason for t in trace] == ["user_level", "caller", "callee"]


def test_trace_records_match_when_rule_fires() -> None:
    rules = [_rule(ord=0, effect="allow", name="match_me")]
    trace: list[TraceStep] = []
    decision = is_allowed(
        rules=rules,
        caller=_view("a"),
        callee=_view("b"),
        user_level="admin",
        trace=trace,
    )
    assert decision.allow
    assert len(trace) == 1
    assert trace[0].matched is True
    assert trace[0].skipped_reason is None


def test_trace_self_call_recorded() -> None:
    trace: list[TraceStep] = []
    is_allowed(
        rules=[],
        caller=_view("same"),
        callee=_view("same"),
        user_level="admin",
        trace=trace,
    )
    # Self-call short-circuit emits a `<self_call>` step — same
    # string as the Decision.rule_name and the metric label, so
    # simulate trace + metric agree (R5 fix).
    assert len(trace) == 1
    assert trace[0].rule_name == "<self_call>"


# ---------------------------------------------------------------------------
# Decision factories
# ---------------------------------------------------------------------------


def test_decision_deny_factory_includes_reason() -> None:
    d = Decision.deny(reason="default")
    assert d.allow is False
    assert d.rule_name == "<default>"


def test_decision_allow_via_uses_rule_name_when_set() -> None:
    rule = _rule(ord=0, effect="allow", name="my_rule")
    d = Decision.allow_via(rule)
    assert d.allow is True
    assert d.rule_name == "my_rule"


def test_decision_allow_via_falls_back_to_unnamed() -> None:
    rule = Rule(
        ord=0,
        effect="allow",
        user_level="*",
        caller_pattern="*/*",
        callee_pattern="*/*",
    )
    d = Decision.allow_via(rule)
    assert d.rule_name == "<unnamed>"


# ---------------------------------------------------------------------------
# RuleSet
# ---------------------------------------------------------------------------


def test_ruleset_sorts_by_ord_on_construction() -> None:
    rs = RuleSet([
        _rule(ord=2, name="c"),
        _rule(ord=0, name="a"),
        _rule(ord=1, name="b"),
    ])
    assert [r.name for r in rs.rules] == ["a", "b", "c"]
    assert len(rs) == 3


def test_ruleset_replace_re_sorts() -> None:
    rs = RuleSet([_rule(ord=0, name="a")])
    rs.replace([
        _rule(ord=5, name="z"),
        _rule(ord=1, name="b"),
    ])
    assert [r.name for r in rs.rules] == ["b", "z"]


# ---------------------------------------------------------------------------
# deployment_levels helper
# ---------------------------------------------------------------------------


def test_deployment_levels_includes_admin_service_and_tiers() -> None:
    assert deployment_levels(0) == ["admin", "service", "tier0"]
    assert deployment_levels(3) == [
        "admin", "service", "tier0", "tier1", "tier2", "tier3",
    ]


def test_deployment_levels_rejects_negative() -> None:
    with pytest.raises(ValueError):
        deployment_levels(-1)
