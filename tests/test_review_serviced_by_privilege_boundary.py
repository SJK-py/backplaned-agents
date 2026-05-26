"""R9 CRITICAL: serviced_by privilege boundary — a service principal
must never be able to mint credentials for, or be granted
serviced-by over, an admin/service target.

Pre-R9 the `serviced_by` delegated-token-minting mechanism never
checked `target.level`:

  - `grant_serviced_by` validated only the *grantee* is
    `level=service`, never the *target*.
  - `service_mint_refresh_token` / `mint_password_reset_token`
    authorised purely on `principal.user_id in target.serviced_by`.

So if an operator added a service principal to an *admin* user's
`serviced_by` (a plausible "let this automation rotate the
operator's tokens" setup), that service principal could mint a
password-reset / refresh token for the admin and redeem it into an
admin session — a low-trust service credential → full admin
takeover. The password-reset docstring even anticipated a
"compromised service principal" but defended only with a
rate-limit, not a privilege boundary.

Fix: a `_PRIVILEGED_LEVELS = ("admin", "service")` guard in all
three endpoints — refuse the service-caller mint when the target
is privileged, and refuse the grant outright (defense-in-depth).
The existing source-pin convention for these admin endpoints is
followed.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _src(fn) -> str:  # type: ignore[no-untyped-def]
    return inspect.getsource(fn)


# ---------------------------------------------------------------------------
# The shared constant
# ---------------------------------------------------------------------------


def test_privileged_levels_constant() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    assert set(admin._PRIVILEGED_LEVELS) == {"admin", "service"}
    # tuple/frozenset (immutable) so it can't be mutated at runtime.
    assert isinstance(admin._PRIVILEGED_LEVELS, (tuple, frozenset))


# ---------------------------------------------------------------------------
# service_mint_refresh_token  (caller is ALWAYS level=service here)
# ---------------------------------------------------------------------------


def test_refresh_mint_refuses_privileged_target() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = _src(admin.service_mint_refresh_token)
    assert "target.level in _PRIVILEGED_LEVELS" in src
    assert '"reason": "privileged_target"' in src
    assert "service principals may not mint tokens for" in src


def test_refresh_mint_privileged_check_precedes_serviced_by() -> None:
    """The privileged-target refusal MUST run before the
    `serviced_by` membership check — otherwise a stale/mis-granted
    `serviced_by` entry would still let the mint through."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = _src(admin.service_mint_refresh_token)
    priv_idx = src.index("target.level in _PRIVILEGED_LEVELS")
    serviced_idx = src.index("principal.user_id not in target.serviced_by")
    assert priv_idx < serviced_idx, (
        "privileged-target guard must precede (not be gated by) the "
        "serviced_by check"
    )


# ---------------------------------------------------------------------------
# mint_password_reset_token  (require_authenticated; branches on level)
# ---------------------------------------------------------------------------


def test_password_reset_mint_refuses_privileged_target_for_service() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = _src(admin.mint_password_reset_token)
    assert "target.level in _PRIVILEGED_LEVELS" in src
    assert '"reason": "privileged_target"' in src
    assert "service principals may not mint tokens for" in src


def test_password_reset_privileged_guard_is_in_service_branch_only() -> None:
    """The guard belongs to the `elif principal.level == "service":`
    branch. An ADMIN caller must still be able to mint for an
    admin/service target (legitimate operator action) — the guard
    must NOT sit in the admin (`pass`) branch."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = textwrap.dedent(_src(admin.mint_password_reset_token))
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)

    # Find the `if principal.level == "admin": pass / elif
    # principal.level == "service": ...` chain and assert the
    # `_PRIVILEGED_LEVELS` comparison lives inside the service elif,
    # not the admin if-body.
    found_in_service_branch = False
    found_in_admin_branch = False

    def _mentions_privileged(node: ast.AST) -> bool:
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Name) and n.id == "_PRIVILEGED_LEVELS"
            ):
                return True
        return False

    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            test = node.test
            # `principal.level == "admin"`
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Attribute)
                and test.left.attr == "level"
                and isinstance(test.comparators[0], ast.Constant)
            ):
                if test.comparators[0].value == "admin":
                    if any(_mentions_privileged(b) for b in node.body):
                        found_in_admin_branch = True
                    # The `elif service` is the first orelse If node.
                    for orelse in node.orelse:
                        if isinstance(orelse, ast.If) and _mentions_privileged(
                            orelse
                        ):
                            found_in_service_branch = True

    assert found_in_service_branch, (
        "privileged-target guard must be inside the service-caller "
        "branch"
    )
    assert not found_in_admin_branch, (
        "privileged-target guard must NOT restrict the admin caller "
        "branch"
    )


# ---------------------------------------------------------------------------
# grant_serviced_by  (require_admin; defense-in-depth)
# ---------------------------------------------------------------------------


def test_grant_serviced_by_refuses_privileged_target() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = _src(admin.grant_serviced_by)
    assert "target.level in _PRIVILEGED_LEVELS" in src
    # 400, with a message naming the danger.
    assert "serviced_by may not be" in src
    # The guard must precede the actual append.
    guard_idx = src.index("target.level in _PRIVILEGED_LEVELS")
    append_idx = src.index("append_to_serviced_by")
    assert guard_idx < append_idx


# ---------------------------------------------------------------------------
# Regression guard: the original checks are still present
# ---------------------------------------------------------------------------


def test_existing_grantee_and_serviced_by_checks_retained() -> None:
    """The new target-level guard is ADDITIVE — the pre-R9 grantee
    level check and serviced_by membership checks must remain."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    grant_src = _src(admin.grant_serviced_by)
    assert 'svc.level != "service"' in grant_src  # grantee check kept

    refresh_src = _src(admin.service_mint_refresh_token)
    assert "principal.user_id not in target.serviced_by" in refresh_src

    reset_src = _src(admin.mint_password_reset_token)
    assert "target.serviced_by" in reset_src
    assert 'principal.level == "admin"' in reset_src
