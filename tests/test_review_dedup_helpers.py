"""Dedup helpers — `extract_bearer` and `user_is_active`.

R2 cleanup: the bearer-extract idiom
`if not auth.lower().startswith("bearer "): ... auth[len("bearer "):].strip()`
appeared 3× across `bp_router/security/jwt.py`,
`bp_router/api/onboard.py`, and `bp_router/api/health.py`. The
soft-delete check
`user is None or user.suspended_at is not None or user.deleted_at is not None`
appeared 4× across `bp_router/api/auth.py` (login / refresh /
change_password / reset_password).

Both helpers now live in a single canonical place. These tests
pin the helpers' contracts and confirm every former duplicate
site calls the helper.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

# ===========================================================================
# extract_bearer
# ===========================================================================


def test_extract_bearer_returns_token_on_standard_header() -> None:
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("Bearer abc.def.ghi") == "abc.def.ghi"


def test_extract_bearer_is_case_insensitive_on_keyword() -> None:
    """Matches the existing call-site shape (auth.lower().startswith)
    which tolerated `bearer` / `BEARER` / `Bearer`."""
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("bearer abc") == "abc"
    assert extract_bearer("BEARER abc") == "abc"
    assert extract_bearer("BeArEr abc") == "abc"


def test_extract_bearer_strips_whitespace_around_token() -> None:
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("Bearer    abc   ") == "abc"


def test_extract_bearer_returns_none_on_missing_header() -> None:
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("") is None


def test_extract_bearer_returns_none_on_wrong_scheme() -> None:
    """Basic auth / Digest / arbitrary tokens are NOT bearers."""
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("Basic dXNlcjpwYXNz") is None
    assert extract_bearer("xyz abc") is None


def test_extract_bearer_returns_none_on_empty_token() -> None:
    """`Bearer ` with nothing after it is not a credential."""
    from bp_router.security.jwt import extract_bearer

    assert extract_bearer("Bearer ") is None
    assert extract_bearer("Bearer    ") is None


def test_extract_bearer_used_in_principal_from_request() -> None:
    """Source pin: `_principal_from_request` calls the helper
    instead of the inline shape."""
    pytest.importorskip("fastapi")
    from bp_router.security import jwt

    src = inspect.getsource(jwt._principal_from_request)
    assert "extract_bearer(" in src
    # And the inline idiom is gone.
    assert 'auth.lower().startswith("bearer ")' not in src


def test_extract_bearer_used_in_onboard_refresh() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import onboard

    src = inspect.getsource(onboard.refresh_agent_token)
    assert "extract_bearer(" in src
    assert 'authorization.lower().startswith("bearer ")' not in src


def test_extract_bearer_used_in_metrics_endpoint() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import health

    src = inspect.getsource(health.metrics)
    assert "extract_bearer(" in src
    assert 'auth.lower().startswith("bearer ")' not in src


# ===========================================================================
# user_is_active
# ===========================================================================


def test_user_is_active_true_for_normal_user() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import user_is_active

    user = MagicMock()
    user.suspended_at = None
    user.deleted_at = None
    assert user_is_active(user) is True


def test_user_is_active_false_when_user_none() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import user_is_active

    assert user_is_active(None) is False


def test_user_is_active_false_when_suspended() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import user_is_active

    user = MagicMock()
    user.suspended_at = datetime.now(UTC)
    user.deleted_at = None
    assert user_is_active(user) is False


def test_user_is_active_false_when_deleted() -> None:
    pytest.importorskip("asyncpg")
    from bp_router.db.queries import user_is_active

    user = MagicMock()
    user.suspended_at = None
    user.deleted_at = datetime.now(UTC)
    assert user_is_active(user) is False


def test_user_is_active_used_in_all_auth_sites() -> None:
    """Source pin: every former duplicate of the triplet is now
    `not queries.user_is_active(user)`. A future check site that
    forgets the helper will fail this pin."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth)
    # The inline triplet must be entirely gone from auth.py.
    assert (
        "user is None or user.suspended_at is not None or user.deleted_at is not None"
        not in src
    ), (
        "auth.py still has the inline triplet — replace with "
        "`not queries.user_is_active(user)` to keep the predicate "
        "centralised."
    )
    # And the helper is called at least 4× (login / refresh /
    # change_password / reset_password).
    assert src.count("user_is_active(") >= 4
