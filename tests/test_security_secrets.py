"""Tests for `bp_router.security.secrets.resolve_secret_ref`.

The review (C8) flagged this as untested. The function is a small
parser-and-dispatch over `<scheme>://<rest>` URLs:

  env://VAR_NAME            — read os.environ
  vault://...               — deferred (NotImplementedError until hvac wired)
  awssm://...               — deferred
  gcpsm://...               — deferred
  <unknown scheme>          — ValueError
  <no `://`>                — pass through unchanged

Loaded via `importlib.util` directly to bypass `bp_router.security.__init__`,
which eagerly imports `passwords` → cryptography → pyo3 panic in this
sandbox. The module under test has no third-party deps of its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SECRETS_MOD_PATH = Path("/home/user/backplaned-next/bp_router/security/secrets.py")


def _load_secrets_module():
    """Load `bp_router.security.secrets` via importlib without going
    through the package init. Memoised in module globals so successive
    calls return the same module object (matters for monkeypatch on
    module-level names)."""
    cached = sys.modules.get("_secrets_under_test")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "_secrets_under_test", _SECRETS_MOD_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_secrets_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def secrets_mod():
    return _load_secrets_module()


# ---------------------------------------------------------------------------
# Pass-through (no scheme)
# ---------------------------------------------------------------------------


def test_value_without_scheme_returned_unchanged(secrets_mod) -> None:
    """Callers may pass either a raw secret or a `secret_ref` URL.
    Anything without `://` is treated as raw and returned as-is."""
    assert secrets_mod.resolve_secret_ref("plain-text-secret") == (
        "plain-text-secret"
    )
    # Empty string also passes through (callers handle empty themselves).
    assert secrets_mod.resolve_secret_ref("") == ""


# ---------------------------------------------------------------------------
# env:// scheme
# ---------------------------------------------------------------------------


def test_env_scheme_reads_environment_variable(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BP_TEST_SECRET", "the-actual-secret")
    assert secrets_mod.resolve_secret_ref("env://BP_TEST_SECRET") == (
        "the-actual-secret"
    )


def test_env_scheme_missing_var_raises_keyerror(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators want a loud failure when an env var hasn't been set —
    silently returning empty string would let agents authenticate
    against `Authorization: Bearer ` (literal blank token)."""
    monkeypatch.delenv("BP_DEFINITELY_UNSET", raising=False)
    with pytest.raises(KeyError, match="BP_DEFINITELY_UNSET"):
        secrets_mod.resolve_secret_ref("env://BP_DEFINITELY_UNSET")


