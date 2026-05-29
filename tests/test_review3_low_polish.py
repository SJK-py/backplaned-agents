"""Tests for the third-pass review low-priority polish bundle.

  - L-1: `ws_unknown_correlation_total{frame_type}` Counter exists
    and increments when `_handle_ack` / `_handle_pong` drop a
    frame whose `ref_correlation_id` isn't in the socket's
    `inflight_correlations`. The drop itself is intentional
    (review item H2); the metric makes the rate visible.
  - L-2: `update_user` and `update_rule` raise `HTTPException(500)`
    on the column-allowlist defence-in-depth path, not bare
    `RuntimeError`. The client gets a clean error envelope and
    operator metrics see a 500-coded response.
  - L-3: `LlmService.peek_user_level_cached(user_id)` returns the
    cached level on a fresh hit without touching the DB; returns
    None on miss/expired so the call site falls through to the
    DB-acquiring `resolve_user_level`. Dispatch consults the peek
    BEFORE acquiring a pool connection.
"""

from __future__ import annotations

import inspect
import time
from collections import OrderedDict

import pytest

# ===========================================================================
# L-1: ws_unknown_correlation_total metric
# ===========================================================================


def test_l1_metric_defined_with_frame_type_label_only() -> None:
    """Counter must exist with `frame_type` as its only label.
    NOT `agent_id` (which would re-introduce the H-5 cardinality
    blowup) — `frame_type` is a small enum, bounded."""
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import ws_unknown_correlation_total

    label_names = list(
        ws_unknown_correlation_total._labelnames  # type: ignore[attr-defined]
    )
    assert label_names == ["frame_type"], (
        f"review3-L1: ws_unknown_correlation_total should label "
        f"['frame_type'] only; got {label_names}"
    )


def test_l1_handle_ack_increments_metric_on_unknown_correlation() -> None:
    """Source pin: `_handle_ack` must increment the counter when
    the membership check fails."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_ack)
    assert "ws_unknown_correlation_total" in src
    assert 'frame_type="Ack"' in src


def test_l1_handle_pong_increments_metric_on_unknown_correlation() -> None:
    """Source pin: `_handle_pong` must also increment with
    `frame_type="Pong"`."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._handle_pong)
    assert "ws_unknown_correlation_total" in src
    assert 'frame_type="Pong"' in src


# ===========================================================================
# L-2: column-allowlist defence-in-depth raises HTTPException(500)
# ===========================================================================


def test_l2_update_user_raises_http_exception_not_runtime_error() -> None:
    """`update_user`'s allowlist-violation branch must raise
    `HTTPException(status_code=500, ...)` — NOT bare `RuntimeError`.
    The client gets a structured error envelope; ops metrics see a
    500-coded response rather than an opaque stack trace."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_user)
    # The structured 500 must be present.
    assert "HTTPException(" in src
    assert "status_code=500" in src
    assert "user column allowlist drift" in src
    # The bare RuntimeError must be GONE (matching the old
    # `raise RuntimeError(...)` shape would catch a regression
    # that re-introduces the bare-exception form).
    assert "raise RuntimeError(" not in src, (
        "review3-L2 regression: update_user raising bare RuntimeError"
    )


def test_l2_update_rule_raises_http_exception_not_runtime_error() -> None:
    """Same L-2 contract for `update_rule`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_rule)
    assert "HTTPException(" in src
    assert "status_code=500" in src
    assert "rule column allowlist drift" in src
    assert "raise RuntimeError(" not in src


# ===========================================================================
# L-3: peek_user_level_cached short-circuits the pool acquire
# ===========================================================================


def test_l3_peek_method_exists_and_is_sync() -> None:
    """`peek_user_level_cached` must exist on `LlmService` and be
    synchronous (no `await` for in-memory cache lookup)."""
    from bp_router.llm.service import LlmService

    assert hasattr(LlmService, "peek_user_level_cached")
    fn = LlmService.peek_user_level_cached
    # NOT a coroutine — the whole point is to skip awaitable work
    # on a cache hit.
    assert not inspect.iscoroutinefunction(fn)
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["self", "user_id"]


