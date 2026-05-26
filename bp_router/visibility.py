"""bp_router.visibility — Catalog construction for the Welcome frame.

Thin wrapper over the firewall ACL evaluator. Used at WS handshake
and at HTTP onboarding to project the agent's view of every other
active agent.
"""

from __future__ import annotations

from typing import Any

from bp_router.acl import (
    Rule,
    compute_callable_user_levels,
    deployment_levels,
    is_visible,
)
from bp_router.db.models import AgentRow


def available_destinations(
    caller: AgentRow,
    candidates: list[AgentRow],
    rules: list[Rule],
    *,
    max_tier: int,
) -> dict[str, dict[str, Any]]:
    """Build the catalog injected into `WelcomeFrame.available_destinations`.

    Each entry includes a `callable_user_levels` list — the subset of
    `deployment_levels(max_tier)` for which `(caller → callee)` is
    allowed by the rule list. The SDK uses this to filter outbound
    LLM tool schemas without re-evaluating rules.

    Agents the caller cannot see at any user level are omitted entirely.
    """
    levels = deployment_levels(max_tier)
    result: dict[str, dict[str, Any]] = {}
    for agent in candidates:
        if agent.status != "active":
            continue
        if agent.agent_id == caller.agent_id:
            continue  # never list self
        callable_levels = compute_callable_user_levels(
            rules,
            caller_id=caller.agent_id,
            caller_groups=caller.groups,
            caller_capabilities=caller.capabilities,
            callee_id=agent.agent_id,
            callee_groups=agent.groups,
            callee_capabilities=agent.capabilities,
            deployment_levels=levels,
        )
        if not callable_levels:
            continue
        info = agent.agent_info or {}
        result[agent.agent_id] = {
            "description": info.get("description", ""),
            "groups": agent.groups,
            "capabilities": agent.capabilities,
            "accepts_schema": info.get("accepts_schema"),
            "non_tool_modes": info.get("non_tool_modes", []),
            "hidden": info.get("hidden", False),
            "documentation_url": info.get("documentation_url"),
            "callable_user_levels": callable_levels,
            # ISO-8601 string of the agent's last successful WS handshake.
            # Hint for admin UIs / LLMs to gauge staleness; does not
            # affect ACL eligibility (catalog membership = registered +
            # rule-allowed, not currently online).
            "last_seen_at": (
                agent.last_seen_at.isoformat() if agent.last_seen_at else None
            ),
        }
    return result


__all__ = ["available_destinations", "is_visible"]
