"""Tests for the test-infrastructure review fixes (Test-H1, Test-H2).

Test-H1 — conftest guard prevents silent passes on `async def
test_xxx` when no async-runner plugin (`pytest-asyncio` etc.) is
installed. Without the guard, pytest collected the async function,
called it, got a coroutine object back (truthy → "passed"), and
the test body NEVER executed.

Test-H2 — `TestRouter._start` snapshots `os.environ` before
mutation and `_stop` restores. Without this, `ROUTER_DB_URL`,
`ROUTER_JWT_SECRET`, etc. leaked across the rest of the pytest
session.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

# ===========================================================================
# Test-H1: conftest guard for async tests
# ===========================================================================


def test_conftest_has_async_guard_hook() -> None:
    """Source-level pin: `tests/conftest.py` defines
    `pytest_collection_modifyitems` and the `_has_async_runner`
    helper. Catches a future regression that drops the hook."""
    import tests.conftest as conftest_mod

    assert hasattr(conftest_mod, "pytest_collection_modifyitems")
    assert hasattr(conftest_mod, "_has_async_runner")
    # Hook references the runner-detection helper.
    src = inspect.getsource(conftest_mod.pytest_collection_modifyitems)
    assert "_ASYNC_RUNNER_AVAILABLE" in src
    # Error message guides the contributor.
    assert "pytest-asyncio" in src
    assert "asyncio.run" in src


def test_async_runner_detection_returns_bool() -> None:
    """`_has_async_runner` returns a bool. The runtime value depends
    on the install env so we don't pin True/False; just the type."""
    import tests.conftest as conftest_mod

    out = conftest_mod._has_async_runner()
    assert isinstance(out, bool)


def test_async_runner_detection_finds_pytest_asyncio_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pytest-asyncio is importable, `_has_async_runner`
    returns True. Stub the import to simulate the installed case."""
    import sys
    import types

    # Build a fake pytest_asyncio module so the dynamic import inside
    # _has_async_runner finds it.
    fake = types.ModuleType("pytest_asyncio")
    monkeypatch.setitem(sys.modules, "pytest_asyncio", fake)

    import tests.conftest as conftest_mod

    assert conftest_mod._has_async_runner() is True


def test_async_runner_detection_returns_false_when_no_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither pytest-asyncio nor anyio[pytest] is importable,
    `_has_async_runner` returns False. Simulated by stubbing imports
    to fail."""
    import builtins
    import sys

    # Drop any cached import of pytest_asyncio + the anyio plugin.
    monkeypatch.delitem(sys.modules, "pytest_asyncio", raising=False)
    monkeypatch.delitem(sys.modules, "anyio.pytest_plugin", raising=False)

    real_import = builtins.__import__

    def _import_no_async_runner(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pytest_asyncio":
            raise ImportError("simulated no pytest-asyncio")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import_no_async_runner)
    # The find_spec path also needs to return None for the anyio
    # plugin. We patch importlib.util.find_spec.
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def _find_spec_no_anyio(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "anyio.pytest_plugin":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec_no_anyio)

    import tests.conftest as conftest_mod

    assert conftest_mod._has_async_runner() is False


def test_async_test_function_is_recognised_as_coroutinefunction() -> None:
    """Sanity check the underlying assumption: pytest exposes the
    test function via `item.function`, and `inspect.iscoroutinefunction`
    returns True for `async def` definitions. The conftest hook
    relies on this distinction."""
    async def _example_async() -> None:
        return None

    def _example_sync() -> None:
        return None

    assert inspect.iscoroutinefunction(_example_async) is True
    assert inspect.iscoroutinefunction(_example_sync) is False


# ===========================================================================
# Test-H2: TestRouter env snapshot/restore
# ===========================================================================


