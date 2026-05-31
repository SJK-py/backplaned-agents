"""bp_router.llm.service — LlmService and provider-neutral types."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bp_protocol.frames import RETRIABLE_LLM_CODES
from bp_router.llm.presets import (
    Preset,
    PresetCycleError,
    PresetNotAllowedError,
    PresetUnknownError,
    ResolvedCallParams,
    default_presets_with_overlay,
    detect_fallback_cycles,
    resolve_call_params,
    user_level_satisfies,
    walk_fallback_chain,
)
from bp_router.llm.retry_classification import (
    LlmUpstreamError,
    RetryHint,
    StreamInterrupted,
    compute_backoff,
    safe_classify,
)

if TYPE_CHECKING:
    import asyncpg

    from bp_router.llm.providers.base import ProviderAdapter
    from bp_router.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider-neutral types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    # Gemini 3 returns a thought_signature on the FIRST tool call of any
    # response that contains function calls. Subsequent parallel calls
    # in the same response carry no signature. Round-tripping the
    # signature back is mandatory on the next turn — omitting it
    # produces a 400 from the upstream. Stored as base64 of the raw
    # encrypted bytes the provider returns.
    thought_signature: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Gemini reports thinking tokens separately from output tokens for
    # billing — surfaced via `usage_metadata.thoughts_token_count`.
    thoughts_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_microusd: int = 0


@dataclass
class LlmResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"] = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    # When `include_thoughts=True`, Gemini emits a separate part with
    # `thought=True`. We concatenate those parts into thought_summary
    # so agents can surface the model's reasoning trace. Anthropic's
    # `thinking` blocks (with display=summarized) feed the same field.
    thought_summary: str | None = None
    # Signature attached to the last text part on Gemini 3 (and to the
    # first part of any 2.5 response with thinking + function calling).
    # Optional to round-trip for non-tool turns, but recommended to keep
    # reasoning quality consistent across turns.
    thought_signature: str | None = None
    # Provider-shaped reasoning blocks that MUST be passed back
    # unchanged on the next assistant turn during tool use.
    #
    #   - **Anthropic**: list of `{"type": "thinking", "thinking": ...,
    #     "signature": ...}` and `{"type": "redacted_thinking",
    #     "data": ...}` blocks. Returning them is mandatory for
    #     extended-thinking + tool-use multi-turn loops; dropping them
    #     produces a 400 from the upstream.
    #   - **OpenAI**: list of `{"type": "reasoning", "id": ...,
    #     "summary": [...], "encrypted_content": ...}` blocks.
    #     Required round-trip in stateless mode (no
    #     previous_response_id) for context continuity across turns.
    #   - **Gemini**: empty (Gemini ferries reasoning context via
    #     thought_signature on individual parts instead).
    #
    # The SDK helper `Message.assistant_from_response` automatically
    # prepends these blocks to the rebuilt assistant turn so agent
    # code that uses the helper gets the round-trip for free.
    reasoning_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LlmDelta:
    text: str | None = None
    tool_call: ToolCall | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    # Streaming: True when this delta's `text` is a thought summary
    # chunk (`part.thought=True`), False otherwise. Lets agents render
    # thoughts and answer in separate panes.
    thought: bool = False
    # Per the docs FAQ: "during a model response not containing a FC
    # with a streaming request, the model may return the thought
    # signature in a part with an empty text content part. It is
    # advisable to parse the entire request until the finish_reason
    # is returned." We forward that signature on its own delta.
    thought_signature: str | None = None
    # A complete provider-shaped reasoning block emitted at the moment
    # its source content block closes. On Anthropic, this is the
    # `thinking` or `redacted_thinking` block reconstructed from
    # `thinking_delta` + `signature_delta` events. On OpenAI, this is
    # the `reasoning` item assembled at `response.output_item.done`.
    # The dispatch streaming aggregator collects these into
    # `LlmResultFrame.reasoning_blocks` so agents using the
    # final-message API still get the round-trip data.
    reasoning_block: dict[str, Any] | None = None
    # Status hint, populated only by the streaming setup-retry loop.
    # When set, all other content fields
    # MUST be None / False — the wire-frame `LlmDeltaFrame.meta`
    # validator enforces the same. UI clients use this to show a
    # "retrying" spinner during the backoff between setup attempts;
    # SDK clients that don't care can `if delta.meta: continue`.
    meta: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Cross-provider tool-call ID safety
# ---------------------------------------------------------------------------


def _messages_have_tool_call_ids(messages: list[Message]) -> bool:
    """True iff the conversation history carries any tool_call_id —
    either on a `role="tool"` Message or embedded in an assistant
    message's content as a `tool_use`/`function_call`/`tool_use_id`
    block.

    Used by `_call_with_fallback` to refuse cross-provider fallback
    hops when the tool-call IDs were generated by the root provider.
    Sending them through a different provider's API 400s with
    "no such tool_use_id in conversation" (Anthropic) / "unknown
    call_id" (OpenAI Responses) / silent drop (Gemini).
    """
    for m in messages:
        if m.tool_call_id:
            return True
        if isinstance(m.content, list):
            for part in m.content:
                if not isinstance(part, dict):
                    continue
                # Anthropic-style tool_use block
                if part.get("type") == "tool_use" and part.get("id"):
                    return True
                # Neutral function_call shape (Gemini / SDK round-trip)
                fc = part.get("function_call")
                if isinstance(fc, dict) and fc.get("id"):
                    return True
                # OpenAI native function_call item
                if part.get("type") == "function_call" and part.get("call_id"):
                    return True
                # tool_result block — carries a tool_use_id reference
                if part.get("type") == "tool_result" and part.get("tool_use_id"):
                    return True
    return False


# ---------------------------------------------------------------------------
# User-level cache (for tier gating)
# ---------------------------------------------------------------------------


@dataclass
class _UserLevelCacheEntry:
    level: str
    expires_at: float


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LlmService:
    """Router-side LLM facade.

    Holds:
      - in-memory preset map (loaded from `llm_presets` table at
        startup; reloadable on admin edits)
      - per-binding ProviderAdapter instances (lazy)
      - small TTL cache of user_id → user_level for the tier gate

    Hot path:
      1. Resolve preset by name; fall back to legacy alias-style lookup
         if the caller passed a raw model name and no preset matches.
      2. Tier-gate the call against the caller's user level.
      3. Lazy-construct + cache adapter (inline `api_key` if set,
         otherwise `resolve_secret_ref(api_key_ref)`).
      4. Apply preset defaults + call-time overrides.
      5. Delegate. Record metrics + token counts on the way back.

    Retry + fallback (non-streaming only):
      - For `generate(..., stream=False)`, `embed`, and `count_tokens`,
        a failed call retries the same preset up to `preset.max_retries`
        times, then walks `preset.fallback_preset` and tries that one
        too. The walk continues until success, an unsatisfied tier
        gate (silently skipped on fallback targets — see below), or
        the chain runs out (in which case the last error is raised).
      - For `generate(..., stream=True)`, only the originally requested
        preset is attempted. Once we've started yielding deltas there's
        no transparent way to fall back, so streaming bypasses the
        wrapper entirely.

    Tier gate on fallback targets:
      - The user's level is checked against EACH preset in the chain.
        If the user can't access the *originally requested* preset,
        we surface `PresetNotAllowedError` immediately — we never
        upgrade them onto a different preset.
      - If the user can't access a fallback target, we skip it
        silently and try its own fallback, so admins can mix
        permissive presets with restricted-tier ones in the same chain.
    """

    # User-level cache TTL — re-fetch from DB after this many seconds.
    # Short enough that a level demotion propagates quickly, long
    # enough that hot LLM calls don't hammer the DB.
    USER_LEVEL_TTL_S = 60.0

    # Hard cap on the user-level cache. With many distinct user_ids
    # (e.g. a multi-tenant install) the cache would otherwise grow
    # without bound. LRU eviction past this size — least-recently-used
    # entries are popped on insert. 5000 entries × ~100 bytes ≈ 500 KB,
    # negligible. Operators with much higher user counts should bump
    # this and accept the RAM cost.
    USER_LEVEL_CACHE_MAX = 5000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Preset map populated by `load_presets_from_db` at startup.
        # Empty until then; falls back to default_presets() if the DB
        # call hasn't been made yet (test harnesses, in-process tests).
        self._presets: dict[str, Preset] = {
            p.name: p for p in self._seed_presets()
        }
        self._adapters: dict[str, ProviderAdapter] = {}
        # OrderedDict drives the LRU: move-to-end on hit/insert, then
        # popitem(last=False) past the cap.
        self._user_level_cache: OrderedDict[str, _UserLevelCacheEntry] = OrderedDict()

    # ------------------------------------------------------------------
    # Preset management
    # ------------------------------------------------------------------

    def _catalog_path(self) -> str | None:
        """The operator-configured JSONC preset catalogue path, if any
        (`None` → the bundled catalogue). Tolerant of settings stubs that
        predate the field."""
        return getattr(self.settings, "llm_preset_catalog_path", None) or None

    def _overlay_path(self) -> str | None:
        """The operator preset OVERLAY path, if any — merged over the base
        catalogue (custom wins). Tolerant of settings stubs predating it."""
        return getattr(self.settings, "llm_preset_overlay_path", None) or None

    def _seed_presets(self) -> list[Preset]:
        """The presets to seed / fall back to: base catalogue merged with the
        optional operator overlay (custom wins on collision)."""
        return default_presets_with_overlay(
            catalog_path=self._catalog_path(), overlay_path=self._overlay_path()
        )

    async def load_presets_from_db(self, conn: asyncpg.Connection) -> int:
        """Read the `llm_presets` table into the in-memory map.

        On first startup the table is empty: we seed it with
        `default_presets()` so deployments using the old `model="..."`
        kwarg keep working unchanged. Returns the number of presets
        in the resulting in-memory map.

        Fallback cycles raise `PresetCycleError` — surfaced from the
        admin API as 400 on save, and logged + ignored at startup
        (so an existing-but-broken DB doesn't brick the router; the
        old in-memory map continues to serve requests).
        """
        from bp_router.db import queries  # noqa: PLC0415

        rows = await queries.list_llm_presets(conn)
        if not rows:
            # Empty table → seed defaults. Wrap in a SINGLE transaction
            # so the seed is all-or-nothing. Without
            # the transaction, a CHECK-constraint failure on any one
            # preset would commit the rows that succeeded before it
            # and leave the table in a non-empty "partially seeded"
            # state — `list_llm_presets` would return non-empty on
            # the next startup, so the seed branch never runs again,
            # and the operator has to manually `TRUNCATE llm_presets`
            # to recover.
            seeded = self._seed_presets()
            async with conn.transaction():
                for p in seeded:
                    await queries.insert_llm_preset(
                        conn,
                        name=p.name,
                        description=p.description,
                        provider=p.provider,
                        concrete_model=p.concrete_model,
                        api_key_ref=p.api_key_ref,
                        api_key=p.api_key,
                        base_url=p.base_url,
                        min_user_level=p.min_user_level,
                        default_temperature=p.default_temperature,
                        default_max_tokens=p.default_max_tokens,
                        default_provider_options=p.default_provider_options or None,
                        fallback_preset=p.fallback_preset,
                        max_retries=p.max_retries,
                        created_by=None,
                    )
            self._presets = {p.name: p for p in seeded}
            logger.info(
                "llm_presets_seeded",
                extra={"event": "llm_presets_seeded", "count": len(seeded)},
            )
            return len(seeded)

        new_map: dict[str, Preset] = {}
        for row in rows:
            new_map[row.name] = Preset(
                name=row.name,
                description=row.description,
                provider=row.provider,
                concrete_model=row.concrete_model,
                api_key_ref=row.api_key_ref,
                api_key=row.api_key,
                base_url=row.base_url,
                min_user_level=row.min_user_level,
                default_temperature=row.default_temperature,
                default_max_tokens=row.default_max_tokens,
                default_provider_options=dict(row.default_provider_options or {}),
                fallback_preset=row.fallback_preset,
                max_retries=row.max_retries,
            )
        try:
            detect_fallback_cycles(new_map)
        except PresetCycleError as exc:
            logger.error(
                "llm_preset_fallback_cycle",
                extra={
                    "event": "llm_preset_fallback_cycle",
                    "error": str(exc),
                },
            )
            # Keep serving with the old in-memory map; admins can fix
            # via the API and reload.
            return len(self._presets)
        self._presets = new_map
        # Adapter cache may now be stale (e.g., concrete_model changed)
        # so flush; lazy reconstruction is cheap.
        self._adapters.clear()
        return len(new_map)

    def list_presets(self) -> list[Preset]:
        return sorted(self._presets.values(), key=lambda p: p.name)

    def get_preset(self, name: str) -> Preset | None:
        return self._presets.get(name)

    def chain_needs_tier(self, name: str) -> bool:
        """True if the requested preset OR any preset reachable via its
        ``fallback_preset`` chain is tier-gated (``min_user_level != "*"``).

        The dispatch gate uses this to decide whether to resolve the
        caller's trusted ``user_level``. A ``*`` (ungated) preset whose
        fallback chain contains a gated preset STILL needs the level
        resolved: otherwise ``_call_with_fallback`` re-checks the gate per
        hop with ``user_level=None`` and silently skips that gated fallback
        for *every* caller (the documented mixed permissive→restricted
        chain would lose its restricted hop). Unknown preset → False (the
        generate path raises ``PresetUnknownError`` on its own)."""
        if name not in self._presets:
            return False
        return any(
            p.min_user_level != "*"
            for p in walk_fallback_chain(self._presets, name)
        )

    def _register_preset_for_test(self, preset: Preset) -> None:
        """Register / overwrite a preset in the in-memory map.

        Test-only convenience (underscore-prefixed and renamed to
        signal that). Production code mutates via the admin API +
        `load_presets_from_db`. Flushes the matching adapter cache
        entry so the next request reads the new shape."""
        self._presets[preset.name] = preset
        self._adapters.clear()

    # ------------------------------------------------------------------
    # User-level cache (tier gate)
    # ------------------------------------------------------------------

    async def resolve_user_level(
        self,
        conn: asyncpg.Connection | None,
        user_id: str | None,
    ) -> str | None:
        """Look up the user_id's level. TTL-cached; falls through to
        the DB on cache miss / expiry.

        Suspended users return ``None`` so the tier gate denies them
        on every preset except those with `min_user_level="*"`. The
        admin API also calls `invalidate_user_level` whenever
        ``suspended_at`` flips, so the next request re-fetches and
        sees the suspension immediately rather than waiting out the
        TTL.
        """
        if not user_id:
            return None
        now = time.monotonic()
        entry = self._user_level_cache.get(user_id)
        if entry is not None and entry.expires_at > now:
            # LRU touch — move-to-end keeps hot users alive past the cap.
            self._user_level_cache.move_to_end(user_id)
            return entry.level
        if conn is None:
            return entry.level if entry is not None else None
        from bp_router.db import queries  # noqa: PLC0415

        user = await queries.get_user_by_id(conn, user_id)
        if user is None:
            return None
        if user.suspended_at is not None or user.deleted_at is not None:
            # Don't cache the None — admin un-suspend / un-delete
            # must take effect at the next invalidate, not after
            # TTL.
            return None
        self._user_level_cache[user_id] = _UserLevelCacheEntry(
            level=user.level,
            expires_at=now + self.USER_LEVEL_TTL_S,
        )
        self._user_level_cache.move_to_end(user_id)
        # Evict the oldest entries past the cap. `popitem(last=False)`
        # is FIFO when nothing's been moved-to-end, LRU otherwise.
        while len(self._user_level_cache) > self.USER_LEVEL_CACHE_MAX:
            self._user_level_cache.popitem(last=False)
        return user.level

    def peek_user_level_cached(self, user_id: str | None) -> str | None:
        """Return the cached level if there's a FRESH entry; else None.

        Pure in-memory; no DB call. Lets call sites check the cache
        WITHOUT acquiring a DB connection (which they would otherwise
        have to hold across the `resolve_user_level` call just to
        cover the cold-cache case). Pattern:

            level = svc.peek_user_level_cached(user_id)
            if level is None:
                async with pool.acquire() as conn:
                    level = await svc.resolve_user_level(conn, user_id)

        Returns None for: missing user_id, no cache entry, or entry
        past expiry. Does NOT do an LRU touch — the touch happens on
        the next `resolve_user_level` call that exercises the entry.

        Emits a Prometheus counter on every outcome so operators can
        graph cache effectiveness for peek-heavy workloads. R4
        second-pass review noted that peek-heavy users (admin-UI
        sessions that never make LLM calls) get no LRU touch and can
        fall out of the cache while resolve-heavy callers push them
        aside. The metric makes that risk observable.
        """
        outcome: str
        try:
            if not user_id:
                outcome = "no_user"
                return None
            entry = self._user_level_cache.get(user_id)
            if entry is None:
                outcome = "miss"
                return None
            if entry.expires_at <= time.monotonic():
                outcome = "expired"
                return None
            outcome = "hit"
            return entry.level
        finally:
            try:
                from bp_router.observability.metrics import (  # noqa: PLC0415
                    user_level_cache_peek_total,
                )
                user_level_cache_peek_total.labels(outcome=outcome).inc()
            except Exception:  # noqa: BLE001
                pass

    def invalidate_user_level(self, user_id: str) -> None:
        """Drop the cached level for a user. Called when admin level
        changes happen (so a demotion takes effect immediately)."""
        self._user_level_cache.pop(user_id, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: list[Message],
        *,
        preset: str = "default",
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
        user_id: str | None = None,
        user_level: str | None = None,
        task_id: str | None = None,
    ) -> LlmResponse | AsyncIterator[LlmDelta]:
        # Streaming bypasses the fallback CHAIN — once we start
        # yielding deltas we can't transparently switch to a different
        # preset. But the SAME preset CAN be retried before any delta
        # has been yielded (the first-delta failure means the
        # connection / first chunk never reached the agent). The
        # pre-first-delta retry loop wires `LlmDelta(meta=...)`
        # hints during the backoff.
        # Tier gate still applies on the first preset.
        if stream:
            # Capture preset_obj from `_resolve`'s return tuple instead
            # of re-indexing `self._presets[preset]`. The previous form had a TOCTOU:
            # `load_presets_from_db()` swaps `self._presets` and
            # clears `self._adapters` atomically, but a second indexed
            # read against `self._presets` after `_resolve` returned
            # could KeyError (preset removed) or pick up a different
            # Preset object than the adapter was built for. Capturing
            # the tuple binds preset_obj to the snapshot `_resolve`
            # used.
            resolved, adapter, preset_obj = self._resolve(
                preset_name=preset,
                user_level=user_level,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_options=provider_options,
                require_first_tier=True,
            )
            return self._generate_stream_with_setup_retry(
                adapter=adapter,
                preset=preset_obj,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                resolved=resolved,
            )

        async def _attempt(p: Preset) -> tuple[ProviderAdapter, LlmResponse]:
            resolved, adapter, _ = self._resolve_one(
                preset=p,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_options=provider_options,
            )
            result = await adapter.generate(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=resolved.temperature,
                max_tokens=resolved.max_tokens,
                stream=False,
                provider_options=resolved.provider_options,
            )
            return adapter, result

        adapter, used_preset, result = await self._call_with_fallback(
            preset_name=preset,
            user_level=user_level,
            attempt=_attempt,
            has_tool_call_history=_messages_have_tool_call_ids(messages),
        )
        self._record(adapter, used_preset, result, user_id=user_id, task_id=task_id)
        return result

    async def embed(
        self,
        text: str | list[str],
        *,
        preset: str = "text-embedding-3-small",
        user_id: str | None = None,
        user_level: str | None = None,
    ) -> list[list[float]]:
        async def _attempt(p: Preset) -> tuple[ProviderAdapter, list[list[float]]]:
            resolved, adapter, _ = self._resolve_one(
                preset=p,
                temperature=None,
                max_tokens=None,
                provider_options=None,
            )
            # The preset's `default_provider_options` flow through (call-time
            # None doesn't override) — embedding adapters read e.g.
            # `output_dimensionality` / `dimensions` from here.
            return adapter, await adapter.embed(
                text, provider_options=resolved.provider_options
            )

        _, _, vectors = await self._call_with_fallback(
            preset_name=preset,
            user_level=user_level,
            attempt=_attempt,
        )
        return vectors

    async def count_tokens(
        self,
        messages: list[Message],
        *,
        preset: str = "default",
        user_level: str | None = None,
    ) -> int:
        async def _attempt(p: Preset) -> tuple[ProviderAdapter, int]:
            _, adapter, _ = self._resolve_one(
                preset=p,
                temperature=None,
                max_tokens=None,
                provider_options=None,
            )
            return adapter, await adapter.count_tokens(messages)

        _, _, count = await self._call_with_fallback(
            preset_name=preset,
            user_level=user_level,
            attempt=_attempt,
        )
        return count

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve(
        self,
        *,
        preset_name: str,
        user_level: str | None,
        temperature: float | None,
        max_tokens: int | None,
        provider_options: dict[str, Any] | None,
        require_first_tier: bool = True,
    ) -> tuple[ResolvedCallParams, ProviderAdapter, Preset]:
        """Resolve the *originally requested* preset. Validates tier
        gate up front and returns (resolved_params, adapter, preset)."""
        preset = self._presets.get(preset_name)
        if preset is None:
            raise PresetUnknownError(preset_name)
        if require_first_tier and not user_level_satisfies(
            user_level, preset.min_user_level
        ):
            raise PresetNotAllowedError(
                preset_name=preset_name,
                user_level=user_level,
                required=preset.min_user_level,
            )
        return self._resolve_one(
            preset=preset,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
        )

    def _resolve_one(
        self,
        *,
        preset: Preset,
        temperature: float | None,
        max_tokens: int | None,
        provider_options: dict[str, Any] | None,
    ) -> tuple[ResolvedCallParams, ProviderAdapter, Preset]:
        """Build / cache an adapter for a *single* preset, with no
        tier check. Used by both the direct path and the fallback
        walker (which has already done its own tier check)."""
        resolved = resolve_call_params(
            preset,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_options=provider_options,
        )

        # Cache key: provider + model + base_url + which secret. Inline
        # `api_key` gets a stable marker (`inline:<name>`) so two presets
        # with the same provider+model but different inline keys end up
        # in separate adapter instances. `base_url` is part of the key
        # so two openai-compatible presets pointing at different local
        # servers stay isolated.
        #
        # LOAD-BEARING INVARIANT:
        # the `inline:<name>` marker is NOT a hash of the actual key
        # bytes — it's a stable identifier. That's safe TODAY only
        # because `load_presets_from_db()` does an atomic
        # `self._presets = ... ; self._adapters.clear()` whenever
        # presets change (admin rotates `api_key` via PATCH, etc.),
        # so a rotated key never hits a stale cached adapter. Any
        # future optimization that drops or narrows the global
        # `_adapters.clear()` (e.g. a thundering-herd-aware
        # selective eviction) MUST switch this marker to a hash of
        # the resolved secret bytes — otherwise a key rotation
        # would silently keep using the old SDK client.
        secret_marker = (
            f"inline:{preset.name}" if resolved.api_key else resolved.api_key_ref
        )
        base_url_marker = resolved.base_url or "-"
        cache_key = (
            f"{resolved.provider}::{resolved.concrete_model}::"
            f"{base_url_marker}::{secret_marker}"
        )
        adapter = self._adapters.get(cache_key)
        if adapter is None:
            adapter = self._build_adapter(resolved)
            self._adapters[cache_key] = adapter
        return resolved, adapter, preset

    async def _call_with_fallback(
        self,
        *,
        preset_name: str,
        user_level: str | None,
        attempt: Any,
        has_tool_call_history: bool = False,
    ) -> tuple[ProviderAdapter, str, Any]:
        """Walk the fallback chain. Each preset gets `max_retries+1`
        attempts before we move on. Returns (adapter, used_preset_name, result).

        Tier gate semantics:
          - The *first* preset (the one the caller asked for) must
            satisfy the user's level — if not, we surface
            `PresetNotAllowedError` immediately.
          - Fallback targets failing the tier gate are skipped silently
            and we continue to *their* fallback (this lets admins mix
            permissive + restricted-tier presets in a single chain).

        Cross-provider tool-call ID safety (`has_tool_call_history`):
          - When the caller's `messages` carry tool_call_ids from a
            prior turn, those IDs were generated by the WINNING
            provider on that turn. Anthropic uses `toolu_<hex>`, OpenAI
            `call_<hex>`, Gemini integers. If we fall back to a
            different-provider preset, the next API call sends the
            stale IDs back as `tool_result.tool_use_id` /
            `function_call_output.call_id` — and the destination
            provider 400s ("no such tool_use_id in conversation") OR
            silently misroutes the result. Skip fallback targets
            whose provider differs from the ROOT preset's provider
            in that case; same-provider fallback (e.g. `gpt-4o` →
            `gpt-4o-mini`) is still safe. Metric:
            `llm_fallback_skipped_provider_total`.

        On exhaustion of the chain, the last error encountered is
        re-raised. `PresetUnknownError` for the first preset is also
        surfaced immediately rather than swallowed.
        """
        first = self._presets.get(preset_name)
        if first is None:
            raise PresetUnknownError(preset_name)
        if not user_level_satisfies(user_level, first.min_user_level):
            self._inc_metric("llm_tier_gate_denied_total", preset=preset_name)
            raise PresetNotAllowedError(
                preset_name=preset_name,
                user_level=user_level,
                required=first.min_user_level,
            )

        root_provider = first.provider
        chain = walk_fallback_chain(self._presets, preset_name)
        last_error: BaseException | None = None
        last_hint: RetryHint | None = None
        for idx, preset in enumerate(chain):
            # Tier check on fallback targets only — first preset was
            # already gated above.
            if idx > 0 and not user_level_satisfies(user_level, preset.min_user_level):
                self._inc_metric(
                    "llm_fallback_skipped_tier_total", preset=preset.name
                )
                logger.info(
                    "llm_fallback_skipped_tier",
                    extra={
                        "event": "llm_fallback_skipped_tier",
                        "preset": preset.name,
                        "min_user_level": preset.min_user_level,
                        "user_level": user_level,
                    },
                )
                continue
            # Cross-provider safety: skip fallback hops that would
            # change provider while messages carry tool_call_ids
            # from the root provider. See docstring for the failure
            # mode.
            if (
                idx > 0
                and has_tool_call_history
                and preset.provider != root_provider
            ):
                self._inc_metric(
                    "llm_fallback_skipped_provider_total",
                    preset=preset.name,
                    root_provider=root_provider,
                    fallback_provider=preset.provider,
                )
                logger.info(
                    "llm_fallback_skipped_provider",
                    extra={
                        "event": "llm_fallback_skipped_provider",
                        "preset": preset.name,
                        "root_provider": root_provider,
                        "fallback_provider": preset.provider,
                    },
                )
                continue
            # Resolve the adapter up-front so we can call its
            # classifier on failure (the `attempt` callback also
            # builds it; both hits are cache-hits in `_adapters`).
            try:
                _, adapter_for_classify, _ = self._resolve_one(
                    preset=preset,
                    temperature=None,
                    max_tokens=None,
                    provider_options=None,
                )
            except Exception:  # noqa: BLE001
                # Adapter construction itself failed. The `attempt`
                # callback will hit the same error; defer classification
                # to the default hint.
                adapter_for_classify = None
            attempts_for_this_preset = max(0, preset.max_retries) + 1
            for attempt_idx in range(attempts_for_this_preset):
                try:
                    adapter, result = await attempt(preset)
                    self._inc_metric(
                        "llm_fallback_attempts_total",
                        preset=preset.name,
                        outcome="success",
                    )
                    # If we won via a non-root preset, record the
                    # fallback rescue so ops can alert on degraded
                    # primary providers.
                    if idx > 0:
                        self._inc_metric(
                            "llm_fallback_used_total",
                            root_preset=preset_name,
                            winning_preset=preset.name,
                        )
                    return adapter, preset.name, result
                except (PresetUnknownError, PresetNotAllowedError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    hint = safe_classify(adapter_for_classify, exc)
                    last_error = exc
                    last_hint = hint
                    is_last_for_this_preset = (
                        attempt_idx == attempts_for_this_preset - 1
                    )
                    # Distinguish retry-pending from chain-walk-pending
                    # outcomes so per-preset retry pressure is visible.
                    self._inc_metric(
                        "llm_fallback_attempts_total",
                        preset=preset.name,
                        outcome=("failed" if is_last_for_this_preset else "retry"),
                    )
                    logger.warning(
                        "llm_call_failed",
                        extra={
                            "event": "llm_call_failed",
                            "preset": preset.name,
                            "attempt": attempt_idx + 1,
                            "max_attempts": attempts_for_this_preset,
                            "error_message": str(exc),
                            "error_code": hint.code,
                            "upstream_class": hint.upstream_class,
                            "fallback_pending": (
                                preset.fallback_preset is not None
                                and is_last_for_this_preset
                            ),
                        },
                    )
                    # Sleep before the next attempt on the SAME preset.
                    # No sleep when we're about to walk to fallback —
                    # that's a code-path switch, not a transient retry.
                    if not is_last_for_this_preset:
                        wait_s = compute_backoff(
                            attempt_idx,
                            retry_after_seconds=hint.retry_after_seconds,
                        )
                        await asyncio.sleep(wait_s)
                    continue
        # Chain exhausted — surface the last classified hint as a
        # typed `LlmUpstreamError`. dispatch._run_llm_call catches
        # this and emits the typed code in `LlmResultFrame.error`.
        if last_error is not None:
            self._inc_metric(
                "llm_fallback_chain_exhausted_total", root_preset=preset_name
            )
            raise LlmUpstreamError(
                hint=last_hint or RetryHint(code="internal_error"),
                message=str(last_error),
            ) from last_error
        # Should never get here: chain non-empty (first preset known)
        # and either succeeded or raised at least once.
        raise PresetUnknownError(preset_name)

    async def _generate_stream_with_setup_retry(
        self,
        *,
        adapter: ProviderAdapter,
        preset: Preset,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        tool_choice: ToolChoice | None,
        resolved: ResolvedCallParams,
    ) -> AsyncIterator[LlmDelta]:
        """Async generator wrapping `adapter.generate(stream=True)` with
        a pre-first-delta retry loop.

        Behaviour matches the design doc §6 + §11.4 pseudocode:

          - For each attempt in `0..preset.max_retries`:
              1. Get an iterator from the adapter.
              2. `await iterator.__anext__()` to trigger first I/O
                 (connection, request, first chunk).
              3. On `StopAsyncIteration`, treat as a successful empty
                 stream — return cleanly.
              4. On `Exception`: classify; if retriable AND attempts
                 remain, yield a `LlmDelta(meta={"kind":
                 "retry_pending", ...})` hint, sleep
                 `compute_backoff(...)`, and retry.
              5. Otherwise, raise (the dispatcher catches and emits
                 the typed code).
          - Once the first delta lands, yield it and every subsequent
            delta straight through. Mid-stream failures raise
            `StreamInterrupted` — NOT retriable, the agent already
            has partial output.

        Streaming bypasses the fallback CHAIN by design (retries the
        SAME preset only); chain-walking across providers mid-stream
        isn't safe.
        """
        max_retries = max(0, preset.max_retries)
        previous_iterator: AsyncIterator[LlmDelta] | None = None

        for attempt_idx in range(max_retries + 1):
            # Best-effort cleanup of the previous attempt's iterator
            # before starting a new one. Async generators backed by
            # `async with stream() as s:` (Anthropic, OpenAI Responses)
            # need `aclose()` to release the SDK-side connection.
            if previous_iterator is not None:
                aclose = getattr(previous_iterator, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001
                        logger.debug("stream aclose failed", exc_info=True)

            iterator = await adapter.generate(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=resolved.temperature,
                max_tokens=resolved.max_tokens,
                stream=True,
                provider_options=resolved.provider_options,
            )
            previous_iterator = iterator

            # Step 2 — try to pull the first delta. This is where the
            # actual SDK-side I/O happens (each adapter's
            # `_generate_stream` defers the connection establishment
            # until iteration begins).
            try:
                first = await iterator.__anext__()
            except StopAsyncIteration:
                # Empty stream — treat as a successful no-op response.
                self._inc_metric(
                    "llm_fallback_attempts_total",
                    preset=preset.name,
                    outcome="success",
                )
                return
            except Exception as exc:  # noqa: BLE001
                hint = safe_classify(adapter, exc)
                attempts_remaining = attempt_idx < max_retries
                # Reuse the protocol-side constant rather than a
                # hardcoded literal — keeps the streaming retry boundary
                # in lockstep with the non-streaming `_call_with_fallback`
                # and the SDK `RetryPolicy.retry_codes` default. Drift
                # between the two would manifest as "this code retries on
                # the unary path but not the streaming path", which is
                # exactly the kind of bug that's hard to debug from logs.
                is_retriable = hint.code in RETRIABLE_LLM_CODES
                if attempts_remaining and is_retriable:
                    wait_s = compute_backoff(
                        attempt_idx,
                        retry_after_seconds=hint.retry_after_seconds,
                    )
                    self._inc_metric(
                        "llm_fallback_attempts_total",
                        preset=preset.name,
                        outcome="setup_retry",
                    )
                    logger.warning(
                        "llm_stream_setup_retry",
                        extra={
                            "event": "llm_stream_setup_retry",
                            "preset": preset.name,
                            "attempt": attempt_idx + 1,
                            "max_attempts": max_retries + 1,
                            "wait_seconds": wait_s,
                            "error_code": hint.code,
                            "upstream_class": hint.upstream_class,
                        },
                    )
                    # Emit the meta delta so UI clients can show a
                    # "retrying" spinner. Mutex with content fields:
                    # text / tool_call / etc. all stay None.
                    yield LlmDelta(meta={
                        "kind": "retry_pending",
                        "attempt": attempt_idx + 1,
                        "max_attempts": max_retries + 1,
                        "retry_after_seconds": wait_s,
                        "reason_code": hint.code,
                    })
                    await asyncio.sleep(wait_s)
                    continue
                # Either non-retriable or out of attempts — surface as
                # a typed upstream error.
                self._inc_metric(
                    "llm_fallback_attempts_total",
                    preset=preset.name,
                    outcome="failed",
                )
                self._inc_metric(
                    "llm_fallback_chain_exhausted_total",
                    root_preset=preset.name,
                )
                raise LlmUpstreamError(
                    hint=hint, message=str(exc)
                ) from exc

            # First delta succeeded — count this attempt as a success
            # and yield through the rest. Mid-stream failures raise
            # StreamInterrupted (not retriable).
            self._inc_metric(
                "llm_fallback_attempts_total",
                preset=preset.name,
                outcome="success",
            )
            yield first
            n_deltas = 1
            try:
                async for d in iterator:
                    yield d
                    n_deltas += 1
            except Exception as exc:  # noqa: BLE001
                hint = safe_classify(adapter, exc)
                logger.warning(
                    "llm_stream_interrupted",
                    extra={
                        "event": "llm_stream_interrupted",
                        "preset": preset.name,
                        "after_n_deltas": n_deltas,
                        "upstream_class": hint.upstream_class,
                    },
                )
                raise StreamInterrupted(
                    message=str(exc),
                    after_n_deltas=n_deltas,
                    upstream_class=hint.upstream_class,
                ) from exc
            return

    @staticmethod
    def _inc_metric(name: str, **labels: str) -> None:
        """Increment a Prometheus counter by name. Wrapped so test
        harnesses without `prometheus_client` (or without the registry
        configured) keep working — the metric module imports lazily
        and any failure is swallowed.

        Lookups happen via getattr each call. That's fine — Prometheus
        Counter handles are themselves dict lookups under the hood, so
        the indirection is in the noise.
        """
        try:
            from bp_router.observability import metrics  # noqa: PLC0415

            counter = getattr(metrics, name, None)
            if counter is None:
                return
            counter.labels(**labels).inc()
        except Exception:  # noqa: BLE001
            logger.debug("llm metric increment failed", exc_info=True)

    def _build_adapter(self, resolved: ResolvedCallParams) -> ProviderAdapter:
        from bp_router.security.secrets import resolve_secret_ref  # noqa: PLC0415

        # Local OpenAI-compatible servers don't always require a key
        # (vLLM defaults are unauthenticated, LM Studio echoes anything).
        # Fall back to the placeholder "EMPTY" — the OpenAI SDK requires
        # a non-empty string. Hosted providers go through the real
        # resolver and explode loudly on missing secrets.
        if resolved.provider in ("openai-compatible", "openai-compatible-embeddings"):
            api_key = (
                resolved.api_key
                or (resolve_secret_ref(resolved.api_key_ref) if resolved.api_key_ref else None)
                or "EMPTY"
            )
        else:
            api_key = resolved.api_key or resolve_secret_ref(resolved.api_key_ref)

        # Hosted providers accept an optional base_url for proxy /
        # regional / enterprise-gateway use cases (Azure OpenAI,
        # Bedrock-fronted Anthropic, Vertex AI / EU Gemini, LiteLLM,
        # Portkey, etc.). When None, the upstream SDK uses its
        # built-in default endpoint.
        if resolved.provider == "gemini":
            from bp_router.llm.providers.gemini import GeminiAdapter  # noqa: PLC0415

            return GeminiAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        if resolved.provider == "anthropic":
            from bp_router.llm.providers.anthropic import AnthropicAdapter  # noqa: PLC0415

            return AnthropicAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        if resolved.provider == "openai":
            from bp_router.llm.providers.openai import OpenAIAdapter  # noqa: PLC0415

            return OpenAIAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        if resolved.provider == "openai-embeddings":
            from bp_router.llm.providers.openai import (  # noqa: PLC0415
                OpenAIEmbeddingsAdapter,
            )

            return OpenAIEmbeddingsAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        if resolved.provider == "openai-compatible":
            from bp_router.llm.providers.openai_compatible import (  # noqa: PLC0415
                OpenAICompatibleAdapter,
            )

            if not resolved.base_url:
                raise ValueError(
                    "openai-compatible preset requires a base_url"
                )
            return OpenAICompatibleAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        if resolved.provider == "openai-compatible-embeddings":
            from bp_router.llm.providers.openai_compatible import (  # noqa: PLC0415
                OpenAICompatibleEmbeddingsAdapter,
            )

            if not resolved.base_url:
                raise ValueError(
                    "openai-compatible-embeddings preset requires a base_url"
                )
            return OpenAICompatibleEmbeddingsAdapter(
                concrete_model=resolved.concrete_model,
                api_key=api_key,
                base_url=resolved.base_url,
            )
        raise NotImplementedError(
            f"provider {resolved.provider!r} adapter not yet wired"
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def record_streaming_outcome(
        self,
        *,
        preset_name: str,
        usage: TokenUsage,
        finish_reason: str,
        user_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Public hook for the streaming dispatch path to record
        telemetry after an LlmRequest stream completes. The unary
        path calls `_record` directly (it has an `LlmResponse` in
        hand); the streaming path aggregates per-delta `usage`
        objects across the stream and calls this to land the same
        Prometheus counters.

        Without this, every streaming LLM call was invisible to
        `router_llm_calls_total`, `router_llm_tokens_total`, and
        `router_llm_cost_microusd_total` — a silent telemetry hole
        for what is the dominant call shape in any chat-style
        agent suite.
        """
        try:
            preset_obj = self._presets.get(preset_name)
            if preset_obj is None:
                # Preset was removed mid-stream (admin CRUD); skip
                # telemetry rather than crash. Counter is per-preset
                # so there's no useful "unknown" bucket to fall
                # back to.
                return
            # `_resolve_one` reuses the adapter cache — no fresh SDK
            # client construction on the telemetry path.
            _resolved, adapter, _preset = self._resolve_one(
                preset=preset_obj,
                temperature=None,
                max_tokens=None,
                provider_options=None,
            )
            synthetic = LlmResponse(
                text="",
                finish_reason=finish_reason,  # type: ignore[arg-type]
                usage=usage,
            )
            self._record(
                adapter, preset_name, synthetic,
                user_id=user_id, task_id=task_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "streaming telemetry record failed", exc_info=True
            )

    def _record(
        self,
        adapter: ProviderAdapter,
        preset: str,
        result: Any,
        *,
        user_id: str | None,
        task_id: str | None,
    ) -> None:
        if not isinstance(result, LlmResponse):
            return
        try:
            from bp_router.observability.metrics import (  # noqa: PLC0415
                llm_calls_total,
                llm_cost_microusd_total,
                llm_tokens_total,
            )

            llm_calls_total.labels(
                model=preset,
                provider=adapter.provider_name,
                status=result.finish_reason,
            ).inc()
            llm_tokens_total.labels(model=preset, direction="in").inc(
                result.usage.input_tokens
            )
            llm_tokens_total.labels(model=preset, direction="out").inc(
                result.usage.output_tokens
            )
            if result.usage.cost_microusd:
                llm_cost_microusd_total.labels(model=preset).inc(
                    result.usage.cost_microusd
                )
        except Exception:  # noqa: BLE001
            logger.debug("llm metric record failed", exc_info=True)
