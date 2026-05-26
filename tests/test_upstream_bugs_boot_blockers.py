"""Tests for the upstream-bug bundle reported from a clean dev-stack
boot of the Backplaned template:

  - Bug 1: `pyproject.toml` admin extra didn't declare
    `itsdangerous` (Starlette ≥1.0 dropped it from base reqs).
    Without it, `bp_admin/app.py` fails to import and the router
    silently logs `admin_ui_unavailable` instead of mounting the
    admin UI.
  - Bug 2: three Gemini default presets (`gemini-2.5`,
    `gemini-2.5-flash`, `gemini-2.5-pro`) — and five OpenAI
    defaults (`gpt-5.5`, `gpt-5.5-pro`, `gpt-5.4`, `gpt-5.4-mini`,
    `gpt-4.1`) — violated the `llm_presets.name` CHECK regex
    (`^[a-z][a-z0-9_-]{0,63}$` disallows `.`). On first boot the
    seed loop crashed mid-way through, leaving the table
    partially seeded; the `if not rows` guard then never
    re-seeded on subsequent boots. Plus the seed loop wasn't
    transactional so the partial state was sticky.
  - Bug 3: `app.mount("/admin", admin_app)` doesn't run the
    sub-app's `lifespan`, so `admin_app.state.upstream` was never
    set; every `/admin/*` request 500'd. Pre-populate at mount
    time + teardown from the parent lifespan.
  - Smaller: `regex=` kwarg deprecation in FastAPI's `Query(...)`.
"""

from __future__ import annotations

import inspect
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Bug 1: pyproject admin extra carries itsdangerous
# ===========================================================================


def test_bug1_pyproject_admin_extra_declares_itsdangerous() -> None:
    """The `admin` optional-deps group MUST declare `itsdangerous`
    explicitly. Starlette ≥1.0 dropped it from base requirements;
    `SessionMiddleware` (used by `bp_admin/app.py`) imports
    `itsdangerous` directly. Without it, the admin sub-app fails
    to import and the router silently logs `admin_ui_unavailable`
    on startup."""
    from pathlib import Path

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # Locate the admin extra block.
    admin_block_match = re.search(
        r"admin\s*=\s*\[(.*?)\]", pyproject, re.DOTALL
    )
    assert admin_block_match, "`admin` extra block not found in pyproject.toml"
    admin_block = admin_block_match.group(1)
    assert "itsdangerous" in admin_block, (
        "upstream-bug #1 regression: itsdangerous no longer in the "
        "admin extra — bp_admin will fail to import"
    )


def test_bug1_admin_extra_comment_is_not_stale() -> None:
    """The pre-fix comment claimed Starlette brings itsdangerous
    'transitively' — no longer true since Starlette ≥1.0. Pin
    that the comment now correctly states it's a soft dep we
    depend on."""
    from pathlib import Path

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # The misleading "transitively" claim must be gone.
    assert "transitively" not in pyproject, (
        "upstream-bug #1: stale 'transitively' comment still present"
    )


def test_bug1_bp_admin_imports_clean() -> None:
    """End-to-end: with the admin extra installed (which it is
    in CI), `bp_admin.app.create_app` must import without
    raising. This is the failure the reviewer hit on a fresh
    `pip install -e ".[router,admin]"`."""
    pytest.importorskip("itsdangerous")
    pytest.importorskip("fastapi")
    from bp_admin.app import create_app  # noqa: F401


# ===========================================================================
# Bug 2: default preset names + transactional seed
# ===========================================================================


PRESET_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def test_bug2_every_default_preset_name_satisfies_db_check_constraint() -> None:
    """Every preset returned by `default_presets()` MUST satisfy
    the `llm_presets.name` CHECK regex. A regression that
    re-introduces a dotted preset name would crash the seed loop
    on first boot — exactly what upstream-bug #2 fixed."""
    from bp_router.llm.presets import default_presets

    for preset in default_presets():
        assert PRESET_NAME_RE.match(preset.name), (
            f"upstream-bug #2 regression: preset name {preset.name!r} "
            f"violates the DB CHECK regex {PRESET_NAME_RE.pattern!r}; "
            "this preset will crash the first-boot seed loop"
        )