def test_test_router_snapshots_env_on_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_env_set` records the prior value (or unset sentinel) on
    first call so `_restore_env` can roll back."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    # Build a TestRouter without `_start`ing it (no DB needed for
    # the snapshot logic). `__init__` requires db_url.
    router = TestRouter(db_url="postgresql://stub")

    # Var unset before — snapshot records the unset sentinel.
    monkeypatch.delenv("REVIEW_TEST_ENV_NEW", raising=False)
    router._env_set("REVIEW_TEST_ENV_NEW", "value")
    assert os.environ["REVIEW_TEST_ENV_NEW"] == "value"
    assert router._env_snapshot["REVIEW_TEST_ENV_NEW"] is router._ENV_UNSET

    # Var was set before — snapshot records the prior string.
    monkeypatch.setenv("REVIEW_TEST_ENV_PREEXISTING", "original")
    router._env_set("REVIEW_TEST_ENV_PREEXISTING", "overridden")
    assert os.environ["REVIEW_TEST_ENV_PREEXISTING"] == "overridden"
    assert router._env_snapshot["REVIEW_TEST_ENV_PREEXISTING"] == "original"

    # Cleanup so the test itself doesn't leak the vars.
    router._restore_env()
    assert "REVIEW_TEST_ENV_NEW" not in os.environ
    assert os.environ["REVIEW_TEST_ENV_PREEXISTING"] == "original"


def test_test_router_setdefault_snapshots_even_when_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_env_setdefault` snapshots the prior value even when the
    var was already set and `setdefault` doesn't actually write.
    Restore must still land back at the prior value (no-op in that
    case, but the snapshot is what guarantees it)."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    router = TestRouter(db_url="postgresql://stub")

    monkeypatch.setenv("REVIEW_TEST_PRESET", "preset_value")
    router._env_setdefault("REVIEW_TEST_PRESET", "would_not_write")
    # setdefault preserves existing value.
    assert os.environ["REVIEW_TEST_PRESET"] == "preset_value"
    # Snapshot still records the prior value.
    assert router._env_snapshot["REVIEW_TEST_PRESET"] == "preset_value"

    router._restore_env()
    assert os.environ["REVIEW_TEST_PRESET"] == "preset_value"


def test_test_router_snapshot_only_records_first_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `_env_set` is called twice for the same key, the snapshot
    keeps the FIRST call's prior value — the one that existed
    BEFORE the context entered. Without this guard, the second
    call's "prior" would be the first call's overwrite, and
    restore would land at the wrong value."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    router = TestRouter(db_url="postgresql://stub")

    monkeypatch.setenv("REVIEW_TEST_TWICE", "ORIGINAL")
    router._env_set("REVIEW_TEST_TWICE", "FIRST_OVERRIDE")
    router._env_set("REVIEW_TEST_TWICE", "SECOND_OVERRIDE")
    # Snapshot records ORIGINAL, NOT FIRST_OVERRIDE.
    assert router._env_snapshot["REVIEW_TEST_TWICE"] == "ORIGINAL"

    router._restore_env()
    assert os.environ["REVIEW_TEST_TWICE"] == "ORIGINAL"


def test_test_router_start_uses_env_helpers_not_raw_os_environ() -> None:
    """Source-level: `_start` mutates env via `_env_set` /
    `_env_setdefault`, NOT direct `os.environ[...]` /
    `os.environ.setdefault`. A regression that drops the snapshot
    helpers would let env vars leak across tests again (review
    item Test-H2)."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    src = inspect.getsource(TestRouter._start)
    # Helper calls present.
    assert "self._env_set(" in src
    assert "self._env_setdefault(" in src
    # And the previous direct mutations are gone.
    assert "os.environ[\"ROUTER_DB_URL\"]" not in src
    assert 'os.environ.setdefault("ROUTER_PUBLIC_URL"' not in src


def test_test_router_stop_calls_restore() -> None:
    """Source-level: `_stop` invokes `_restore_env` so the
    `__aexit__` path leaves no env vars hanging."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    src = inspect.getsource(TestRouter._stop)
    assert "self._restore_env()" in src


def test_test_router_restore_handles_unset_marker() -> None:
    """The unset-sentinel path: a var that didn't exist before the
    context entered must be DELETED on restore, not assigned the
    sentinel object as a string."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    router = TestRouter(db_url="postgresql://stub")
    # Var didn't exist before.
    if "REVIEW_TEST_DELETE_ON_RESTORE" in os.environ:
        del os.environ["REVIEW_TEST_DELETE_ON_RESTORE"]

    router._env_set("REVIEW_TEST_DELETE_ON_RESTORE", "set_during_context")
    assert os.environ["REVIEW_TEST_DELETE_ON_RESTORE"] == "set_during_context"
    router._restore_env()
    # After restore: the var is gone, NOT the sentinel object.
    assert "REVIEW_TEST_DELETE_ON_RESTORE" not in os.environ


def test_test_router_restore_clears_snapshot() -> None:
    """After `_restore_env`, the snapshot dict is empty. A second
    `_start` / `_stop` cycle on the same router instance starts
    fresh."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    router = TestRouter(db_url="postgresql://stub")
    router._env_set("REVIEW_TEST_CLEAR_SNAPSHOT", "v")
    assert router._env_snapshot
    router._restore_env()
    assert router._env_snapshot == {}


# ===========================================================================
# Smoke: conftest module imports cleanly even without runner installed
# ===========================================================================


def test_conftest_imports_without_async_runner() -> None:
    """The hook itself must not raise at import time when no async
    runner is installed — the dev-env CI matrix that doesn't ship
    pytest-asyncio relies on conftest.py loading without errors."""
    # If the import fails at collection, pytest can't even reach
    # this test. The fact that we ARE running confirms the import
    # succeeded.
    import tests.conftest as conftest_mod
    assert conftest_mod is not None
