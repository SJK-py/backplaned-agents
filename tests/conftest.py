"""tests/conftest.py — pytest fixtures + shared LLM-test helpers.

The helpers below were duplicated across
`test_llm_presets*.py` / `test_llm_hosted_base_url.py` /
`test_llm_openai_compatible_adapter.py`. Centralising them keeps
the cache-key construction, stub semantics, and `_preset` defaults
in one place — when one of those changes, every test picks it up.

Provided as plain importable callables (not pytest fixtures) so
existing tests can `from tests.conftest import ...` without
restructuring. pytest fixtures still work because conftest is a
regular Python module.

This file ALSO holds the project-wide `pytest_collection_modifyitems`
hook that prevents silent passes on `async def test_xxx` (review
item Test-H1).
"""

from __future__ import annotations

import inspect
import os
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Async-test silent-pass guard (review item Test-H1)
# ---------------------------------------------------------------------------
#
# `pyproject.toml` declares `asyncio_mode = "auto"` which the
# `pytest-asyncio` plugin honours by automatically marking + running
# async test functions. The plugin is listed in `[project.optional-
# dependencies].dev` but the CI matrix does NOT install it on every
# job — most of the suite runs without async tests at all, and the
# few that do use the `asyncio.run(_drive())` pattern explicitly.
#
# The footgun: a contributor adding a NEW `async def test_xxx` on a
# job without pytest-asyncio gets silently green. Pytest collects
# the async function, calls it, gets back a coroutine (which it
# happily evaluates as truthy), and reports PASSED — the body never
# executes.
#
# The hook below detects async test functions at collection time and
# fails them loudly with an actionable error. The hook is a no-op
# when pytest-asyncio is installed (it has its own collection logic
# that converts async items into synchronous wrappers, so by the
# time we run the function is no longer a coroutine factory).


def _has_async_runner() -> bool:
    """True when an async-test runner plugin is installed.
    Pytest-asyncio is the canonical one; tolerant of other plugins
    that wrap async functions (`anyio[pytest]`, `pytest-trio`)."""
    try:
        import pytest_asyncio  # noqa: F401, PLC0415
        return True
    except ImportError:
        pass
    try:
        # `anyio[pytest]` registers a marker.
        # anyio is installed transitively via httpx; only the pytest
        # plugin counts. Heuristic: presence of the plugin module.
        import importlib  # noqa: PLC0415

        import anyio  # noqa: F401, PLC0415
        if importlib.util.find_spec("anyio.pytest_plugin") is not None:
            return True
    except ImportError:
        pass
    return False


_ASYNC_RUNNER_AVAILABLE = _has_async_runner()


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Refuse to collect raw async test functions when no async
    runner plugin is installed.

    Without this hook, a `async def test_xxx` returns a coroutine
    object that pytest treats as a passing return — the test body
    NEVER executes (review item Test-H1). The error below makes
    the failure obvious so a contributor sees it immediately.

    Tests that drive their own async via `asyncio.run(...)` inside
    a sync `def test_xxx` are unaffected — they're plain sync
    functions to pytest's collector.
    """
    if _ASYNC_RUNNER_AVAILABLE:
        return
    for item in items:
        func = getattr(item, "function", None)
        if func is None:
            continue
        if inspect.iscoroutinefunction(func):
            # Mark the test as failed at collection time. `xfail` would
            # also work but `fail` makes the issue louder.
            raise pytest.UsageError(
                f"async test {item.nodeid!r} would silently no-op: "
                "pytest-asyncio (or another async-runner plugin) is "
                "not installed in this environment. Either install "
                "pytest-asyncio in dev deps and re-run, or rewrite "
                "the test as a sync function that drives its async "
                "code via `asyncio.run(_drive())` (see existing tests "
                "in `test_review_*` for examples)."
            )


# ---------------------------------------------------------------------------
# Postgres integration fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db_url() -> str:
    url = os.environ.get("TEST_DB_URL")
    if not url:
        pytest.skip("TEST_DB_URL not set; integration tests skipped")
    return url


@pytest.fixture
def suite_db_url() -> str:
    """DSN for the agent suite's own Postgres (`bp_agents`). Assumes the
    suite schema is applied (`alembic -c alembic_suite.ini upgrade head`);
    suite tests truncate between runs rather than re-migrating."""
    url = os.environ.get("SUITE_DATABASE_URL")
    if not url:
        pytest.skip("SUITE_DATABASE_URL not set; suite DB tests skipped")
    return url


# ---------------------------------------------------------------------------
# LLM-test helpers (formerly duplicated across test_llm_presets*.py)
# ---------------------------------------------------------------------------


class _StubSettings:
    """Empty settings stand-in. LlmService never actually reads off
    settings except via attrs the live tests don't exercise."""


