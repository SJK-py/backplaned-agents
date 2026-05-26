"""`peek_user_level_cached` emits a Prometheus counter per outcome.

R4 second-pass review (low) noted that `_user_level_cache` is
LRU-by-resolve (`resolve_user_level` moves entries to end on
hit) but NOT LRU-by-peek (`peek_user_level_cached` deliberately
doesn't touch). Peek-heavy users — admin-UI sessions that never
make LLM calls — get no LRU touch and can fall out of the cache
while inactive resolve-heavy callers push them aside.

Real risk depends on workload mix. R5 fix adds a counter so
operators can graph the miss rate; a sustained non-zero
`outcome=miss` against admin-UI traffic signals the cap or LRU
policy needs adjusting.

Four outcomes pinned:
  - `hit`     — cache had a fresh entry
  - `miss`    — no entry at all
  - `expired` — entry present but past expiry
  - `no_user` — None / empty user_id input
"""

from __future__ import annotations

import inspect

import pytest


def test_counter_registered_with_outcome_label() -> None:
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import user_level_cache_peek_total

    assert (
        user_level_cache_peek_total._name  # type: ignore[attr-defined]
        == "router_user_level_cache_peek"
    )
    # Each outcome label must work.
    for outcome in ("hit", "miss", "expired", "no_user"):
        user_level_cache_peek_total.labels(outcome=outcome)


def test_peek_emits_no_user_for_falsy_input() -> None:
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import user_level_cache_peek_total
    from tests.conftest import make_llm_service

    svc = make_llm_service()
    before = user_level_cache_peek_total.labels(
        outcome="no_user"
    )._value.get()  # type: ignore[attr-defined]
    svc.peek_user_level_cached(None)
    svc.peek_user_level_cached("")
    after = user_level_cache_peek_total.labels(
        outcome="no_user"
    )._value.get()  # type: ignore[attr-defined]
    assert after - before == 2


def test_peek_emits_miss_for_unknown_user() -> None:
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    from bp_router.observability.metrics import user_level_cache_peek_total
    from tests.conftest import make_llm_service

    svc = make_llm_service()
    before = user_level_cache_peek_total.labels(
        outcome="miss"
    )._value.get()  # type: ignore[attr-defined]
    svc.peek_user_level_cached("usr_unknown")
    after = user_level_cache_peek_total.labels(
        outcome="miss"
    )._value.get()  # type: ignore[attr-defined]
    assert after - before == 1


def test_peek_emits_hit_for_fresh_entry() -> None:
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    import time

    from bp_router.llm.service import _UserLevelCacheEntry
    from bp_router.observability.metrics import user_level_cache_peek_total
    from tests.conftest import make_llm_service

    svc = make_llm_service()
    # Plant a fresh entry directly.
    svc._user_level_cache["usr_x"] = _UserLevelCacheEntry(
        level="admin",
        expires_at=time.monotonic() + 600,
    )

    before = user_level_cache_peek_total.labels(
        outcome="hit"
    )._value.get()  # type: ignore[attr-defined]
    out = svc.peek_user_level_cached("usr_x")
    after = user_level_cache_peek_total.labels(
        outcome="hit"
    )._value.get()  # type: ignore[attr-defined]
    assert out == "admin"
    assert after - before == 1


def test_peek_emits_expired_for_stale_entry() -> None:
    pytest.importorskip("asyncpg")
    pytest.importorskip("prometheus_client")
    import time

    from bp_router.llm.service import _UserLevelCacheEntry
    from bp_router.observability.metrics import user_level_cache_peek_total
    from tests.conftest import make_llm_service

    svc = make_llm_service()
    # Plant a past-expiry entry.
    svc._user_level_cache["usr_y"] = _UserLevelCacheEntry(
        level="tier3",
        expires_at=time.monotonic() - 1,
    )

    before = user_level_cache_peek_total.labels(
        outcome="expired"
    )._value.get()  # type: ignore[attr-defined]
    out = svc.peek_user_level_cached("usr_y")
    after = user_level_cache_peek_total.labels(
        outcome="expired"
    )._value.get()  # type: ignore[attr-defined]
    assert out is None
    assert after - before == 1


def test_peek_metric_emits_on_every_outcome_branch() -> None:
    """Source pin: the metric increment lives in a `finally:` so
    EVERY return path emits exactly once. A future refactor that
    moves the increment inline (per branch) is harder to verify
    completeness — pin the finally pattern."""
    pytest.importorskip("asyncpg")
    from bp_router.llm import service

    src = inspect.getsource(service.LlmService.peek_user_level_cached)
    assert "finally:" in src
    assert "user_level_cache_peek_total.labels(outcome=outcome).inc()" in src
