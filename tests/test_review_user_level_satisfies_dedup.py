"""`_user_level_satisfies` consolidated in `bp_router.principals`.

R4 second-pass review found two near-identical implementations:
  * `bp_router/acl.py:_user_level_satisfies` (no None handling)
  * `bp_router/llm/presets.py:user_level_satisfies` (explicit None
    short-circuit before exact-match)

Both implement the same grammar but with subtly different
signatures. A future grammar change (e.g. `super_admin`) had to
land in two places. Now both modules re-export the canonical
implementation in `bp_router.principals`.

These tests pin:
  * The canonical implementation matches the documented grammar
    on every shape (admin / service / tier / `*` / unknown rule
    level).
  * Both call sites are wired to the canonical impl (identity
    check) so a future drift fails the pin.
"""

from __future__ import annotations

import pytest


def test_canonical_helper_admits_star_for_any_actual() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.principals import user_level_satisfies

    assert user_level_satisfies("admin", "*") is True
    assert user_level_satisfies("tier3", "*") is True
    assert user_level_satisfies(None, "*") is True


def test_canonical_helper_admin_and_service_are_exact() -> None:
    """`rule_level=admin` admits only `actual=admin`; same for
    `service`."""
    pytest.importorskip("asyncpg")
    from bp_router.principals import user_level_satisfies

    assert user_level_satisfies("admin", "admin") is True
    assert user_level_satisfies("service", "admin") is False
    assert user_level_satisfies("tier0", "admin") is False
    assert user_level_satisfies(None, "admin") is False

    assert user_level_satisfies("service", "service") is True
    assert user_level_satisfies("admin", "service") is False
    assert user_level_satisfies("tier0", "service") is False


def test_canonical_helper_tier_is_le() -> None:
    """`tierN` admits any level whose tier index ≤ N. admin/service
    map to tier_index=-1 so satisfy every tier rule."""
    pytest.importorskip("asyncpg")
    from bp_router.principals import user_level_satisfies

    # tier1 rule
    assert user_level_satisfies("admin", "tier1") is True
    assert user_level_satisfies("service", "tier1") is True
    assert user_level_satisfies("tier0", "tier1") is True
    assert user_level_satisfies("tier1", "tier1") is True
    assert user_level_satisfies("tier2", "tier1") is False
    assert user_level_satisfies("tier3", "tier1") is False
    assert user_level_satisfies(None, "tier1") is False


def test_canonical_helper_rejects_malformed_rule_level() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.principals import user_level_satisfies

    assert user_level_satisfies("admin", "garbage") is False
    assert user_level_satisfies("admin", "tier") is False
    assert user_level_satisfies("admin", "") is False


def test_acl_module_reuses_canonical_helper() -> None:
    """Identity check: `bp_router.acl._user_level_satisfies` IS the
    canonical helper, not a parallel implementation."""
    pytest.importorskip("asyncpg")
    from bp_router import acl
    from bp_router.principals import user_level_satisfies

    assert acl._user_level_satisfies is user_level_satisfies


def test_presets_module_reuses_canonical_helper() -> None:
    """Identity check: `bp_router.llm.presets.user_level_satisfies`
    IS the canonical helper."""
    pytest.importorskip("asyncpg")
    from bp_router.llm import presets
    from bp_router.principals import user_level_satisfies

    assert presets.user_level_satisfies is user_level_satisfies
