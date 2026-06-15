"""Tests for the admin-UI registrations queue (Phase 9a).

Source-pin style — same shape as `test_llm_presets_admin_ui.py`.
Covers route mounting, nav-link wiring, form pre-fill semantics,
error-path re-render, and the explicit-dismiss approval flow.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Mount + nav
# ===========================================================================


def test_admin_app_mounts_registrations_router() -> None:
    """Source pin: registrations router included under
    /registrations and imported at module top."""
    pytest.importorskip("fastapi")
    from bp_admin import app as app_module

    src = inspect.getsource(app_module)
    assert "registrations," in src.split("from bp_admin.pages import", 1)[1].split(")", 1)[0]
    assert 'registrations.router, prefix="/registrations"' in src


def test_admin_nav_has_registrations_section() -> None:
    """Source pin: base.html nav sections list includes the
    registrations entry. Without this, the page is reachable by URL
    but invisible in the sidebar — the upstream-bug pattern from
    earlier."""
    from pathlib import Path

    base = (
        Path(__file__).parent.parent / "bp_admin" / "templates" / "base.html"
    ).read_text()
    assert '("registrations"' in base
    assert "/admin/registrations" in base


# ===========================================================================
# Module-level helpers
# ===========================================================================


def test_generate_initial_password_yields_url_safe_token() -> None:
    """At least 16 chars URL-safe — strong default the admin can
    override before submitting."""
    pytest.importorskip("fastapi")
    from bp_admin.pages.registrations import _generate_initial_password

    pw = _generate_initial_password()
    assert len(pw) >= 16
    # secrets.token_urlsafe alphabet — `[A-Za-z0-9_-]`.
    assert all(c.isalnum() or c in "-_" for c in pw)
    # Each call yields a fresh value (no module-level cache).
    assert _generate_initial_password() != pw


def test_generate_initial_password_uses_secrets_module() -> None:
    """Pin the cryptographic source so a future refactor to
    `random.choice(...)` is caught."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations._generate_initial_password)
    assert "secrets.token_urlsafe" in src


# ===========================================================================
# Approve handler — pre-fill + error redisplay + success reveal
# ===========================================================================


def test_registration_detail_pre_fills_email_from_requested_email() -> None:
    """Source pin: detail handler seeds the email input from
    `requested_email` on the pending row, falling back to empty
    string when None."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.registration_detail)
    assert 'row.get("requested_email") or ""' in src


def test_registration_detail_pre_fills_default_level_tier0() -> None:
    """Matches the API's `level="tier0"` default. Operators get a
    sensible starting point; high-trust levels (admin, service)
    require an explicit pick — guards against accidentally
    promoting a channel-submitted user."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.registration_detail)
    assert '"level": "tier0"' in src


def test_registration_detail_pre_fills_auto_generated_password() -> None:
    """The whole reason this page exists as an override form rather
    than a one-click button: admin gets a strong default they can
    edit. Source pin on the call site."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.registration_detail)
    assert "_generate_initial_password()" in src


def test_approve_handler_redisplays_form_on_upstream_error() -> None:
    """If POST /v1/admin/registrations/{id}/approve fails (e.g. 409
    email exists), the admin must NOT lose the edits they made —
    re-render the override form with the submitted values + an
    error banner."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.approve_registration)
    assert "except UpstreamError" in src
    # Form values forwarded back into the re-render so edits survive.
    assert '"email": email' in src
    assert '"level": level' in src
    assert '"initial_password": initial_password' in src
    assert '"label": label' in src
    assert '"error": detail_message(exc)' in src


def test_approve_handler_renders_approved_template_on_success() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.approve_registration)
    assert '"registrations/approved.html"' in src
    assert '"result": body' in src


def test_approve_handler_passes_user_inputs_unchanged_to_upstream() -> None:
    """Critical: the upstream POST body must reflect what the admin
    typed, NOT the original pre-fill from the detail page. Pinning
    this so a future refactor that "re-generates the password
    server-side at submit time" is caught."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.approve_registration)
    # The payload dict reads from Form() parameters directly, not
    # from `_generate_initial_password()` or any pending-row field.
    assert '"email": email' in src
    assert '"level": level' in src
    assert '"initial_password": initial_password' in src
    assert "_generate_initial_password()" not in src


def test_approve_handler_omits_blank_label_from_payload() -> None:
    """Optional field. Empty string would override the API's
    default `<channel> · <timestamp>` label with the literal empty
    string — undesirable. Source pin on the `if label.strip()`
    guard."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.approve_registration)
    assert "label.strip()" in src
    assert 'payload["label"] = label.strip()' in src


