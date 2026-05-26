"""Tests for Phase 9b — user-detail credential management UI.

Three new surfaces on the user-detail page:
  * `serviced_by` membership panel — list, grant, revoke
  * Refresh-token kill button — two-step confirm
  * Password-reset mint button — one-time token reveal with
    explicit-dismiss acknowledgement

Source-pin style — same shape as the registrations admin UI
tests.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Route registration
# ===========================================================================


def test_users_router_has_serviced_by_grant_route() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/serviced-by"' in src
    assert "async def grant_serviced_by" in src


def test_users_router_has_serviced_by_revoke_route() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/serviced-by/{service_user_id}/revoke"' in src
    assert "async def revoke_serviced_by" in src


def test_users_router_has_refresh_token_revoke_route() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/refresh-tokens/revoke"' in src
    assert "async def revoke_user_refresh_tokens" in src


def test_users_router_has_password_reset_mint_route() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/password-reset"' in src
    assert "async def mint_password_reset_token" in src


def test_user_routes_registered_on_admin_app() -> None:
    """Boot-time confirmation that all four new routes land at the
    paths the templates point to."""
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr

    from bp_admin.app import create_app
    from bp_admin.config import AdminConfig
    from bp_admin.upstream import UpstreamClient

    cfg = AdminConfig(
        router_url="http://127.0.0.1:0",
        session_secret=SecretStr("x" * 32),
    )
    app = create_app(cfg)
    app.state.upstream = UpstreamClient(
        cfg.router_url, timeout_s=cfg.upstream_timeout_s
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/users/{user_id}/serviced-by" in paths
    assert "/users/{user_id}/serviced-by/{service_user_id}/revoke" in paths
    assert "/users/{user_id}/refresh-tokens/revoke" in paths
    assert "/users/{user_id}/password-reset" in paths


# ===========================================================================
# Upstream endpoint wiring
# ===========================================================================


def test_grant_serviced_by_calls_upstream_put() -> None:
    """Source pin: wraps PUT /v1/admin/users/{id}/serviced-by/{svc_id}
    — not POST. The router endpoint is PUT for idempotent semantics."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.grant_serviced_by)
    assert '"PUT"' in src
    assert "/users/{user_id}/serviced-by/{svc}" in src


def test_grant_serviced_by_rejects_empty_input() -> None:
    """UI-side guard: empty service_user_id shouldn't hit the
    upstream at all. Flash + redirect instead."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.grant_serviced_by)
    assert "if not svc:" in src
    assert "service_user_id is required" in src


def test_revoke_serviced_by_calls_upstream_delete() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.revoke_serviced_by)
    assert '"DELETE"' in src
    assert "/users/{user_id}/serviced-by/{service_user_id}" in src


def test_revoke_user_refresh_tokens_calls_upstream_delete() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.revoke_user_refresh_tokens)
    assert '"DELETE"' in src
    assert "/users/{user_id}/refresh-tokens" in src


def test_mint_password_reset_token_calls_upstream_post() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.mint_password_reset_token)
    assert '"POST"' in src
    assert "/users/{user_id}/password-reset-tokens" in src


def test_mint_renders_reveal_template_on_success() -> None:
    """One-time reveal — token plaintext goes to a dedicated
    template, NOT a flash redirect (a flash would log the token to
    the URL bar and lose the dismissal gate)."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users.mint_password_reset_token)
    assert '"users/password_reset_minted.html"' in src
    assert '"reset_token": body["reset_token"]' in src
    assert '"expires_at": body["expires_at"]' in src


# ===========================================================================
# Template — user-detail extensions
# ===========================================================================


def _detail_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "users"
        / "detail.html"
    ).read_text()


def test_detail_template_renders_serviced_by_list() -> None:
    body = _detail_html()
    assert "Serviced by" in body
    # Iterates user.serviced_by entries.
    assert "for svc in user.serviced_by" in body


def test_detail_template_shows_default_deny_message_when_empty() -> None:
    body = _detail_html()
    assert "Default-deny" in body


def test_detail_template_grant_form_targets_correct_route() -> None:
    body = _detail_html()
    assert 'action="/admin/users/{{ user.user_id }}/serviced-by"' in body
    assert 'name="service_user_id"' in body
    assert 'pattern="usr_[A-Za-z0-9_-]+"' in body


def test_detail_template_revoke_warning_mentions_refresh_tokens() -> None:
    """Critical UX cue: the revoke-confirmation prompt must tell
    the admin that already-minted refresh tokens stay valid until
    the separate kill button is clicked."""
    body = _detail_html()
    assert "Already-minted refresh tokens stay valid" in body


def test_detail_template_password_reset_mint_button() -> None:
    body = _detail_html()
    assert 'action="/admin/users/{{ user.user_id }}/password-reset"' in body
    assert "Mint password-reset token" in body


def test_detail_template_refresh_token_kill_has_two_step_confirm() -> None:
    """Destructive cross-device action — must require an explicit
    second click, not a single submission. Matches the suspend-user
    pattern higher up the page."""
    body = _detail_html()
    assert "confirmingRevoke" in body
    assert "Confirm — log out everywhere" in body


# ===========================================================================
# Template — password-reset reveal
# ===========================================================================


def _reveal_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "users"
        / "password_reset_minted.html"
    ).read_text()


def test_reveal_template_has_acknowledgement_checkbox() -> None:
    """The reveal page must require explicit ack before navigation,
    same pattern as registrations/approved.html."""
    body = _reveal_html()
    assert 'x-model="acknowledged"' in body
    # Acknowledgement copy contains the "deliver securely" intent.
    # Normalise whitespace so the test isn't tripped by template
    # line-breaks inside the label text.
    normalised = " ".join(body.split())
    assert "deliver it to the user securely" in normalised


def test_reveal_template_disables_navigation_until_acknowledged() -> None:
    body = _reveal_html()
    assert "pointer-events-none" in body
    assert "x-bind:class" in body


def test_reveal_template_renders_token_in_monospace_with_copy() -> None:
    body = _reveal_html()
    assert "reset_token" in body
    assert "font-mono" in body
    assert "navigator.clipboard.writeText" in body


def test_reveal_template_breadcrumbs_back_to_user_detail() -> None:
    """Breadcrumb chain: Users › user_id › Password-reset token.
    Lets the operator orient themselves before / after the dismiss."""
    body = _reveal_html()
    assert "/admin/users/{{ user_id }}" in body
    assert "Back to user" in body
