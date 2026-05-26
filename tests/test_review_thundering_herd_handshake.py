"""R10 HIGH: thundering-herd fleet reconnect.

A whole fleet reconnecting in lockstep (router restart, network
blip) used to issue, *per handshake*: a `get_agent`, a full
`list_agents` table scan, and an `update_agent_last_seen`. With N
concurrent handshakes that is N pool checkouts and an O(N) scan
each — O(N²) DB + deserialise work that drains the default 10-conn
pool and stalls every other router DB op for the duration.

The fix has two parts, both pinned here:

  1. A global `state.ws_handshake_semaphore` (size <
     `db_pool_max_size`) wrapping the DB-heavy section so a
     reconnect storm can never consume the whole pool.
  2. A short-TTL single-flight `_CatalogCache` fronting
     `list_agents` so the storm collapses to ~1 shared scan per
     TTL instead of one scan per handshake.

The structural pins (AST) guard against a future refactor
regressing the O(N) scan back inside the handshake or moving the
DB section outside the semaphore.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import textwrap

import pytest

# ---------------------------------------------------------------------------
# fake pool
# ---------------------------------------------------------------------------


class _FakePool:
    """`acquire()` → async-CM yielding a throwaway conn. Records the
    number of checkouts so a test can prove the cache collapses N
    concurrent handshakes into one DB round-trip."""

    def __init__(self) -> None:
        self.checkouts = 0

    def acquire(self):  # type: ignore[no-untyped-def]
        pool = self

        @contextlib.asynccontextmanager
        async def _cm():  # type: ignore[no-untyped-def]
            pool.checkouts += 1
            yield object()

        return _cm()


# ---------------------------------------------------------------------------
# _CatalogCache — single-flight + TTL
# ---------------------------------------------------------------------------


def test_catalog_cache_single_flights_concurrent_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N coroutines hitting a cold cache concurrently must produce
    exactly ONE `list_agents` DB call (the single-flight collapse),
    and all must receive the same list object."""
    from bp_router import ws_hub

    calls = 0
    sentinel = [object(), object()]

    async def _slow_list_agents(conn):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        # Long enough that every gathered task is parked on the
        # cache lock before the first DB call returns.
        await asyncio.sleep(0.02)
        return sentinel

    monkeypatch.setattr(ws_hub.queries, "list_agents", _slow_list_agents)

    async def _run() -> None:
        cache = ws_hub._CatalogCache(ttl_s=60.0)
        pool = _FakePool()
        results = await asyncio.gather(
            *[cache.get(pool) for _ in range(25)]
        )
        assert calls == 1, f"expected single-flight, got {calls} DB calls"
        assert pool.checkouts == 1
        assert all(r is sentinel for r in results)

    asyncio.run(_run())


def test_catalog_cache_warm_hit_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call inside the TTL window is served from cache —
    no extra DB checkout."""
    from bp_router import ws_hub

    calls = 0

    async def _list_agents(conn):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return ["row"]

    monkeypatch.setattr(ws_hub.queries, "list_agents", _list_agents)

    async def _run() -> None:
        cache = ws_hub._CatalogCache(ttl_s=60.0)
        pool = _FakePool()
        a = await cache.get(pool)
        b = await cache.get(pool)
        assert a is b
        assert calls == 1
        assert pool.checkouts == 1

    asyncio.run(_run())


def test_catalog_cache_refetches_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the cached entry is past `_expires_at`, the next call
    does a fresh scan (bounded staleness, not stale-forever)."""
    from bp_router import ws_hub

    calls = 0

    async def _list_agents(conn):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return [f"gen{calls}"]

    monkeypatch.setattr(ws_hub.queries, "list_agents", _list_agents)

    async def _run() -> None:
        cache = ws_hub._CatalogCache(ttl_s=60.0)
        pool = _FakePool()
        first = await cache.get(pool)
        # Force expiry without sleeping real time.
        cache._expires_at = 0.0
        second = await cache.get(pool)
        assert calls == 2
        assert first != second

    asyncio.run(_run())