def make_llm_service():
    """Build an `LlmService` against an empty settings stub. Most LLM
    tests only need the in-memory presets/adapters maps."""
    from bp_router.llm.service import LlmService

    return LlmService(_StubSettings())  # type: ignore[arg-type]


def make_preset(
    name: str = "test",
    *,
    provider: str = "gemini",
    concrete_model: str | None = None,
    api_key_ref: str = "env://X",
    api_key: str | None = None,
    base_url: str | None = None,
    min_user_level: str = "*",
    fallback_preset: str | None = None,
    max_retries: int = 0,
):
    """Test-friendly `Preset` factory.

    Default `concrete_model` derives from the preset name so each
    preset lands in its own adapter-cache slot — the cache key is
    `provider::concrete_model::base_url::api_key_ref`, and tests
    routinely register multiple presets with the same provider +
    api_key_ref. Without name-derived models they'd all collide.
    """
    from bp_router.llm.presets import Preset

    if concrete_model is None:
        concrete_model = f"model-{name}"
    return Preset(
        name=name,
        provider=provider,
        concrete_model=concrete_model,
        api_key_ref=api_key_ref,
        api_key=api_key,
        base_url=base_url,
        min_user_level=min_user_level,
        fallback_preset=fallback_preset,
        max_retries=max_retries,
    )


def cache_key_for(preset) -> str:
    """Reconstruct the adapter-cache key for a preset.

    Mirrors `LlmService._resolve_one`'s key formation. Tests
    pre-populate `svc._adapters[<key>]` to avoid building real
    adapters (which import `cryptography` for secret resolution).
    """
    secret_marker = (
        f"inline:{preset.name}" if preset.api_key else preset.api_key_ref
    )
    base_url_marker = preset.base_url or "-"
    return (
        f"{preset.provider}::{preset.concrete_model}::"
        f"{base_url_marker}::{secret_marker}"
    )


class StubAdapter:
    """Programmable stand-in for a `ProviderAdapter`.

    Drives `generate`, `embed`, and `count_tokens` from a queue of
    pre-staged outcomes (Exception → raise; any other value →
    return). Tracks call counts so tests can assert how many attempts
    were made against this adapter.
    """

    provider_name = "stub"

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        self.outcomes: list[Any] = []
        self.calls = 0

    def push(self, outcome: Any) -> StubAdapter:
        self.outcomes.append(outcome)
        return self

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        return self._next()

    async def embed(self, *args: Any, **kwargs: Any) -> Any:
        return self._next()

    async def count_tokens(self, *args: Any, **kwargs: Any) -> Any:
        return self._next()

    def _next(self) -> Any:
        self.calls += 1
        if not self.outcomes:
            raise RuntimeError(f"StubAdapter {self.name!r} ran out of outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def wire_stub_adapter(svc, preset, adapter: StubAdapter | None = None) -> StubAdapter:
    """Pre-populate the adapter cache for a preset.

    Bypasses `LlmService._build_adapter`, which would try to resolve
    the api_key via `cryptography` (not always installed in CI). The
    returned adapter is the one routed to when `_resolve_one` looks
    up the cache key.
    """
    if adapter is None:
        adapter = StubAdapter(preset.name)
    svc._adapters[cache_key_for(preset)] = adapter  # type: ignore[assignment]
    return adapter