# ===========================================================================
# Reject handler
# ===========================================================================


def test_reject_handler_calls_upstream_reject_endpoint() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.reject_registration)
    assert "/registrations/{registration_id}/reject" in src
    assert 'redirect_with_flash(' in src
    assert '"registration rejected"' in src


# ===========================================================================
# Template — explicit-dismiss reveal
# ===========================================================================


def test_approved_template_has_acknowledgement_checkbox() -> None:
    """The reveal page must require an explicit acknowledgement
    before letting admin leave. Without the checkbox, an admin
    could click straight through and miss the one-time password —
    the design decision we agreed on for this PR."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "approved.html"
    ).read_text()
    assert 'x-model="acknowledged"' in body
    assert "I have copied the initial password" in body


def test_approved_template_disables_navigation_until_acknowledged() -> None:
    """Pointer-events guard on the navigation links — they appear
    grayed out and don't accept clicks until the checkbox is
    ticked. Defence in depth on top of the visual cue."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "approved.html"
    ).read_text()
    assert "pointer-events-none" in body
    # And the guard's condition is the acknowledged flag.
    assert "acknowledged ?" in body or "acknowledged\n" in body or "x-bind:class" in body


def test_approved_template_renders_initial_password_in_monospace() -> None:
    """The password is the user-visible secret. Render it as
    selectable monospace (consistent with the invitation issued
    page) and provide a copy-to-clipboard helper."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "approved.html"
    ).read_text()
    assert "result.initial_password" in body
    assert "font-mono" in body
    assert "navigator.clipboard.writeText" in body


def test_detail_template_has_override_form_fields() -> None:
    """Spot-check that the four override fields are present and
    pointed at the approve endpoint."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "detail.html"
    ).read_text()
    for field in ("email", "level", "initial_password", "label"):
        assert f'name="{field}"' in body, f"missing form field {field!r}"
    assert "/approve" in body
    # Reject button on the same page too (secondary action).
    assert "/reject" in body


def test_detail_template_explains_serviced_by_auto_grant() -> None:
    """When the pending row has `submitted_by_service_user_id`,
    approval auto-grants servicing rights. Admin needs to see this
    BEFORE clicking approve."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "detail.html"
    ).read_text()
    assert "submitted_by_service_user_id" in body
    assert "serviced_by" in body


def test_list_template_filter_uses_htmx_partial_swap() -> None:
    """Channel-filter form uses HTMX `hx-target` to swap just the
    table body — matches the invitations / users / agents pattern.
    Without `hx-push-url`, deep-linking to a filtered view
    breaks."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "list.html"
    ).read_text()
    assert "hx-get=" in body
    assert "registrations-table" in body
    assert "hx-push-url" in body


# ===========================================================================
# End-to-end smoke through the boot path
# ===========================================================================


def _build_admin_app():
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
    return app


def test_admin_login_page_renders_registrations_nav_link() -> None:
    """The login page renders base.html in unauthenticated context,
    which means the nav-link literal appears in the response. We
    don't render the FULL nav for unauthed visitors, but the
    sections-list constant is reachable via the template — pin the
    boot path instead by checking that the app's route table
    includes a /registrations entry."""
    pytest.importorskip("fastapi")

    app = _build_admin_app()
    # fastapi >=0.137 lazily wraps included routers in app.routes
    # (_IncludedRouter), so individual routes are no longer flattened there.
    # openapi()["paths"] reflects every registered path on both old and new
    # fastapi — the version-stable way to assert route registration.
    paths = set(app.openapi()["paths"])
    # The exact paths under /registrations:
    assert "/registrations" in paths
    assert "/registrations/{registration_id}" in paths
    assert "/registrations/{registration_id}/approve" in paths
    assert "/registrations/{registration_id}/reject" in paths
