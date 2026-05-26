"""bp_router.llm.presets — Preset resolution + tier gate.

A **preset** is a named bundle of (provider, concrete_model,
api_key_ref, sampling defaults, provider_options defaults,
min_user_level). Agents call `ctx.llm.generate(preset="...")` and
the service resolves the bundle, checks the tier gate against the
caller's user level, then dispatches.

Two design decisions made up-front:

1. **Override semantics for `provider_options`** — call-time
   `provider_options` REPLACES the preset's default dict entirely.
   Agents wanting partial tweaks need to spread the preset's defaults
   themselves at call time. The merge alternative is more useful but
   harder to reason about; replace is predictable.

2. **Tier check** — uses the same `_user_level_satisfies` semantics
   as the ACL evaluator: `*` admits any, `admin`/`service` are exact
   matches, `tierN` is "this tier or stricter" (lower number).

3. **Default-seed fallback** — on first startup the database is empty;
   the service seeds it from `DEFAULT_PRESETS` (which mirror the
   pre-preset built-in alias map). After that, presets live in the
   `llm_presets` DB table and are admin-managed via the webUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bp_router.principals import level_satisfies_tier, tier_index

# ---------------------------------------------------------------------------
# Neutral preset shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """A resolvable preset. Mirrors `LlmPresetRow` but lives in
    memory so the LLM service can hold a cached copy and reload on
    admin edits.

    `api_key` is the inline-secret alternative to `api_key_ref`. When
    both are set, `api_key` wins (the resolver short-circuits). It is
    NEVER surfaced in API responses — see `bp_router.api.admin._preset_to_view`.

    `base_url` overrides the upstream endpoint. OPTIONAL for hosted
    providers (`gemini` / `anthropic` / `openai` / `openai-embeddings`):
    blank → the SDK's official default URL; set → that endpoint
    instead — i.e. a third-party server speaking the Anthropic or
    Gemini API (MiniMax, Bedrock-fronted Anthropic, Vertex / EU
    Gemini, LiteLLM / Portkey gateways, enterprise auth proxies).
    REQUIRED for `openai-compatible(-embeddings)` (vLLM / LM Studio /
    llama.cpp-server / Ollama OpenAI-mode — no official default to
    fall back to). Either way it is SSRF-validated at admin save
    time (`url_validation.validate_base_url`); see
    `docs/sdk/services.md` §1.1.2.

    `fallback_preset` is the next preset to try after `max_retries+1`
    attempts on this one all fail. None means "no fallback, surface
    the error". Cycles are rejected at load time.
    """

    name: str
    provider: str
    concrete_model: str
    api_key_ref: str
    min_user_level: str = "*"
    description: str | None = None
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_provider_options: dict[str, Any] = field(default_factory=dict)
    api_key: str | None = None
    base_url: str | None = None
    fallback_preset: str | None = None
    max_retries: int = 0


# ---------------------------------------------------------------------------
# Tier gate
# ---------------------------------------------------------------------------


# Centralised in `bp_router.principals.user_level_satisfies` so
# the ACL evaluator (acl.py) and this preset gate share one
# grammar. The two were textually duplicated before R4; a future
# grammar change (e.g. `super_admin`) updates one place.
from bp_router.principals import user_level_satisfies  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Override resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCallParams:
    """The flat set of parameters the LLM service hands to a provider
    adapter after applying preset defaults + call-time overrides.

    `api_key` is the inline secret when the preset has one set;
    `api_key_ref` is the reference (e.g. ``env://OPENAI_API_KEY``).
    The service prefers `api_key` when truthy and falls back to
    resolving `api_key_ref` otherwise.

    `base_url` flows through unchanged from the preset. ALL provider
    adapters consume it when set — the hosted adapters
    (`gemini` / `anthropic` / `openai` / `openai-embeddings`) pass it
    to the SDK to override the official endpoint (third-party
    Anthropic-/Gemini-compatible servers, Bedrock/Vertex mirrors,
    LiteLLM / Portkey gateways); `openai-compatible*` require it.
    Blank/None → the SDK's built-in default endpoint.
    """

    provider: str
    concrete_model: str
    api_key_ref: str
    temperature: float | None
    max_tokens: int | None
    provider_options: dict[str, Any] | None
    api_key: str | None = None
    base_url: str | None = None


def resolve_call_params(
    preset: Preset,
    *,
    temperature: float | None,
    max_tokens: int | None,
    provider_options: dict[str, Any] | None,
) -> ResolvedCallParams:
    """Apply preset defaults + call-time overrides.

    Top-level scalars (`temperature`, `max_tokens`) — call-time wins
    when set, otherwise fall back to the preset's default.

    `provider_options` — call-time REPLACES the preset's default dict
    entirely (see module docstring). When the agent doesn't pass it,
    the preset's default flows through unchanged.
    """
    return ResolvedCallParams(
        provider=preset.provider,
        concrete_model=preset.concrete_model,
        api_key_ref=preset.api_key_ref,
        api_key=preset.api_key,
        base_url=preset.base_url,
        temperature=(
            temperature if temperature is not None else preset.default_temperature
        ),
        max_tokens=(
            max_tokens if max_tokens is not None else preset.default_max_tokens
        ),
        provider_options=(
            provider_options
            if provider_options is not None
            else (
                dict(preset.default_provider_options)
                if preset.default_provider_options
                else None
            )
        ),
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PresetUnknownError(KeyError):
    """The agent asked for a preset name we don't know."""


class PresetNotAllowedError(PermissionError):
    """The caller's user level doesn't satisfy the preset's
    `min_user_level` gate."""

    def __init__(self, preset_name: str, user_level: str | None, required: str) -> None:
        super().__init__(
            f"preset {preset_name!r} requires min_user_level={required!r}; "
            f"caller is at level {user_level!r}"
        )
        self.preset_name = preset_name
        self.user_level = user_level
        self.required = required


class PresetCycleError(ValueError):
    """`fallback_preset` chain contains a cycle. Raised at load time
    so a misconfigured set of presets fails loud — at startup or on
    admin save — rather than infinite-looping at request time."""


def detect_fallback_cycles(presets: dict[str, Preset]) -> None:
    """Walk every preset's fallback chain. Raises `PresetCycleError`
    on the first cycle found. O(N) per call (each node is visited at
    most twice across all walks)."""
    # Standard 3-color DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in presets}

    def visit(name: str, trail: list[str]) -> None:
        if name not in presets:
            # Dangling fallback target → not a cycle. Skipping is
            # correct: the runtime walker treats unknowns as terminal
            # (no further fallback) and surfaces the original error.
            return
        if color[name] == BLACK:
            return
        if color[name] == GRAY:
            cycle_start = trail.index(name)
            chain = " -> ".join(trail[cycle_start:] + [name])
            raise PresetCycleError(f"fallback cycle: {chain}")
        color[name] = GRAY
        trail.append(name)
        nxt = presets[name].fallback_preset
        if nxt is not None:
            visit(nxt, trail)
        trail.pop()
        color[name] = BLACK

    for name in presets:
        if color[name] == WHITE:
            visit(name, [])


def walk_fallback_chain(
    presets: dict[str, Preset], start: str
) -> list[Preset]:
    """Return the ordered list of presets to try, starting at `start`
    and following `fallback_preset` until None or an unknown name.

    Cycle protection: if `start` (or any link) is missing, we stop.
    Cycle prevention is delegated to `detect_fallback_cycles` at load
    time, but we also defensively cap the walk via a `seen` set so a
    runtime mutation that introduces a cycle can't infinite-loop.
    """
    chain: list[Preset] = []
    seen: set[str] = set()
    cur: str | None = start
    while cur is not None and cur in presets and cur not in seen:
        seen.add(cur)
        preset = presets[cur]
        chain.append(preset)
        cur = preset.fallback_preset
    return chain


# ---------------------------------------------------------------------------
# Default seed
# ---------------------------------------------------------------------------


def default_presets() -> list[Preset]:
    """Built-in presets seeded into an empty `llm_presets` table on
    first startup. Mirrors the pre-preset alias map exactly so
    deployments using the old `model="..."` kwarg keep working
    unchanged.

    Default `min_user_level="*"` (any tier) preserves the prior
    no-gate behaviour. Operators can tighten via the admin webUI.
    """
    return [
        # ----- Gemini family -----
        Preset(
            name="default",
            provider="gemini",
            concrete_model="gemini-3.5-flash",
            api_key_ref="env://GEMINI_API_KEY",
            description="Default fast Gemini model. Open to all tiers.",
        ),
        Preset(
            name="default_embedding",
            provider="gemini",
            concrete_model="gemini-embedding-2",
            api_key_ref="env://GEMINI_API_KEY",
            description="Default embedding model (Gemini). Open to all tiers.",
            default_provider_options={"output_dimensionality": 1536},
        ),
        # Preset NAMES use `-` instead of `.` because the
        # `llm_presets.name` CHECK constraint disallows `.`
        # (`^[a-z][a-z0-9_-]{0,63}$`). The `concrete_model`
        # field — which is the actual upstream model identifier
        # the provider SDK sees — keeps the dotted form
        # (`gemini-3.5-flash` etc.) since that's what google-genai
        # expects on the wire.
        Preset(
            name="gemini",
            provider="gemini",
            concrete_model="gemini-3.5-flash",
            api_key_ref="env://GEMINI_API_KEY",
        ),
        Preset(
            name="gemini-2-5-pro",
            provider="gemini",
            concrete_model="gemini-2.5-pro",
            api_key_ref="env://GEMINI_API_KEY",
        ),
        Preset(
            name="gemini-3-5-flash",
            provider="gemini",
            concrete_model="gemini-3.5-flash",
            api_key_ref="env://GEMINI_API_KEY",
        ),
        Preset(
            name="gemini-3-1-flash-lite",
            provider="gemini",
            concrete_model="gemini-3.1-flash-lite",
            api_key_ref="env://GEMINI_API_KEY",
        ),
        Preset(
            name="gemini-3-1-pro",
            provider="gemini",
            concrete_model="gemini-3.1-pro-preview",
            api_key_ref="env://GEMINI_API_KEY",
        ),
        # Gemini embeddings ride the same provider adapter — its `embed()`
        # uses `concrete_model`, so no separate embeddings provider is needed.
        Preset(
            name="gemini-embedding-2",
            provider="gemini",
            concrete_model="gemini-embedding-2",
            api_key_ref="env://GEMINI_API_KEY",
            default_provider_options={"output_dimensionality": 1536},
        ),
        # ----- Anthropic / Claude family -----
        Preset(
            name="claude",
            provider="anthropic",
            concrete_model="claude-sonnet-4-6",
            api_key_ref="env://ANTHROPIC_API_KEY",
            description="General-purpose Claude (Sonnet). Open to all tiers.",
        ),
        Preset(
            name="claude-opus",
            provider="anthropic",
            concrete_model="claude-opus-4-7",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        Preset(
            name="claude-opus-4-7",
            provider="anthropic",
            concrete_model="claude-opus-4-7",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        Preset(
            name="claude-sonnet",
            provider="anthropic",
            concrete_model="claude-sonnet-4-6",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        Preset(
            name="claude-sonnet-4-6",
            provider="anthropic",
            concrete_model="claude-sonnet-4-6",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        Preset(
            name="claude-haiku",
            provider="anthropic",
            concrete_model="claude-haiku-4-5",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        Preset(
            name="claude-haiku-4-5",
            provider="anthropic",
            concrete_model="claude-haiku-4-5",
            api_key_ref="env://ANTHROPIC_API_KEY",
        ),
        # ----- OpenAI / GPT family -----
        Preset(
            name="openai",
            provider="openai",
            concrete_model="gpt-5.5",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt",
            provider="openai",
            concrete_model="gpt-5.5",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        # Same `-` for `.` substitution as the gemini-2-5 family above.
        # `concrete_model` keeps the dotted form
        # since that's what the OpenAI SDK passes upstream.
        Preset(
            name="gpt-5-5",
            provider="openai",
            concrete_model="gpt-5.5",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-5-pro",
            provider="openai",
            concrete_model="gpt-5.5-pro",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-4",
            provider="openai",
            concrete_model="gpt-5.4",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-4-mini",
            provider="openai",
            concrete_model="gpt-5.4-mini",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-4-nano",
            provider="openai",
            concrete_model="gpt-5.4-nano",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5",
            provider="openai",
            concrete_model="gpt-5",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-mini",
            provider="openai",
            concrete_model="gpt-5-mini",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-5-nano",
            provider="openai",
            concrete_model="gpt-5-nano",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="gpt-4-1",
            provider="openai",
            concrete_model="gpt-4.1",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        # ----- OpenAI embeddings (separate adapter) -----
        Preset(
            name="text-embedding-3-small",
            provider="openai-embeddings",
            concrete_model="text-embedding-3-small",
            api_key_ref="env://OPENAI_API_KEY",
        ),
        Preset(
            name="text-embedding-3-large",
            provider="openai-embeddings",
            concrete_model="text-embedding-3-large",
            api_key_ref="env://OPENAI_API_KEY",
        ),
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_LEVEL_PATTERN = re.compile(r"^(\*|admin|service|tier[0-9]+)$")


def is_valid_preset_name(name: str) -> bool:
    """DNS-friendly slug grammar enforced both client-side (admin
    form) and DB-side (CHECK constraint)."""
    return bool(_NAME_PATTERN.match(name))


def is_valid_min_user_level(level: str) -> bool:
    """Accept the same grammar the ACL evaluator uses for rule
    user_level fields."""
    return bool(_LEVEL_PATTERN.match(level))


SUPPORTED_PROVIDERS = (
    "gemini",
    "anthropic",
    "openai",
    "openai-embeddings",
    # OpenAI-compatible local servers (vLLM, LM Studio, llama.cpp-server,
    # Ollama OpenAI-mode, text-generation-inference, etc.). Two flavours
    # mirror the hosted split: chat completions + embeddings.
    "openai-compatible",
    "openai-compatible-embeddings",
)

# Providers whose adapter requires a `base_url` on the preset.
PROVIDERS_REQUIRING_BASE_URL = frozenset({
    "openai-compatible",
    "openai-compatible-embeddings",
})


def is_valid_provider(provider: str) -> bool:
    return provider in SUPPORTED_PROVIDERS


# ---------------------------------------------------------------------------
# Field bounds — single source of truth for the per-field range checks.
#
# These constants are consumed by both the admin API (which raises
# HTTPException 400 with API-friendly messages) and the admin webUI
# form helper (which returns user-friendly strings to render in the
# form). Without a shared definition, the bounds drifted: 0..10 here
# but 0..15 there, etc. — silent inconsistencies that surfaced only
# when a request slipped past the form but failed at the API.
# ---------------------------------------------------------------------------


# Sampling temperature (OpenAI / Anthropic / Gemini all share 0..2).
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0

# `max_tokens` must be a positive integer; the upstream rejects 0
# everywhere. No upper bound — model-specific (e.g. 8K vs 200K).
MAX_TOKENS_MIN = 1

# Retry attempts on a single preset before walking to fallback. Cap
# at 10 to bound worst-case latency (10 retries × ~30s = 5 min).
MAX_RETRIES_MIN = 0
MAX_RETRIES_MAX = 10


def temperature_in_range(value: float) -> bool:
    return TEMPERATURE_MIN <= value <= TEMPERATURE_MAX


def max_tokens_in_range(value: int) -> bool:
    return value >= MAX_TOKENS_MIN


def max_retries_in_range(value: int) -> bool:
    return MAX_RETRIES_MIN <= value <= MAX_RETRIES_MAX


def is_valid_base_url_scheme(url: str) -> bool:
    """Cheap up-front check — full SSRF validation lives in
    `bp_router.url_validation.validate_base_url`. This one only
    catches obvious typos like `ftp://...` so the form / API can
    surface a quick rejection without invoking the full validator."""
    return url.startswith(("http://", "https://"))


def provider_requires_base_url(provider: str) -> bool:
    return provider in PROVIDERS_REQUIRING_BASE_URL
