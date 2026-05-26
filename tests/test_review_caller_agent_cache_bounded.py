"""`caller_agent_cache` is a bounded LRU, no longer unbounded dict.

R8 fourth-pass review (HIGH, flagged independently by both
tasks.py and Performance reviewers): pre-R8 `state.caller_agent_cache`
was a plain `dict` with no size cap. The terminal-task eviction
(`_notify_task_terminal`, R1 PR #124) only fires on the SAME
worker that admitted the task. Multi-worker deployments where
a task terminates on a different worker leak the cache entry
forever — proportional to cross-worker traffic, bounded only
by the universe of distinct task_ids.

R8 fix:
  - `bp_router/lru_cache.py:BoundedLRUDict` provides minimal
    LRU eviction past `maxsize`. In-house rather than adding
    cachetools as a dep.
  - `Settings.caller_agent_cache_max` (default 10,000) caps
    RSS at ~500 KiB.
  - `state.caller_agent_cache` is now an instance of this class,
    not a `dict`. Drop-in interface (subscript, `pop`, `in`,
    `get`) so callers don't change.
"""

from __future__ import annotations

import pytest


def test_bounded_lru_dict_evicts_oldest_at_cap() -> None:
    pytest.importorskip("pydantic")
    from bp_router.lru_cache import BoundedLRUDict

    cache = BoundedLRUDict(maxsize=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    cache["d"] = 4  # → evicts "a"

    assert "a" not in cache
    assert "b" in cache and "c" in cache and "d" in cache
    assert len(cache) == 3


def test_bounded_lru_dict_get_touches_entry() -> None:
    """A read counts as a touch — keeps the entry hot past
    subsequent insertions."""
    pytest.importorskip("pydantic")
    from bp_router.lru_cache import BoundedLRUDict

    cache = BoundedLRUDict(maxsize=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    # Touch "a".
    assert cache["a"] == 1
    # New insert should evict "b" (least-recently-touched), not "a".
    cache["d"] = 4
    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache
    assert "d" in cache


def test_bounded_lru_dict_pop_works() -> None:
    """The terminal-task callback uses `cache.pop(task_id, None)`.
    Pin the pop API matches the dict interface."""
    pytest.importorskip("pydantic")
    from bp_router.lru_cache import BoundedLRUDict

    cache = BoundedLRUDict(maxsize=10)
    cache["x"] = 99
    out = cache.pop("x", None)
    assert out == 99
    assert "x" not in cache
    # Default works on miss.
    assert cache.pop("missing", "default") == "default"


def test_bounded_lru_dict_overwrites_in_place() -> None:
    """Setting an existing key updates value + touches; does NOT
    count against the cap as a new insert."""
    pytest.importorskip("pydantic")
    from bp_router.lru_cache import BoundedLRUDict

    cache = BoundedLRUDict(maxsize=2)
    cache["a"] = 1
    cache["b"] = 2
    cache["a"] = 11  # overwrite — touches "a", no eviction
    assert "a" in cache
    assert "b" in cache
    assert cache["a"] == 11


def test_bounded_lru_dict_maxsize_validated() -> None:
    pytest.importorskip("pydantic")
    from bp_router.lru_cache import BoundedLRUDict

    with pytest.raises(ValueError):
        BoundedLRUDict(maxsize=0)
    with pytest.raises(ValueError):
        BoundedLRUDict(maxsize=-5)


def test_settings_field_present_with_sane_default() -> None:
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "caller_agent_cache_max" in fields

    s = Settings(
        db_url="postgresql://x:x@localhost/x",
        public_url="https://example.com",
        jwt_secret="x" * 32,
        admin_session_secret="y" * 32,
    )
    assert s.caller_agent_cache_max >= 100


def test_lifespan_uses_bounded_dict_for_caller_agent_cache() -> None:
    """Source pin: lifespan boots the cache as `BoundedLRUDict(
    maxsize=settings.caller_agent_cache_max)` rather than a
    plain dict."""
    pytest.importorskip("fastapi")
    import inspect

    from bp_router import app

    src = inspect.getsource(app.lifespan)
    assert "BoundedLRUDict" in src
    assert "caller_agent_cache_max" in src