def test_env_scheme_empty_var_returns_empty_string(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from missing: an env var deliberately set to empty
    returns empty (operators can rely on this for "no auth" upstreams)."""
    monkeypatch.setenv("BP_EMPTY", "")
    assert secrets_mod.resolve_secret_ref("env://BP_EMPTY") == ""


def test_env_scheme_preserves_special_chars(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JWT-shaped secrets contain dots, colons, slashes. The resolver
    must return the byte sequence verbatim — no decoding, no
    URL-unescaping."""
    weird = "sk-abc:def.ghi/jkl=mno+pqr"
    monkeypatch.setenv("BP_WEIRD", weird)
    assert secrets_mod.resolve_secret_ref("env://BP_WEIRD") == weird


def test_env_scheme_var_name_with_special_chars_passed_through_to_os_environ(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `rest` after `env://` IS the env var name. The resolver
    doesn't validate the name (env names with dots / hyphens are rare
    but legal on POSIX)."""
    monkeypatch.setenv("BP.WEIRD-NAME", "ok")
    assert secrets_mod.resolve_secret_ref("env://BP.WEIRD-NAME") == "ok"


# ---------------------------------------------------------------------------
# Scheme matching (case + delimiter)
# ---------------------------------------------------------------------------


def test_scheme_matching_is_case_insensitive(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators sometimes capitalise URL schemes by habit."""
    monkeypatch.setenv("BP_TEST", "v")
    assert secrets_mod.resolve_secret_ref("ENV://BP_TEST") == "v"
    assert secrets_mod.resolve_secret_ref("Env://BP_TEST") == "v"


def test_scheme_separator_must_be_three_chars(secrets_mod) -> None:
    """`env:VAR` (single colon, no slashes) is treated as raw text per
    the `://` short-circuit. Catches operators who think they're using
    the `secret_ref` machinery but actually wrote a literal."""
    out = secrets_mod.resolve_secret_ref("env:VAR_NAME")
    assert out == "env:VAR_NAME"


# ---------------------------------------------------------------------------
# Unknown / unimplemented schemes
# ---------------------------------------------------------------------------


def test_unknown_scheme_raises_value_error(secrets_mod) -> None:
    with pytest.raises(ValueError, match="Unsupported secret_ref scheme: 'foo'"):
        secrets_mod.resolve_secret_ref("foo://bar")


def test_vault_scheme_raises_not_implemented(secrets_mod) -> None:
    """The deferred backends should fail with NotImplementedError so an
    operator who's set the right env var but hasn't installed the SDK
    sees a clear message — not a generic ValueError that looks like a
    typo."""
    with pytest.raises(NotImplementedError, match="vault://"):
        secrets_mod.resolve_secret_ref("vault://kv/path")


def test_awssm_scheme_raises_not_implemented(secrets_mod) -> None:
    with pytest.raises(NotImplementedError, match="awssm://"):
        secrets_mod.resolve_secret_ref("awssm://my-secret")


def test_gcpsm_scheme_raises_not_implemented(secrets_mod) -> None:
    with pytest.raises(NotImplementedError, match="gcpsm://"):
        secrets_mod.resolve_secret_ref("gcpsm://projects/p/secrets/s")


# ---------------------------------------------------------------------------
# SecretCache
# ---------------------------------------------------------------------------


def test_cache_returns_same_value_within_ttl(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BP_CACHED", "first-value")
    cache = secrets_mod.SecretCache(ttl_s=60)
    v1 = cache.get("env://BP_CACHED")
    # Mutate the env. Within TTL the cache wins.
    monkeypatch.setenv("BP_CACHED", "rotated-value")
    v2 = cache.get("env://BP_CACHED")
    assert v1 == v2 == "first-value"


def test_cache_invalidate_specific_ref(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BP_CACHED", "first-value")
    cache = secrets_mod.SecretCache(ttl_s=60)
    cache.get("env://BP_CACHED")
    monkeypatch.setenv("BP_CACHED", "rotated-value")
    cache.invalidate("env://BP_CACHED")
    assert cache.get("env://BP_CACHED") == "rotated-value"


def test_cache_invalidate_all(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`invalidate(None)` drops everything — used after a rotation event."""
    monkeypatch.setenv("BP_A", "a1")
    monkeypatch.setenv("BP_B", "b1")
    cache = secrets_mod.SecretCache(ttl_s=60)
    cache.get("env://BP_A")
    cache.get("env://BP_B")
    monkeypatch.setenv("BP_A", "a2")
    monkeypatch.setenv("BP_B", "b2")
    cache.invalidate()
    assert cache.get("env://BP_A") == "a2"
    assert cache.get("env://BP_B") == "b2"


def test_cache_expiry_re_resolves(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After TTL expires, the next get() re-resolves against the
    backend. Patch `time.monotonic` to avoid sleeping in the test."""
    monkeypatch.setenv("BP_TTL", "v1")
    cache = secrets_mod.SecretCache(ttl_s=10)

    fake_now = [1000.0]

    import time

    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    assert cache.get("env://BP_TTL") == "v1"

    monkeypatch.setenv("BP_TTL", "v2")
    # Within TTL — cache wins.
    fake_now[0] = 1005.0
    assert cache.get("env://BP_TTL") == "v1"
    # Past TTL — re-resolve.
    fake_now[0] = 1011.0
    assert cache.get("env://BP_TTL") == "v2"


def test_cache_misses_dont_cache_exceptions(
    secrets_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyError on first lookup must NOT be cached as an empty string —
    callers retry after fixing the env, expect a successful resolution."""
    monkeypatch.delenv("BP_LATE_SET", raising=False)
    cache = secrets_mod.SecretCache(ttl_s=60)
    with pytest.raises(KeyError):
        cache.get("env://BP_LATE_SET")
    # Operator sets the env, retries.
    monkeypatch.setenv("BP_LATE_SET", "now-it-works")
    assert cache.get("env://BP_LATE_SET") == "now-it-works"