def test_bug2_concrete_models_keep_dotted_form() -> None:
    """Sanity: the rename was preset-name-only. `concrete_model`
    (the upstream provider model identifier) MUST keep its
    dotted form — that's what google-genai / openai SDKs expect
    on the wire. A regression that strips dots from
    concrete_model would break every actual LLM call."""
    from bp_router.llm.presets import default_presets

    presets = {p.name: p for p in default_presets()}
    # Spot-check the renames: name uses `-`, concrete_model uses `.`.
    assert presets["gemini-2-5-pro"].concrete_model == "gemini-2.5-pro"
    assert presets["gemini-3-5-flash"].concrete_model == "gemini-3.5-flash"
    assert presets["gpt-5-5"].concrete_model == "gpt-5.5"
    assert presets["gpt-5-5-pro"].concrete_model == "gpt-5.5-pro"
    assert presets["gpt-5-4"].concrete_model == "gpt-5.4"
    assert presets["gpt-5-4-mini"].concrete_model == "gpt-5.4-mini"
    assert presets["gpt-4-1"].concrete_model == "gpt-4.1"


def test_bug2_load_presets_from_db_seeds_in_a_single_transaction() -> None:
    """Source pin: the seed branch wraps the per-preset INSERT
    loop in `async with conn.transaction()`. Without the
    transaction, a CHECK-constraint failure on any one preset
    leaves the table in a partially-seeded state that the
    `if not rows` guard never recovers from on subsequent
    boots — the operator has to manually `TRUNCATE llm_presets`."""
    from bp_router.llm import service as svc_module

    src = inspect.getsource(svc_module.LlmService.load_presets_from_db)
    # The transaction context must wrap the for-loop.
    txn_idx = src.find("async with conn.transaction()")
    assert txn_idx > 0, (
        "upstream-bug #2 regression: seed loop is no longer wrapped "
        "in a transaction; partial-seed states will be sticky"
    )
    # The for-loop must follow the transaction opener (i.e. be
    # INSIDE it).
    seed_for_idx = src.find("for p in seeded:")
    assert seed_for_idx > txn_idx, (
        "upstream-bug #2: the for-loop must run INSIDE the transaction"
    )
    # The insert call must be inside the loop.
    insert_idx = src.find("queries.insert_llm_preset(", seed_for_idx)
    assert insert_idx > seed_for_idx


# ===========================================================================
# Bug 3: admin sub-app state.upstream eagerly populated at mount time
# ===========================================================================


def test_bug3_mount_admin_ui_eagerly_creates_upstream_client() -> None:
    """Source pin: `_mount_admin_ui` MUST construct
    `UpstreamClient` and assign it to `admin_app.state.upstream`
    BEFORE `app.mount(...)`. Mounted sub-apps don't get their
    own `lifespan` invocation — the parent must populate state
    eagerly or every `/admin/*` request 500s on
    `AttributeError: 'State' object has no attribute 'upstream'`."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module._mount_admin_ui)
    assert "UpstreamClient(" in src, (
        "upstream-bug #3 regression: _mount_admin_ui no longer "
        "eagerly creates UpstreamClient"
    )
    assert "admin_app.state.upstream" in src, (
        "upstream-bug #3 regression: state.upstream not set at mount time"
    )
    # Order: state populate must precede the mount call.
    state_idx = src.find("admin_app.state.upstream")
    mount_idx = src.find('app.mount("/admin"')
    assert 0 < state_idx < mount_idx, (
        "upstream-bug #3: state.upstream must be set BEFORE app.mount; "
        "otherwise the first request that reaches the auth middleware "
        "could 500 before the mount-time setup completes"
    )


def test_bug3_lifespan_finally_calls_subapp_shutdown_helper() -> None:
    """Source pin: the parent lifespan's `finally` block must
    call `_shutdown_mounted_subapps(app)` so resources eagerly
    populated at mount time get torn down. Mounted sub-apps
    don't run their own `lifespan`, so their cleanup falls to
    the parent."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    assert "_shutdown_mounted_subapps(app)" in src, (
        "upstream-bug #3 regression: parent lifespan no longer "
        "tears down mounted sub-apps' upstream clients"
    )
    # Order: must run BEFORE db_pool.close (consistent with the H-4
    # ordering invariant — every consumer drains before the pool
    # closes).
    subapp_idx = src.find("_shutdown_mounted_subapps")
    pool_idx = src.find("db_pool.close")
    assert 0 < subapp_idx < pool_idx, (
        "upstream-bug #3: sub-app teardown must precede pool close"
    )


