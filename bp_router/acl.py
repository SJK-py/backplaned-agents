"""bp_router.acl — Firewall-style ACL evaluator.

Single ordered rule list. Each rule is a 4-tuple:

    <effect>  <user_level>  <caller_pattern>  ->  <callee_pattern>

First-match-wins, default deny, self-call always denied.

See `docs/acl.md` for the full specification.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from bp_router.principals import (
    LEVEL_PATTERN,
    is_valid_level,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identifier grammars (mirror docs/acl.md §10)
# ---------------------------------------------------------------------------

AGENT_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
GROUP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_:.\-]{0,63}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
USER_LEVEL_RULE_PATTERN = re.compile(r"^(\*|admin|service|tier[0-9]+)$")


# Prefix-glob form for the capability half of a slash pattern.
# Accepts `prefix.*` where `prefix` is one or more dotted lowercase
# segments. Trailing `.*` is the ONLY supported wildcard form
# beyond the whole-token `*`; leading globs (`*.x`), middle globs
# (`x.*.y`), and double-stars (`x.**`) are deliberately rejected
# to keep precedence semantics predictable. See
# `docs/acl.md` for the rationale.
_CAPABILITY_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


def _is_capability_prefix_glob(cap_part: str) -> bool:
    """True iff `cap_part` is a `<prefix>.*` prefix-glob shape."""
    if not cap_part.endswith(".*"):
        return False
    prefix = cap_part[:-2]
    return bool(_CAPABILITY_PREFIX_PATTERN.match(prefix))


def is_valid_pattern(pattern: str) -> bool:
    """True iff `pattern` is one of `<group>/<cap>` or `@<agent_id>`,
    with `*` allowed in either half of the slash form and trailing
    `.*` prefix-globs allowed in the capability half."""
    if pattern.startswith("@"):
        return bool(AGENT_ID_PATTERN.match(pattern[1:]))
    if "/" not in pattern:
        return False
    group_part, _, cap_part = pattern.partition("/")
    if "/" in cap_part:
        return False  # only one slash allowed
    group_ok = group_part == "*" or bool(GROUP_NAME_PATTERN.match(group_part))
    cap_ok = (
        cap_part == "*"
        or bool(CAPABILITY_PATTERN.match(cap_part))
        or _is_capability_prefix_glob(cap_part)
    )
    return group_ok and cap_ok


def is_valid_rule_user_level(s: str) -> bool:
    return bool(USER_LEVEL_RULE_PATTERN.match(s))


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


RuleEffect = Literal["allow", "deny"]


class Rule(BaseModel):
    """One firewall line. Validated end-to-end via the field validators
    below; any rule that survives parsing has legal grammar."""

    rule_id: str | None = None
    """Server-assigned `rule_<token>`. Optional on insert."""

    ord: int = Field(ge=0)
    """Evaluation order. Lower wins."""

    name: str | None = None
    description: str | None = None
    effect: RuleEffect
    user_level: str
    caller_pattern: str
    callee_pattern: str

    @field_validator("user_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        if not is_valid_rule_user_level(v):
            raise ValueError(
                "user_level must be '*' or admin | service | tierN"
            )
        return v

    @field_validator("caller_pattern", "callee_pattern")
    @classmethod
    def _check_pattern(cls, v: str) -> str:
        if not is_valid_pattern(v):
            raise ValueError(
                "pattern must be '<group>/<cap>' or '@<agent_id>'; "
                "wildcards '*' allowed in either half of the slash form"
            )
        return v


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule_name: str | None
    """Matched rule's `name`, or `<self_call>` / `<default>` for
    synthetic outcomes. Each synthetic label is one string used by
    the trace step (simulate endpoint), the `Decision.rule_name`
    field, and the `acl_decisions_total{rule_name}` metric — kept
    consistent so operators can correlate the three views without
    a renaming table."""

    @classmethod
    def deny(cls, *, reason: str) -> Decision:
        return cls(allow=False, rule_name=f"<{reason}>")

    @classmethod
    def allow_via(cls, rule: Rule) -> Decision:
        return cls(allow=True, rule_name=rule.name or rule.rule_id or "<unnamed>")

    @classmethod
    def deny_via(cls, rule: Rule) -> Decision:
        return cls(allow=False, rule_name=rule.name or rule.rule_id or "<unnamed>")


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _matches(pattern: str, agent_id: str, groups: Iterable[str], capabilities: Iterable[str]) -> bool:
    """Whether `pattern` matches the (agent_id, groups, capabilities) view.

    Capability half supports three shapes (validated at admission):
      * `*`                  → matches any capability
      * `<full.capability>`  → exact string membership
      * `<prefix>.*`         → prefix glob; matches any capability
                               of the form `<prefix>.<one-or-more-segments>`.
                               Does NOT match the bare `<prefix>` —
                               the literal dot before `*` is required.

    Group half supports `*` or exact membership only (no prefix
    globs — groups are flat tags by design)."""
    if pattern.startswith("@"):
        return pattern[1:] == agent_id
    group_part, _, cap_part = pattern.partition("/")
    group_ok = group_part == "*" or group_part in groups
    if cap_part == "*":
        cap_ok = True
    elif cap_part.endswith(".*"):
        # Prefix-glob: e.g. `llm.*` → `llm.` → match any capability
        # starting with that string. The trailing dot in the prefix
        # is significant — it gates against `llmpy.foo` matching
        # `llm.*`.
        prefix_dot = cap_part[:-1]
        cap_ok = any(c.startswith(prefix_dot) for c in capabilities)
    else:
        cap_ok = cap_part in capabilities
    return group_ok and cap_ok


# Centralised in `bp_router.principals.user_level_satisfies` (R4
# second-pass review dedup — was duplicated here + in
# `bp_router/llm/presets.py`). Local alias preserved so callers
# in this file keep the original name; new callers should import
# directly from `principals`.
from bp_router.principals import user_level_satisfies as _user_level_satisfies  # noqa: E402

# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgentView:
    """Minimal projection used by the matcher; either side of an evaluation."""

    agent_id: str
    groups: frozenset[str]
    capabilities: frozenset[str]


def _view(agent_id: str, groups: Iterable[str], capabilities: Iterable[str]) -> _AgentView:
    return _AgentView(
        agent_id=agent_id,
        groups=frozenset(groups),
        capabilities=frozenset(capabilities),
    )


@dataclass
class TraceStep:
    """One evaluation step recorded for the simulate endpoint."""

    rule_id: str | None
    rule_name: str | None
    matched: bool
    skipped_reason: str | None = None  # "user_level" | "caller" | "callee" | None


def is_allowed(
    rules: list[Rule],
    *,
    caller: _AgentView,
    callee: _AgentView,
    user_level: str,
    trace: list[TraceStep] | None = None,
) -> Decision:
    """Walk `rules` in order; return on first match, default deny.

    `trace`, if supplied, is appended-to for each rule considered —
    used by `/v1/admin/acl/rules/simulate`.
    """
    if caller.agent_id == callee.agent_id:
        if trace is not None:
            # Same string as the Decision.rule_name produced below
            # (`<self_call>`) so simulate-trace readers and the
            # `acl_decisions_total{rule_name}` metric agree on the
            # label. Pre-R5 the trace step said `<self>` while the
            # metric path checked `<self_call>` — two strings for
            # the same outcome.
            trace.append(TraceStep(None, "<self_call>", matched=True))
        return Decision.deny(reason="self_call")

    for rule in rules:
        if not _user_level_satisfies(user_level, rule.user_level):
            if trace is not None:
                trace.append(TraceStep(rule.rule_id, rule.name, False, "user_level"))
            continue
        if not _matches(rule.caller_pattern, caller.agent_id, caller.groups, caller.capabilities):
            if trace is not None:
                trace.append(TraceStep(rule.rule_id, rule.name, False, "caller"))
            continue
        if not _matches(rule.callee_pattern, callee.agent_id, callee.groups, callee.capabilities):
            if trace is not None:
                trace.append(TraceStep(rule.rule_id, rule.name, False, "callee"))
            continue
        if trace is not None:
            trace.append(TraceStep(rule.rule_id, rule.name, True))
        return Decision.allow_via(rule) if rule.effect == "allow" else Decision.deny_via(rule)

    return Decision.deny(reason="default")


# Convenience wrappers — the rest of the router code calls these rather than
# `is_allowed` directly so the _AgentView construction lives in one place.


def is_allowed_for(
    rules: list[Rule],
    *,
    caller_id: str,
    caller_groups: Iterable[str],
    caller_capabilities: Iterable[str],
    callee_id: str,
    callee_groups: Iterable[str],
    callee_capabilities: Iterable[str],
    user_level: str,
    trace: list[TraceStep] | None = None,
) -> Decision:
    decision = is_allowed(
        rules,
        caller=_view(caller_id, caller_groups, caller_capabilities),
        callee=_view(callee_id, callee_groups, callee_capabilities),
        user_level=user_level,
        trace=trace,
    )
    _record_metric(decision)
    return decision


def compute_callable_user_levels(
    rules: list[Rule],
    *,
    caller_id: str,
    caller_groups: Iterable[str],
    caller_capabilities: Iterable[str],
    callee_id: str,
    callee_groups: Iterable[str],
    callee_capabilities: Iterable[str],
    deployment_levels: Iterable[str],
) -> list[str]:
    """Subset of `deployment_levels` for which calling callee is allowed.

    Used by Welcome-frame catalog construction so SDK callers can
    filter outbound tools by `ctx.user_level ∈ callable_user_levels`
    without re-evaluating rules.
    """
    caller_view = _view(caller_id, caller_groups, caller_capabilities)
    callee_view = _view(callee_id, callee_groups, callee_capabilities)
    allowed: list[str] = []
    for lvl in deployment_levels:
        decision = is_allowed(
            rules, caller=caller_view, callee=callee_view, user_level=lvl
        )
        # R5: emit a `decision="visibility"` metric per probe so
        # operators can graph catalog-construction load alongside
        # the existing `decision="permission"` admit-time
        # decisions. Cardinality is bounded by the helper.
        _record_visibility_metric(decision)
        if decision.allow:
            allowed.append(lvl)
    return allowed


def is_visible(
    rules: list[Rule],
    *,
    caller_id: str,
    caller_groups: Iterable[str],
    caller_capabilities: Iterable[str],
    callee_id: str,
    callee_groups: Iterable[str],
    callee_capabilities: Iterable[str],
    deployment_levels: Iterable[str],
) -> bool:
    """Generous visibility — true if any deployment level admits the call."""
    return bool(
        compute_callable_user_levels(
            rules,
            caller_id=caller_id,
            caller_groups=caller_groups,
            caller_capabilities=caller_capabilities,
            callee_id=callee_id,
            callee_groups=callee_groups,
            callee_capabilities=callee_capabilities,
            deployment_levels=deployment_levels,
        )
    )


def deployment_levels(max_tier: int) -> list[str]:
    """`["admin", "service", "tier0", "tier1", ..., f"tier{max_tier}"]`."""
    if max_tier < 0:
        raise ValueError("max_tier must be non-negative")
    return ["admin", "service", *(f"tier{i}" for i in range(max_tier + 1))]


# ---------------------------------------------------------------------------
# Metric recording
# ---------------------------------------------------------------------------


def _record_metric(decision: Decision) -> None:
    """Record one ACL decision in the Prometheus counter.

    The `decision` label is hard-coded to "permission" because
    `is_allowed_for` (the only function that calls us) is invoked
    exclusively from `admit_task`. The catalog-construction
    visibility path emits with `decision="visibility"` via
    `_record_visibility_metric` instead (R5 second-pass review —
    the spec promised both labels; only "permission" was wired).
    """
    try:
        from bp_router.observability.metrics import acl_decisions_total  # noqa: PLC0415

        if decision.rule_name == "<default>":
            effect = "default_deny"
        elif decision.rule_name == "<self_call>":
            effect = "self_deny"
        else:
            effect = "allow" if decision.allow else "deny"
        acl_decisions_total.labels(
            decision="permission",
            effect=effect,
            rule_name=_bound_metric_label(decision.rule_name),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.debug("acl metric record failed", exc_info=True)


# Bound on the `rule_name` label sent to Prometheus. Admins can
# name rules with arbitrary strings (description fields,
# emoji-heavy names, accidental request-id pasting) which become
# Prometheus label values. The client never expires series — each
# unique combination of (decision, effect, rule_name) lives in
# the registry forever. An admin pasting unique strings into rule
# names blows up the series count.
#
# This bounder:
#   - replaces an empty / None rule_name with `<unnamed>`
#   - truncates to 64 chars
#   - replaces every char outside `[A-Za-z0-9_:.\-<>]` with `_`
#     (the angle brackets keep synthetic labels like `<default>`,
#     `<self_call>`, `<unnamed>` intact)
#
# This is purely a metric-side bound — the underlying
# `Rule.name` and `Decision.rule_name` keep the admin-supplied
# string. Operators correlating the metric with audit log can
# still match: the bounder is deterministic, so `rule_name=...`
# in the metric maps back to the slug-form of the audit log's
# rule_name. R5 second-pass review.
_METRIC_RULE_NAME_MAX_LEN = 64
_METRIC_RULE_NAME_REPLACE_RE = re.compile(r"[^A-Za-z0-9_:.\-<>]")


def _bound_metric_label(rule_name: str | None) -> str:
    if not rule_name:
        return "<unnamed>"
    sanitized = _METRIC_RULE_NAME_REPLACE_RE.sub("_", rule_name)
    if len(sanitized) > _METRIC_RULE_NAME_MAX_LEN:
        sanitized = sanitized[:_METRIC_RULE_NAME_MAX_LEN]
    return sanitized


def _record_visibility_metric(decision: Decision) -> None:
    """Record one visibility-probe decision.

    The catalog-construction path
    (`compute_callable_user_levels`) emits one of these per
    (callee, user_level) it evaluates. Cardinality is bounded by
    pinning the synthetic `rule_name` label to `<batch>` regardless
    of which rule actually matched — the per-pair detail isn't
    actionable from /metrics and would inflate the series count
    (50 agents × 4 levels × 50 callers each handshake = high).

    R5 second-pass review: the `acl_decisions_total` metric
    declared a `decision` label whose `visibility` value was
    never emitted; this wires it.
    """
    try:
        from bp_router.observability.metrics import acl_decisions_total  # noqa: PLC0415

        if decision.rule_name == "<default>":
            effect = "default_deny"
        elif decision.rule_name == "<self_call>":
            effect = "self_deny"
        else:
            effect = "allow" if decision.allow else "deny"
        acl_decisions_total.labels(
            decision="visibility",
            effect=effect,
            rule_name="<batch>",
        ).inc()
    except Exception:  # noqa: BLE001
        logger.debug("acl visibility metric record failed", exc_info=True)


# ---------------------------------------------------------------------------
# In-memory rule store
# ---------------------------------------------------------------------------


class RuleSet:
    """Hot-swappable list of rules sorted by `ord`."""

    def __init__(self, rules: list[Rule]) -> None:
        self._rules = sorted(rules, key=lambda r: r.ord)

    @property
    def rules(self) -> list[Rule]:
        return self._rules

    def replace(self, rules: list[Rule]) -> None:
        self._rules = sorted(rules, key=lambda r: r.ord)

    def __len__(self) -> int:
        return len(self._rules)


# Re-exports for callers that import from `bp_router.acl`.
__all__ = [
    "AGENT_ID_PATTERN",
    "CAPABILITY_PATTERN",
    "Decision",
    "GROUP_NAME_PATTERN",
    "LEVEL_PATTERN",
    "Rule",
    "RuleEffect",
    "RuleSet",
    "TraceStep",
    "compute_callable_user_levels",
    "deployment_levels",
    "is_allowed",
    "is_allowed_for",
    "is_valid_level",
    "is_valid_pattern",
    "is_valid_rule_user_level",
    "is_visible",
]
