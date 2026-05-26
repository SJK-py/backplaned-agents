"""R10 HIGH: boot crashloop is undiagnosable.

A misconfig or unreachable dependency at startup used to exit the
process with a raw, context-free traceback (e.g. an asyncpg
`OSError` ten frames deep) and *no* indication of which boot phase
was running. Under a process supervisor that is an opaque
crashloop: a 2-minute "Postgres DNS is wrong" fix becomes a
multi-person triage.

Two fixes, both pinned here:

  1. Every boot phase sets `current_phase` via `_boot_phase` (which
     also logs a breadcrumb). One `except BaseException` at the
     bottom logs a definitive `boot_failed` line *naming the
     phase*, runs best-effort `_boot_cleanup` (so a crashloop
     doesn't leak a pool/redis handle per restart), then re-raises
     — a fatal misconfig SHOULD still exit, just legibly.

  2. Redis configured-but-unreachable AT BOOT no longer crashes the
     whole router. The running router already tolerates Redis
     flakes everywhere (per-process fallback + `redis_health=0`);
     boot now matches that contract instead of being the one place
     that fails closed.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# _boot_phase — breadcrumb + identity
# ---------------------------------------------------------------------------


def test_boot_phase_logs_and_returns_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from bp_router.app import _boot_phase

    with caplog.at_level(logging.INFO):
        out = _boot_phase("database_pool")

    assert out == "database_pool"
    rec = [r for r in caplog.records if getattr(r, "event", None) == "boot_phase"]
    assert len(rec) == 1
    assert rec[0].phase == "database_pool"


# ---------------------------------------------------------------------------
# _boot_cleanup — best-effort partial-boot teardown
# ---------------------------------------------------------------------------


def test_boot_cleanup_closes_opened_resources() -> None:
    from bp_router.app import AppState, _boot_cleanup

    state = AppState()
    pool = MagicMock()
    pool.close = AsyncMock()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    state.db_pool = pool
    state.redis = redis

    asyncio.run(_boot_cleanup(state))

    pool.close.assert_awaited_once()
    redis.aclose.assert_awaited_once()


def test_boot_cleanup_tolerates_unset_attrs() -> None:
    """A boot that died in phase 1 never set `db_pool`/`redis`.
    Cleanup must not raise on the missing attributes (the whole
    point is to be safe on the failure path)."""
    from bp_router.app import AppState, _boot_cleanup

    state = AppState()  # nothing set
    asyncio.run(_boot_cleanup(state))  # must not raise


def test_boot_cleanup_swallows_close_errors() -> None:
    """We're already re-raising the boot error; a flaky close must
    not mask it."""
    from bp_router.app import AppState, _boot_cleanup

    state = AppState()
    pool = MagicMock()
    pool.close = AsyncMock(side_effect=RuntimeError("pool already dead"))
    state.db_pool = pool
    state.redis = None

    asyncio.run(_boot_cleanup(state))  # must not raise
    pool.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Behavioural: a phase-2 failure produces a phased `boot_failed` and
# still propagates (process exits — diagnosable, not silent).
# ---------------------------------------------------------------------------


def test_boot_failure_logs_phase_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("fastapi")
    from bp_router import app
    from bp_router.db import connection as dbconn

    # Keep phase 1 inert so the failure lands cleanly in phase 2.
    # `caller_agent_cache_max` is read in pre-try state init and
    # must be a real int (BoundedLRUDict validates it).
    fake_settings = MagicMock()
    fake_settings.caller_agent_cache_max = 10_000
    monkeypatch.setattr(app, "load_settings", lambda: fake_settings)
    import bp_router.observability as obs

    monkeypatch.setattr(obs, "configure_logging", lambda s: None)
    monkeypatch.setattr(obs, "configure_tracing", lambda s: None)
    monkeypatch.setattr(obs, "configure_metrics", lambda: None)

    boom = OSError("connection refused")
    monkeypatch.setattr(
        dbconn, "open_pool", AsyncMock(side_effect=boom)
    )

    fake_app = MagicMock()

    async def _drive() -> None:
        cm = app.lifespan(fake_app)
        await cm.__aenter__()

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(OSError, match="connection refused"):
            asyncio.run(_drive())

    failed = [
        r for r in caplog.records if getattr(r, "event", None) == "boot_failed"
    ]
    assert len(failed) == 1
    # The whole point: the failing phase is named.
    assert failed[0].phase == "database_pool"


# ---------------------------------------------------------------------------
# Structural pins on lifespan
# ---------------------------------------------------------------------------


def _lifespan_fn():  # type: ignore[no-untyped-def]
    from bp_router import app

    src = textwrap.dedent(inspect.getsource(app.lifespan))
    return ast.parse(src).body[0]


def _boot_try(fn):  # type: ignore[no-untyped-def]
    """The boot Try is the one whose handler logs `boot_failed`."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            consts = {
                c.value
                for c in ast.walk(h)
                if isinstance(c, ast.Constant) and c.value == "boot_failed"
            }
            if consts:
                return node, h
    return None, None


