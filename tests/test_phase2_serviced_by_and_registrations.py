"""Tests for Phase 2: F7 (registration queue) + F8 (serviced_by) +
F11.serviced_by field.

These tests are shape-and-source-pin tests; they don't spin up the
full FastAPI app. Behaviour that crosses Postgres (the asyncpg
codec, the array operators) is exercised in integration-test land,
not here.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

# ===========================================================================
# F8 — UserRow.serviced_by field + insert_user signature
# ===========================================================================


def test_user_row_has_serviced_by_field() -> None:
    from bp_router.db.models import UserRow

    fields = UserRow.model_fields
    assert "serviced_by" in fields
    # Default is empty list — default-deny.
    assert fields["serviced_by"].default == []


def test_insert_user_accepts_serviced_by_kwarg() -> None:
    from bp_router.db import queries

    sig = inspect.signature(queries.insert_user)
    assert "serviced_by" in sig.parameters
    assert sig.parameters["serviced_by"].default is None


def test_insert_user_inserts_serviced_by_column() -> None:
    """Source pin: the INSERT statement must include the serviced_by
    column so a F8 auto-grant from the registration approve handler
    actually lands."""
    src = inspect.getsource(__import__("bp_router.db.queries", fromlist=["insert_user"]).insert_user)
    assert "serviced_by" in src
    # Default to '[]' (empty array) when caller passes None.
    assert "serviced_by or []" in src


def test_append_to_serviced_by_is_idempotent() -> None:
    """Source pin: the UPDATE clause MUST be idempotent — re-granting
    the same service principal must not double-append."""
    from bp_router.db import queries

    src = inspect.getsource(queries.append_to_serviced_by)
    assert "ANY(serviced_by)" in src
    assert "array_append" in src


def test_remove_from_serviced_by_uses_array_remove() -> None:
    from bp_router.db import queries

    src = inspect.getsource(queries.remove_from_serviced_by)
    assert "array_remove" in src


def test_sweep_serviced_by_references_clears_all_users() -> None:
    """Source pin: a service-user delete should sweep every
    `serviced_by` array — the cleanup helper does an
    `array_remove` over `WHERE $1 = ANY(serviced_by)`."""
    from bp_router.db import queries

    src = inspect.getsource(queries.sweep_serviced_by_references)
    assert "array_remove(serviced_by, $1)" in src
    assert "ANY(serviced_by)" in src


# ===========================================================================
# F8 — Admin endpoints: mint, grant, revoke, kill refresh tokens
# ===========================================================================


def test_service_mint_refresh_token_handler_validates_serviced_by() -> None:
    """The mint endpoint MUST check the caller is in
    `target_user.serviced_by` before issuing. The denial path audits
    `auth.refresh_token_mint_denied` with reason."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.service_mint_refresh_token)
    assert "principal.user_id not in target.serviced_by" in src
    assert "auth.refresh_token_mint_denied" in src
    assert '"reason": "not_serviced_by"' in src


def test_service_mint_refresh_token_requires_service_principal() -> None:
    """Source pin: the endpoint dependency uses `require_service`,
    not `require_admin`."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin
    from bp_router.security.jwt import require_service

    sig = inspect.signature(admin.service_mint_refresh_token)
    principal_default = sig.parameters["principal"].default
    # FastAPI Depends() carries the dependency in .dependency.
    assert principal_default.dependency is require_service


def test_grant_serviced_by_validates_grantee_is_service_level() -> None:
    """Source pin: the grant endpoint enforces the F8.2 invariant —
    only level=service users can be entries in serviced_by."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.grant_serviced_by)
    assert 'svc.level != "service"' in src


def test_revoke_serviced_by_uses_remove_helper() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.revoke_serviced_by)
    assert "remove_from_serviced_by" in src


def test_revoke_user_refresh_tokens_endpoint_exists() -> None:
    """F8 companion: removing a service principal from serviced_by
    does NOT invalidate already-minted refresh tokens. The
    `DELETE .../refresh-tokens` endpoint is what does."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    assert hasattr(admin, "revoke_user_refresh_tokens")
    src = inspect.getsource(admin.revoke_user_refresh_tokens)
    assert "delete_user_refresh_tokens" in src


# ===========================================================================
# F7 — POST /v1/registrations
# ===========================================================================


def test_registration_submit_request_channel_grammar() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.registrations import RegistrationSubmitRequest

    # Valid channels.
    for ch in ("telegram", "discord", "sms-twilio", "x", "web_ui_v1"):
        req = RegistrationSubmitRequest(channel=ch, external_id="abc")
        assert req.channel == ch

    # Invalid: starts with digit, too long, uppercase, has spaces.
    for bad in ("1bad", "BAD", "with space", "x" * 33, "no!special"):
        with pytest.raises(ValidationError):
            RegistrationSubmitRequest(channel=bad, external_id="abc")


def test_submit_registration_captures_only_service_principal_id() -> None:
    """Only level=service callers get their user_id recorded as
    `submitted_by_service_user_id` on the pending row. Admin /
    regular-user submissions don't auto-grant servicing rights."""
    pytest.importorskip("fastapi")
    from bp_router.api import registrations

    src = inspect.getsource(registrations.submit_registration)
    assert 'principal.level == "service"' in src
    assert "submitted_by_service_user_id" in src


