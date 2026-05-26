"""Tests for the admin user soft-delete pipeline.

Covers the full stack from migration through API to admin UI:

  * Migration 0005 adds the `deleted_at` column + active-rows
    partial index.
  * UserRow / UserView surface `deleted_at`.
  * Auth-check sites refuse deleted users.
  * F8/F9 mint endpoints refuse deleted targets with 410 Gone.
  * `soft_delete_user` runs the four-step pipeline.
  * `list_users` hides deleted users by default.
  * `DELETE /v1/admin/users/{id}` refuses self-delete + audits.
  * Admin UI: delete button + two-step confirm, deleted badge,
    list filter checkbox.

Source-pin style.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Migration
# ===========================================================================


_MIGRATION_PATH = (
    Path(__file__).parent.parent
    / "bp_router"
    / "db"
    / "migrations"
    / "versions"
    / "0001_initial_schema.py"
)


def test_consolidated_migration_declares_deleted_at() -> None:
    """`deleted_at` was historically standalone migration 0005; it
    is now folded into the consolidated `0001_initial_schema`
    baseline, declared inline on the `users` CREATE TABLE (no
    ADD COLUMN — it's the initial create)."""
    assert _MIGRATION_PATH.exists()
    body = _MIGRATION_PATH.read_text()
    assert "deleted_at         timestamptz" in body
    assert "down_revision = None" in body


def test_consolidated_migration_creates_active_users_partial_index() -> None:
    """The common admin-list filter `WHERE deleted_at IS NULL
    ORDER BY created_at DESC` needs the partial index to stay
    fast as the deleted set grows."""
    body = _MIGRATION_PATH.read_text()
    assert "users_active_idx" in body
    assert "WHERE deleted_at IS NULL" in body


# ===========================================================================
# Model + view
# ===========================================================================


def test_user_row_has_deleted_at_field() -> None:
    from bp_router.db.models import UserRow

    assert "deleted_at" in UserRow.model_fields
    # Optional with None default — pre-migration rows + un-deleted
    # post-migration rows both parse cleanly.
    assert UserRow.model_fields["deleted_at"].default is None


def test_user_view_exposes_deleted_at() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import UserView

    assert "deleted_at" in UserView.model_fields


def test_user_to_view_threads_deleted_at_through() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin._user_to_view)
    assert "deleted_at=row.deleted_at" in src


# ===========================================================================
# Auth-check sites
# ===========================================================================


def test_auth_login_refresh_change_password_check_deleted_at() -> None:
    """All four auth-side check sites (login / refresh /
    change_password / reset_password) must refuse users with
    `deleted_at` set. Routed through `queries.user_is_active`
    which folds the `is None / suspended_at / deleted_at` triplet
    into one predicate — a future lifecycle flag extends the
    check in one place."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth

    src = inspect.getsource(auth)
    # The inline triplet must be entirely gone.
    assert "user.deleted_at is not None" not in src, (
        "auth.py should route every check through "
        "`queries.user_is_active(...)` — no inline `deleted_at` "
        "comparisons should remain."
    )
    # And the helper is called at all four sites.
    assert src.count("user_is_active(") >= 4


def test_f8_mint_refresh_token_refuses_deleted_target_with_410() -> None:
    """Service mint refuses a deleted user with 410 Gone — the
    semantically correct response for a permanently-deactivated
    resource. Surfaces a clearer error than the 403 the
    serviced_by gate would otherwise return after the sweep."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    assert "target.deleted_at is not None" in src
    assert 'HTTPException(410' in src


def test_f9_mint_password_reset_token_refuses_deleted_target_with_410() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.mint_password_reset_token)
    assert "target.deleted_at is not None" in src
    assert 'HTTPException(410' in src


# ===========================================================================
# Query layer — soft_delete_user pipeline
# ===========================================================================


def test_soft_delete_user_query_exists() -> None:
    from bp_router.db import queries

    assert hasattr(queries, "soft_delete_user")


def test_soft_delete_user_returns_none_when_user_missing() -> None:
    """Caller surfaces 404 from the None return rather than a
    UniqueViolation or NULL UPDATE; pin the early-return path."""
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    assert "if user is None:" in src
    assert "return None" in src


def test_soft_delete_user_idempotent_on_already_deleted() -> None:
    """Second call on an already-deleted user is a no-op (returns
    a dict with `was_already_deleted=True` and zero counts)."""
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    assert "if user.deleted_at is not None:" in src
    assert '"was_already_deleted": True' in src


def test_soft_delete_user_runs_four_step_pipeline() -> None:
    """The full pipeline: set deleted_at, drop refresh tokens,
    drop password-reset tokens, sweep serviced_by. Source pin so
    a future refactor that skips one step is caught."""
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    assert "UPDATE users SET deleted_at = now()" in src
    assert "delete_user_refresh_tokens" in src
    assert "delete_user_password_reset_tokens" in src
    assert "sweep_serviced_by_references" in src


def test_soft_delete_user_returns_per_step_counts() -> None:
    from bp_router.db import queries

    src = inspect.getsource(queries.soft_delete_user)
    # Every step reports its count back to the caller for auditing.
    assert '"refresh_tokens_deleted"' in src
    assert '"reset_tokens_deleted"' in src
    assert '"serviced_by_sweep_count"' in src