def test_catalog_cache_ttl_zero_disables_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ttl_s <= 0` is the documented kill-switch: every call scans
    fresh (no cache, no lock)."""
    from bp_router import ws_hub

    calls = 0

    async def _list_agents(conn):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(ws_hub.queries, "list_agents", _list_agents)

    async def _run() -> None:
        cache = ws_hub._CatalogCache(ttl_s=0.0)
        pool = _FakePool()
        for _ in range(4):
            await cache.get(pool)
        assert calls == 4
        assert pool.checkouts == 4

    asyncio.run(_run())


def test_catalog_cache_emits_hit_miss_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold call increments `result="miss"`; a subsequent warm call
    increments `result="hit"` — the prod signal that the storm
    mitigation is actually engaging."""
    from bp_router import ws_hub
    from bp_router.observability import metrics

    async def _list_agents(conn):  # type: ignore[no-untyped-def]
        return ["row"]

    monkeypatch.setattr(ws_hub.queries, "list_agents", _list_agents)

    miss = metrics.ws_handshake_catalog_cache_total.labels(result="miss")
    hit = metrics.ws_handshake_catalog_cache_total.labels(result="hit")
    miss0 = miss._value.get()
    hit0 = hit._value.get()

    async def _run() -> None:
        cache = ws_hub._CatalogCache(ttl_s=60.0)
        pool = _FakePool()
        await cache.get(pool)  # miss
        await cache.get(pool)  # hit
        await cache.get(pool)  # hit

    asyncio.run(_run())
    assert miss._value.get() - miss0 == 1
    assert hit._value.get() - hit0 == 2


# ---------------------------------------------------------------------------
# structural pins — semaphore wraps the DB section; no raw list_agents
# ---------------------------------------------------------------------------


def _handshake_ast():  # type: ignore[no-untyped-def]
    from bp_router import ws_hub

    src = textwrap.dedent(inspect.getsource(ws_hub._handshake))
    return ast.parse(src).body[0]


def _semaphore_with_node(fn):  # type: ignore[no-untyped-def]
    """Find the `async with state.ws_handshake_semaphore:` node."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            ctx = item.context_expr
            # Attribute chain ending in `.ws_handshake_semaphore`.
            if (
                isinstance(ctx, ast.Attribute)
                and ctx.attr == "ws_handshake_semaphore"
            ):
                return node
    return None


def test_handshake_db_section_is_inside_the_semaphore() -> None:
    """The get_agent checkout, the `agent_catalog_cache.get` call,
    the `update_agent_last_seen` checkout and the Welcome send must
    all be lexically nested in the `ws_handshake_semaphore` block —
    otherwise a fleet reconnect can still drain the pool."""
    pytest.importorskip("fastapi")
    fn = _handshake_ast()
    sem = _semaphore_with_node(fn)
    assert sem is not None, "no `async with state.ws_handshake_semaphore`"

    inside = set(ast.walk(sem))

    # get_agent + update_agent_last_seen calls present and inside.
    qcalls = {
        n.func.attr: n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"get_agent", "update_agent_last_seen"}
    }
    assert "get_agent" in qcalls and "update_agent_last_seen" in qcalls
    for name, call in qcalls.items():
        assert call in inside, f"{name} not inside the handshake semaphore"

    # The catalog cache call replaces the raw scan and is inside.
    cache_calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "agent_catalog_cache"
    ]
    assert len(cache_calls) == 1, "expected one agent_catalog_cache.get call"
    assert cache_calls[0] in inside


def test_handshake_does_not_scan_list_agents_directly() -> None:
    """`_handshake` must NOT call `queries.list_agents` itself — the
    O(N) scan has to go through the single-flight cache. Guards
    against a refactor reintroducing the per-handshake scan."""
    pytest.importorskip("fastapi")
    fn = _handshake_ast()
    direct = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "list_agents"
    ]
    assert not direct, "list_agents must be reached via the cache, not directly"


# ---------------------------------------------------------------------------
# wiring pin — lifespan constructs + binds both guards
# ---------------------------------------------------------------------------


def test_lifespan_constructs_and_binds_handshake_guards() -> None:
    """`app.lifespan` must build the semaphore from the setting and a
    `_CatalogCache` from the TTL setting, and bind both onto state.
    Source/AST pin (full lifespan needs a live DB/Redis)."""
    from bp_router import app

    src = inspect.getsource(app.lifespan)
    assert "asyncio.Semaphore(" in src
    assert "ws_handshake_max_concurrent" in src
    assert "_CatalogCache(" in src
    assert "ws_handshake_catalog_cache_ttl_s" in src

    tree = ast.parse(textwrap.dedent(src))
    assigned = {
        t.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute)
    }
    assert "ws_handshake_semaphore" in assigned
    assert "agent_catalog_cache" in assigned


# ---------------------------------------------------------------------------
# settings pin — knobs exist, sane bounds, sized under the pool
# ---------------------------------------------------------------------------


def test_handshake_guard_settings_defaults_and_bounds() -> None:
    from bp_router.settings import Settings

    fields = Settings.model_fields
    mc = fields["ws_handshake_max_concurrent"]
    ttl = fields["ws_handshake_catalog_cache_ttl_s"]

    assert mc.default == 8
    assert ttl.default == 5.0

    # The whole point: the concurrency cap must default below the
    # pool size, or a reconnect storm can still drain it.
    assert mc.default < fields["db_pool_max_size"].default

    def _ge(field):  # type: ignore[no-untyped-def]
        return min(
            (m.ge for m in field.metadata if getattr(m, "ge", None) is not None),
            default=None,
        )

    assert _ge(mc) == 1  # at least one handshake may proceed
    assert _ge(ttl) == 0.0  # 0 == cache disabled (documented)