def test_submit_registration_rate_limits_per_channel_external_id() -> None:
    """Source pin: rate-limit bucket key is
    `registration:<channel>:<external_id>`, NOT `…:<user_id>` or
    a global key. Per-chat retry storms get throttled; well-behaved
    callers don't share buckets across chats."""
    pytest.importorskip("fastapi")
    from bp_router.api import registrations

    src = inspect.getsource(registrations.submit_registration)
    assert 'f"registration:{req.channel}:{req.external_id}"' in src


def test_submit_registration_audits_rate_limited() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import registrations

    src = inspect.getsource(registrations.submit_registration)
    assert "registration.rate_limited" in src


def test_registrations_router_mounted_under_v1_registrations() -> None:
    """Source pin: app.py mounts the router at /v1/registrations
    (not /v1/admin/...) — submit is user-facing, not admin-only."""
    from bp_router import app as app_module

    src = inspect.getsource(app_module.create_app)
    assert 'prefix="/v1/registrations"' in src


# ===========================================================================
# F7 — Admin approve/reject
# ===========================================================================


def test_approve_registration_auto_grants_serviced_by_from_submitter() -> None:
    """F7×F8 integration: approve handler reads
    `pending.submitted_by_service_user_id` and seeds
    `users.serviced_by` with it. No separate admin grant step
    needed for channel-agent-submitted users."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.approve_registration)
    assert "submitted_by_service_user_id" in src
    assert "serviced_by=[submitter] if submitter else []" in src
    # Audit row exposes who got the auto-grant.
    assert '"auto_serviced_by": submitter' in src


def test_approve_registration_holds_for_update() -> None:
    """Two concurrent admin approvals on the same registration_id
    must serialise. Source pin on the FOR UPDATE clause."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.approve_registration)
    assert "FOR UPDATE" in src


def test_approve_registration_opens_initial_session_with_generic_label() -> None:
    """Source pin: the session label is built from `pending['channel']`
    and a timestamp — NOT a Telegram-specific `chat_<chat_id>` string.
    The framework is channel-agnostic."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.approve_registration)
    # No hardcoded channel-specific labels.
    assert "chat_" not in src.lower() or "chat_id" in src.lower()  # allow comments
    # The label fallback uses the generic format.
    assert "pending['channel']" in src or "pending[\"channel\"]" in src


def test_reject_registration_deletes_row_and_audits() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.reject_registration)
    assert "delete_pending_registration" in src
    assert "registration.rejected" in src


# ===========================================================================
# F11 — serviced_by field on CreateUserRequest
# ===========================================================================


def test_create_user_request_accepts_serviced_by() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import CreateUserRequest

    req = CreateUserRequest(
        email="alice@example.com",
        level="tier0",
        serviced_by=["usr_svc_telegram", "usr_svc_discord"],
    )
    assert req.serviced_by == ["usr_svc_telegram", "usr_svc_discord"]


def test_create_user_request_serviced_by_defaults_none() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import CreateUserRequest

    req = CreateUserRequest(email="alice@example.com", level="tier0")
    assert req.serviced_by is None


def test_create_user_handler_validates_serviced_by_entries() -> None:
    """Source pin: each entry MUST resolve to a level=service user.
    The pre-validate runs OUTSIDE the create transaction so an
    invalid entry surfaces as a typed 400/404 instead of crashing
    the insert."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.create_user)
    assert "for svc_id in req.serviced_by:" in src
    assert 'svc.level != "service"' in src


def test_create_user_handler_audits_serviced_by_on_create() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.create_user)
    assert '"serviced_by": req.serviced_by or []' in src


# ===========================================================================
# Settings — registration rate-limit knobs
# ===========================================================================


def test_settings_has_registration_rate_limit_fields() -> None:
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "registration_rate_limit_per_external_per_s" in fields
    assert "registration_rate_limit_per_external_burst" in fields
