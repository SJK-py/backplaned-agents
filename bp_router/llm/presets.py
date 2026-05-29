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
   the service seeds it from `default_presets()`, which loads a JSONC
   catalogue (`presets_catalog.jsonc`, or the operator's
   `Settings.llm_preset_catalog_path`). After that, presets live in the
   `llm_presets` DB table and are admin-managed via the webUI. Keeping the
   catalogue in a commentable file makes it easy to maintain as models change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

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


# The bundled catalogue ships beside this module. An operator can override
# the path (`Settings.llm_preset_catalog_path`) to keep their model list in a
# commentable file outside the package — see `default_presets`.
BUNDLED_CATALOG_PATH = Path(__file__).with_name("presets_catalog.jsonc")

# The `Preset` fields a catalogue entry may set (keys outside this set are a
# typo → load fails loud rather than silently ignoring them).
_PRESET_FIELD_NAMES = frozenset(f.name for f in fields(Preset))


def strip_jsonc_comments(text: str) -> str:
    """Strip `//` line comments and `/* ... */` block comments from JSONC,
    leaving everything inside string literals untouched (so values like
    `env://KEY` or `https://host` survive) and preserving newlines (so a
    later `json` parse error still points at the right line). Trailing
    commas are NOT handled — the result must be valid JSON."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # keep the escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue  # leaves the newline for the next iteration
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")  # keep block-comment newlines for line nums
                i += 1
            i += 2  # consume the closing */
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_catalog(path: str | Path | None = None) -> list[Preset]:
    """Parse a JSONC preset catalogue (a top-level array of preset objects)
    into `Preset`s. `path=None` loads the bundled catalogue. An unknown key
    or a missing required field surfaces as a `ValueError` so a malformed
    catalogue fails loud at startup rather than serving a broken map."""
    src = Path(path) if path is not None else BUNDLED_CATALOG_PATH
    raw = src.read_text(encoding="utf-8")
    try:
        data = json.loads(strip_jsonc_comments(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"preset catalogue {src} is not valid JSON(C): {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"preset catalogue {src} must be a JSON array of preset objects"
        )
    presets: list[Preset] = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"preset catalogue {src} entry #{idx} is not an object")
        unknown = set(entry) - _PRESET_FIELD_NAMES
        if unknown:
            raise ValueError(
                f"preset catalogue {src} entry #{idx} "
                f"({entry.get('name', '?')!r}) has unknown keys: {sorted(unknown)}"
            )
        try:
            presets.append(Preset(**entry))
        except TypeError as exc:  # missing a required field
            raise ValueError(
                f"preset catalogue {src} entry #{idx} "
                f"({entry.get('name', '?')!r}): {exc}"
            ) from exc
    return presets


@lru_cache(maxsize=8)
def _load_catalog_cached(path: str | None) -> tuple[Preset, ...]:
    return tuple(load_catalog(path))


def default_presets(path: str | Path | None = None) -> list[Preset]:
    """Built-in presets seeded into an empty `llm_presets` table on first
    startup (and the in-memory fallback before the DB load). Loaded from a
    JSONC catalogue — the bundled `presets_catalog.jsonc` when `path` is
    None, else the operator-supplied file. The result is cached per path.

    Default `min_user_level="*"` (any tier) preserves the prior no-gate
    behaviour. Operators tighten via the admin webUI after seeding, or by
    editing the catalogue before first boot.
    """
    return list(_load_catalog_cached(str(path) if path is not None else None))


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