def test_lifespan_wraps_boot_in_phase_aware_handler() -> None:
    """The boot body is wrapped in a `try` whose handler catches
    `BaseException` (not just `Exception` — a misconfig can surface
    as SystemExit/KeyboardInterrupt-adjacent), logs `boot_failed`
    with the tracked phase, cleans up, and re-raises."""
    pytest.importorskip("fastapi")
    fn = _lifespan_fn()
    boot_try, handler = _boot_try(fn)
    assert boot_try is not None, "no boot try/except logging boot_failed"

    # Catches BaseException (broadest — we never want an
    # uncategorised boot error to skip the diagnostic).
    assert isinstance(handler.type, ast.Name)
    assert handler.type.id == "BaseException"

    # boot_failed log passes `phase=current_phase` (the *tracked*
    # variable, not a constant) so the line names the real phase.
    phase_is_tracked_var = False
    for node in ast.walk(handler):
        if not (isinstance(node, ast.Call)):
            continue
        for kw in node.keywords:
            if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                for k, v in zip(kw.value.keys, kw.value.values):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "phase"
                        and isinstance(v, ast.Name)
                        and v.id == "current_phase"
                    ):
                        phase_is_tracked_var = True
    assert phase_is_tracked_var, "boot_failed must log phase=current_phase"

    # Best-effort cleanup, then an unconditional bare re-raise (a
    # fatal misconfig must still exit — legible, not silent).
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_boot_cleanup"
        for n in ast.walk(handler)
    ), "boot handler must call _boot_cleanup"
    bare_reraises = [
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.Raise) and n.exc is None
    ]
    assert bare_reraises, "boot handler must end in a bare `raise`"


def test_lifespan_tracks_every_phase() -> None:
    """`_boot_phase("<name>")` is called for each phase, and the
    boot try is entered with `current_phase = "init"` so a failure
    *before* phase 1 still names something."""
    pytest.importorskip("fastapi")
    fn = _lifespan_fn()

    phase_names = {
        n.args[0].value
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_boot_phase"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    }
    # The numbered phases the boot sequence actually has.
    expected = {
        "observability",
        "database_pool",
        "redis",
        "rate_limiters",
        "file_store",
        "acl_rules",
        "builtin_agents",
        "llm_presets",
        "correlation",
        "background_tasks",
    }
    assert expected <= phase_names, f"missing phases: {expected - phase_names}"

    # current_phase seeded before the try so a pre-phase-1 failure
    # is still attributed.
    seeded_init = any(
        isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "current_phase"
            for t in n.targets
        )
        and isinstance(n.value, ast.Constant)
        and n.value.value == "init"
        for n in ast.walk(fn)
    )
    assert seeded_init, "current_phase must be seeded to 'init' before the try"


def test_boot_handler_precedes_request_serving() -> None:
    """The boot guard must wrap *only* boot, not request serving:
    the `yield` lives in a later, separate try/finally so a runtime
    error during serving is NOT misreported as a boot failure."""
    pytest.importorskip("fastapi")
    fn = _lifespan_fn()
    boot_try, _ = _boot_try(fn)

    # No `yield` inside the boot try (it would mean the handler also
    # catches serving-time errors).
    assert not any(
        isinstance(n, ast.Yield) for n in ast.walk(boot_try)
    ), "yield must not be inside the boot try/except"


# ---------------------------------------------------------------------------
# Structural pin: boot-tolerant Redis
# ---------------------------------------------------------------------------


def test_redis_unreachable_at_boot_is_tolerated() -> None:
    """`open_redis(...)` is wrapped in a try whose except path sets
    `state.redis = None`, flips `redis_health` to 0, and logs
    `redis_unreachable_at_boot` — matching the runtime's existing
    degrade-and-alert contract instead of crashlooping."""
    pytest.importorskip("fastapi")
    fn = _lifespan_fn()

    # The Redis try is the one whose *handler* names
    # `redis_unreachable_at_boot` (uniquely distinguishes it from
    # the outer boot try, whose body transitively contains the
    # open_redis call too).
    redis_try = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if any(
            isinstance(c, ast.Constant)
            and c.value == "redis_unreachable_at_boot"
            for h in node.handlers
            for c in ast.walk(h)
        ):
            redis_try = node
            break
    assert redis_try is not None, "no try with a redis_unreachable_at_boot handler"

    # The guarded body must actually be the open_redis call.
    assert any(
        isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "open_redis"
        for stmt in redis_try.body
        for c in ast.walk(stmt)
    ), "the redis_unreachable try must guard the open_redis call"

    # Sets state.redis = None and redis_health.set(0) in the except.
    h = redis_try.handlers[0]
    sets_redis_none = any(
        isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "redis"
            for t in n.targets
        )
        and isinstance(n.value, ast.Constant)
        and n.value.value is None
        for n in ast.walk(h)
    )
    assert sets_redis_none, "boot-tolerant Redis must set state.redis = None"
    health_zeroed = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "set"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == 0
        for n in ast.walk(h)
    )
    assert health_zeroed, "boot-tolerant Redis must redis_health.set(0)"