def test_bug3_shutdown_helper_walks_routes_and_aclose_each() -> None:
    """The helper iterates `app.routes`, finds mounted sub-apps
    with a `state.upstream`, and calls `aclose()` on each.
    Per-subapp errors are logged but swallowed."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    fn = app_module._shutdown_mounted_subapps
    src = inspect.getsource(fn)
    assert "for route in app.routes" in src
    assert "state" in src
    assert "upstream" in src
    assert "aclose" in src
    # Must swallow per-subapp errors.
    assert "except Exception" in src


def test_bug3_shutdown_helper_no_op_with_no_mounted_subapps() -> None:
    """Behavioural: a parent app with NO mounted sub-apps
    (router-only deployment with `serve_admin_ui=false`) must
    not crash through the helper."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router import app as app_module

    fake_app = MagicMock()
    fake_app.routes = []  # no mounts

    # Must not raise.
    asyncio.run(app_module._shutdown_mounted_subapps(fake_app))


def test_bug3_shutdown_helper_aclose_called_on_each_subapp() -> None:
    """Behavioural: the helper invokes `aclose()` on every
    mounted sub-app's upstream."""
    pytest.importorskip("fastapi")
    import asyncio
    from types import SimpleNamespace

    from bp_router import app as app_module

    upstream_a = MagicMock()
    upstream_a.aclose = AsyncMock()
    upstream_b = MagicMock()
    upstream_b.aclose = AsyncMock()

    sub_a = SimpleNamespace(state=SimpleNamespace(upstream=upstream_a))
    sub_b = SimpleNamespace(state=SimpleNamespace(upstream=upstream_b))
    sub_c = SimpleNamespace(state=SimpleNamespace())  # no upstream attr

    route_a = SimpleNamespace(app=sub_a, path="/admin")
    route_b = SimpleNamespace(app=sub_b, path="/other")
    route_c = SimpleNamespace(app=sub_c, path="/no-upstream")

    fake_app = SimpleNamespace(routes=[route_a, route_b, route_c])

    asyncio.run(app_module._shutdown_mounted_subapps(fake_app))

    upstream_a.aclose.assert_awaited_once()
    upstream_b.aclose.assert_awaited_once()


def test_bug3_shutdown_helper_swallows_per_subapp_aclose_errors() -> None:
    """A misbehaving sub-app's `aclose()` raising must NOT block
    shutdown of other sub-apps."""
    pytest.importorskip("fastapi")
    import asyncio
    from types import SimpleNamespace

    from bp_router import app as app_module

    bad_upstream = MagicMock()
    bad_upstream.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    good_upstream = MagicMock()
    good_upstream.aclose = AsyncMock()

    bad_sub = SimpleNamespace(state=SimpleNamespace(upstream=bad_upstream))
    good_sub = SimpleNamespace(state=SimpleNamespace(upstream=good_upstream))

    fake_app = SimpleNamespace(routes=[
        SimpleNamespace(app=bad_sub, path="/bad"),
        SimpleNamespace(app=good_sub, path="/good"),
    ])

    # Must not raise.
    asyncio.run(app_module._shutdown_mounted_subapps(fake_app))
    # Good upstream still got closed.
    good_upstream.aclose.assert_awaited_once()


# ===========================================================================
# Smaller: pattern= deprecation in admin.list_invitations
# ===========================================================================


def test_smaller_list_invitations_uses_pattern_not_regex() -> None:
    """`Query(..., regex=...)` is deprecated in FastAPI in favour
    of `pattern=...`. Source pin so the warning doesn't creep
    back."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin as admin_module

    src = inspect.getsource(admin_module.list_invitations)
    assert "regex=" not in src, (
        "regex= still present on Query — FastAPI deprecation warning"
    )
    assert "pattern=" in src