def test_l3_peek_returns_level_on_fresh_hit() -> None:
    """A fresh cache entry yields its level immediately, no DB."""
    from bp_router.llm.service import LlmService, _UserLevelCacheEntry

    svc = LlmService.__new__(LlmService)
    svc._user_level_cache = OrderedDict()
    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier2", expires_at=time.monotonic() + 60.0
    )

    assert svc.peek_user_level_cached("u1") == "tier2"


def test_l3_peek_returns_none_on_expired_entry() -> None:
    """An expired cache entry must NOT be returned — caller must
    fall through to the DB-acquiring `resolve_user_level` path."""
    from bp_router.llm.service import LlmService, _UserLevelCacheEntry

    svc = LlmService.__new__(LlmService)
    svc._user_level_cache = OrderedDict()
    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier2", expires_at=time.monotonic() - 10.0  # expired
    )

    assert svc.peek_user_level_cached("u1") is None


def test_l3_peek_returns_none_on_missing_user() -> None:
    """No cache entry → None."""
    from bp_router.llm.service import LlmService

    svc = LlmService.__new__(LlmService)
    svc._user_level_cache = OrderedDict()
    assert svc.peek_user_level_cached("ghost") is None


def test_l3_peek_returns_none_on_empty_user_id() -> None:
    """Empty/None user_id → None (mirrors `resolve_user_level`)."""
    from bp_router.llm.service import LlmService

    svc = LlmService.__new__(LlmService)
    svc._user_level_cache = OrderedDict()
    assert svc.peek_user_level_cached(None) is None
    assert svc.peek_user_level_cached("") is None


def test_l3_peek_does_not_lru_touch() -> None:
    """The peek must NOT promote the entry — promotion happens
    on a real `resolve_user_level` call. Pin so a future "make
    peek look more like resolve" change doesn't accidentally
    keep cold entries alive past the cap via metric polling."""
    from bp_router.llm.service import LlmService, _UserLevelCacheEntry

    svc = LlmService.__new__(LlmService)
    svc._user_level_cache = OrderedDict()
    expires = time.monotonic() + 60.0
    svc._user_level_cache["u1"] = _UserLevelCacheEntry(
        level="tier1", expires_at=expires
    )
    svc._user_level_cache["u2"] = _UserLevelCacheEntry(
        level="tier2", expires_at=expires
    )

    # u1 is at the front of the OrderedDict.
    assert next(iter(svc._user_level_cache)) == "u1"
    svc.peek_user_level_cached("u1")
    # Still at the front — peek didn't promote.
    assert next(iter(svc._user_level_cache)) == "u1"


def test_l3_dispatch_tier_lookup_is_task_derived_and_gated() -> None:
    """The tier-gate user-level lookup in `_run_llm_call` must:

      1. be skipped entirely for `*` presets (the hot default path) — the
         pool acquire sits behind the `preset_needs_tier` guard; and
      2. derive the caller's identity from the TASK (`_derive_task_scope`,
         active-executor verified), NOT the agent-asserted `frame.user_id`.

    This supersedes the old `peek_user_level_cached(frame.user_id)`
    optimization, which was the bypass being closed: trusting the asserted
    `frame.user_id` let a low-trust agent satisfy a tier gate as any user.
    """
    pytest.importorskip("fastapi")
    from bp_router import dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    guard_idx = src.find("if preset_needs_tier:")
    derive_idx = src.find("_derive_task_scope(")
    pool_idx = src.find("state.db_pool.acquire")
    assert guard_idx > 0, "tier lookup must be gated on preset_needs_tier"
    assert derive_idx > 0, "tier identity must come from _derive_task_scope"
    # The pool acquire (and the derive) sit AFTER the preset_needs_tier guard.
    assert guard_idx < pool_idx and guard_idx < derive_idx
    # The gate must NOT resolve the level from the agent-asserted user_id.
    assert "resolve_user_level(conn, frame.user_id)" not in src
    assert "peek_user_level_cached" not in src


def test_l3_resolve_user_level_unchanged_for_miss_path() -> None:
    """Sanity: `resolve_user_level` itself didn't change shape —
    same conn-required signature, same fall-through-to-DB
    behaviour. This guards against a refactor that accidentally
    breaks the cold-cache path."""
    from bp_router.llm.service import LlmService

    sig = inspect.signature(LlmService.resolve_user_level)
    assert list(sig.parameters) == ["self", "conn", "user_id"]
    assert inspect.iscoroutinefunction(LlmService.resolve_user_level)