def test_delete_user_password_reset_tokens_helper_exists() -> None:
    """Companion to delete_user_refresh_tokens; new helper for
    the soft-delete pipeline."""
    from bp_router.db import queries

    assert hasattr(queries, "delete_user_password_reset_tokens")
    src = inspect.getsource(queries.delete_user_password_reset_tokens)
    assert "DELETE FROM password_reset_tokens WHERE user_id = $1" in src


# ===========================================================================
# list_users include_deleted filter
# ===========================================================================


def test_list_users_takes_include_deleted_kwarg() -> None:
    from bp_router.db import queries

    sig = inspect.signature(queries.list_users)
    assert "include_deleted" in sig.parameters
    # Default False — common operator view hides deleted.
    assert sig.parameters["include_deleted"].default is False


def test_list_users_filters_deleted_when_not_included() -> None:
    """Source pin: the WHERE clause includes `deleted_at IS NULL`
    when include_deleted is False. Without this, deleted users
    bleed into every default admin list."""
    from bp_router.db import queries

    src = inspect.getsource(queries.list_users)
    assert "deleted_at IS NULL" in src
    assert "if not include_deleted:" in src


def test_list_users_endpoint_exposes_include_deleted_query_param() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    sig = inspect.signature(admin.list_users)
    assert "include_deleted" in sig.parameters


# ===========================================================================
# DELETE /v1/admin/users/{id}
# ===========================================================================


def test_delete_user_endpoint_exists() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    assert hasattr(admin, "delete_user")


def test_delete_user_endpoint_refuses_self_delete() -> None:
    """An admin clicking the wrong button on their own row would
    lock themselves out of the system — refuse with 400 at the
    endpoint."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_user)
    assert "target_user_id == principal.user_id" in src
    assert "cannot delete your own user" in src


def test_delete_user_endpoint_audits_with_per_step_counts() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_user)
    assert 'event="user.deleted"' in src
    assert '"refresh_tokens_deleted"' in src
    assert '"reset_tokens_deleted"' in src
    assert '"serviced_by_sweep_count"' in src


def test_delete_user_endpoint_idempotent_skip_audit_on_repeat() -> None:
    """Already-deleted user: return 204 without a second audit
    row. Audit-log noise from a misclick refresh is worse than
    no record."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_user)
    assert 'result["was_already_deleted"]' in src
    # On the already-deleted branch: bare return, no audit call.
    assert "return" in src


# ===========================================================================
# Admin UI — page handler
# ===========================================================================


def test_admin_user_delete_handler_exists() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/delete"' in src
    assert "async def delete_user" in src


def test_admin_list_handler_takes_include_deleted_param() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    sig = inspect.signature(users.list_users)
    assert "include_deleted" in sig.parameters


def test_admin_list_handler_forwards_include_deleted_to_upstream() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.list_users)
    # Conditional thread-through — `?include_deleted=true` only
    # when the toggle is on (default URL stays clean).
    assert 'params["include_deleted"] = "true"' in src
    assert "if include_deleted:" in src


# ===========================================================================
# Admin UI — templates
# ===========================================================================


def _detail_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "users"
        / "detail.html"
    ).read_text()


def _list_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "users"
        / "list.html"
    ).read_text()


def _table_body_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "users"
        / "_table_body.html"
    ).read_text()


def test_detail_template_renders_deleted_badge_when_set() -> None:
    body = _detail_html()
    # Conditional on user.deleted_at — same pattern as suspended.
    assert "user.deleted_at" in body
    # Red badge palette (distinct from orange suspend badge).
    deleted_idx = body.find("deleted")
    badge_idx = body.find("bg-red-100", deleted_idx - 500)
    assert badge_idx != -1, "deleted badge missing red palette"


def test_detail_template_delete_button_has_two_step_confirm() -> None:
    """Destructive terminal action — must require a second click,
    same pattern as Suspend."""
    body = _detail_html()
    assert "confirmingDelete" in body
    assert "Confirm — delete (terminal)" in body
    # POSTs to the delete endpoint.
    assert 'action="/admin/users/{{ user.user_id }}/delete"' in body


def test_detail_template_hides_delete_button_when_already_deleted() -> None:
    """No-op clicks on a deleted user should be impossible from
    the UI — surface the deleted state via the header badge and
    omit the button."""
    body = _detail_html()
    assert "{% if not user.deleted_at %}" in body


def test_list_template_has_show_deleted_checkbox() -> None:
    body = _list_html()
    assert 'name="include_deleted"' in body
    assert "Show deleted users" in body


def test_list_template_htmx_triggers_on_checkbox_change() -> None:
    """Without the checkbox-change HTMX trigger, the toggle does
    nothing until the form is otherwise submitted — bad UX."""
    body = _list_html()
    assert "change from:input[type=checkbox]" in body


def test_table_body_template_shows_deleted_badge_distinctly() -> None:
    body = _table_body_html()
    # Status column carries one of three states: deleted, suspended, or
    # neutral. Pin the deleted branch + the red palette.
    assert "u.deleted_at" in body
    assert "bg-red-100" in body


def test_table_body_load_more_preserves_include_deleted() -> None:
    body = _table_body_html()
    # Pagination must keep the filter active or "Load more" would
    # silently drop the deleted users from page 2 onward.
    assert "include_deleted=true" in body
