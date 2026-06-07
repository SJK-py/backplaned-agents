"""bp_router.principals — Single source of truth for the user-level grammar.

Every user is classified by exactly one `level`:

    admin         the only level that satisfies require_admin
    service       automated principal; equivalent to tier0 for require_tier
    tierN         human user at tier N (0 = most privileged, N = least)

Tier ordering matches the agent-tier convention used elsewhere in the
codebase (see `docs/backplaned/acl.md` §3.4): lower number = more privileged.
`require_tier(N)` admits any level whose tier index is ≤ N, so e.g.
`require_tier(2)` accepts admin, service, tier0, tier1, tier2 and
rejects tier3+.
"""

from __future__ import annotations

import re

LEVEL_PATTERN = re.compile(r"^(admin|service|tier[0-9]+)$")
"""Regex enforced by the DB CHECK constraint and validators."""

_TIER_PATTERN = re.compile(r"^tier([0-9]+)$")


def is_valid_level(level: str) -> bool:
    return bool(LEVEL_PATTERN.match(level))


def tier_index(level: str | None) -> int | None:
    """Return the numeric tier used by `require_tier` and `tier_at_most`.

    Returns:
      -1 for `admin` / `service` (always satisfy any tier ceiling).
       N for `tierN`.
       None for unknown / invalid input.
    """
    if level in ("admin", "service"):
        return -1
    if not level:
        return None
    m = _TIER_PATTERN.match(level)
    if not m:
        return None
    return int(m.group(1))


def level_satisfies_tier(level: str | None, max_tier: int) -> bool:
    """Whether `level` passes `require_tier(max_tier)`."""
    idx = tier_index(level)
    return idx is not None and idx <= max_tier


def user_level_satisfies(actual: str | None, rule_level: str) -> bool:
    """Does `actual` (the caller's level) satisfy a rule's
    `user_level` constraint?

    Rule semantics:
      - `*`                  admits any caller (even unknown / None)
      - `admin` / `service`  exact-match only
      - `tierN`              "this tier or stricter" (lower number)

    Centralised here so the ACL evaluator (`bp_router.acl`) and
    the LLM preset gate (`bp_router.llm.presets`) share one
    grammar. A future grammar change (e.g. `super_admin`, a
    new `service:<scope>` shape) updates one place; the prior
    in-module duplicates were a known drift risk flagged in the
    R4 second-pass review.
    """
    if rule_level == "*":
        return True
    if rule_level in ("admin", "service"):
        return actual == rule_level
    # rule_level is `tierN`. Reject malformed values.
    idx = tier_index(rule_level)
    if idx is None:
        return False
    return level_satisfies_tier(actual, idx)


SERVICE_USER_ID_PREFIX = "usr_service_"
"""Reserved `user_id` prefix for the co-located service principal minted
at agent onboarding (`usr_service_{agent_id}`). Caller-supplied user_ids
may NOT use this prefix (enforced in `CreateUserRequest`); reserving it
keeps the auto-provisioned principal greppable and unambiguous within
user-space — an unprefixed id like `service_chatbot` would also be a
valid `agent_id`."""


def service_user_id_for_agent(agent_id: str) -> str:
    """The deterministic `user_id` of the `level=service` principal
    co-located with `agent_id`. Mirrors the agent's identity into
    user-space so a channel agent can act as a service user over HTTP
    (mint per-user tokens, submit registrations) without a separately
    admin-provisioned account — see `api/onboard.py`."""
    return f"{SERVICE_USER_ID_PREFIX}{agent_id}"


MCP_BRIDGE_USER_ID = "service_mcp"
"""Fixed `user_id` of the MCP bridge's `level=service` principal. Unlike the
auto-provisioned `usr_service_*` agents (one per onboarded agent), the bridge is
a single operator-configured daemon, so its identity is a fixed, well-known id
seeded from `ROUTER_MCP_BRIDGE_SECRET` (see `app._bootstrap_mcp_bridge_user`).
Deliberately NOT under `SERVICE_USER_ID_PREFIX` so it never collides with an
auto-provisioned principal, and so the bridge endpoints can gate on an exact
id match (`require_mcp_bridge`) rather than a new capability system."""
