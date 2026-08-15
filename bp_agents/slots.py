"""bp_agents.slots — the suite's LLM preset-slot taxonomy.

A **slot** is an opaque per-user preference key the router resolves into a
preset at call time ([../docs/design/router-resolved-preset-slots.md]). The
router never interprets a slot's name; the meaning below is the suite's,
and it is the only place it is written down.

    PRO       deep_reasoning — the expensive, careful model
    BALANCED  the assistant and its specialists — the default workhorse
    LITE      short mechanical calls (summaries, extraction, distillation)

The operator maps each slot to a default preset via the router's
`ROUTER_LLM_DEFAULT_PRESETS`; a user may prefer any preset their tier
admits, and the router degrades (never refuses) a preference that stops
being admissible.

**Embedding is deliberately absent.** It is not safe to change between
turns — every vector already written was produced by one model — so it
stays operator configuration (`SuiteSettings.default_preset_embedding`)
on the explicit `preset=` path. Adding it here would be a data-loss bug
wearing a preference's clothes (design §12).
"""

from __future__ import annotations

PRO = "pro"
BALANCED = "balanced"
LITE = "lite"

# Ordered most- to least-capable; the webapp renders the settings form in
# this order.
SLOTS: tuple[str, ...] = (PRO, BALANCED, LITE)

# What each slot is FOR, in the user's terms. The router's preset list
# carries per-preset descriptions; this describes the slot itself.
SLOT_LABELS: dict[str, str] = {
    PRO: "Deep reasoning",
    BALANCED: "Assistant & specialists",
    LITE: "Quick helpers",
}

SLOT_HELP: dict[str, str] = {
    PRO: "Used when you ask for careful, long-form analysis or planning.",
    BALANCED: "Your day-to-day model — the assistant and its specialists.",
    LITE: "Short mechanical work: summaries, extraction, page distillation.",
}
