"""Tests for the wire-contract cleanup bundle (review M1, M10, M11,
M13, M14):

  M1  — admin PATCH on llm_presets must not silently clear an
        inline `api_key` when the request sends `api_key=""` without
        the explicit `clear_api_key=true` flag.
  M10 — SDK `embed()` default model must point at an embeddings preset
        (`text-embedding-3-small`), not the chat-side `default` which
        raises NotImplementedError on `embed()`.
  M11 — `ErrorCode` enum carries the LLM-call codes (`preset_unknown`,
        `preset_not_allowed`, `auth_lookup_failed`) so dispatch can
        reference constants instead of bare strings.
  M13 — `_PRESET_PATCHABLE` (admin API) and `_PRESET_PATCHABLE_COLUMNS`
        (queries) must be a single source of truth. Same for
        `PROVIDERS` (admin UI) and `SUPPORTED_PROVIDERS` (presets).
  M14 — Audit event names use the `<entity>.<action>` convention
        consistently. The two `admin.*` outliers from earlier PRs
        get renamed to entity-prefixed forms.
"""

from __future__ import annotations

import inspect

import pytest

# ---------------------------------------------------------------------------
# M11 — ErrorCode enum carries the LLM error codes
# ---------------------------------------------------------------------------


def test_error_code_enum_has_llm_preset_codes() -> None:
    from bp_protocol.frames import ErrorCode

    assert ErrorCode.LLM_PRESET_UNKNOWN == "preset_unknown"
    assert ErrorCode.LLM_PRESET_NOT_ALLOWED == "preset_not_allowed"
    assert ErrorCode.LLM_AUTH_LOOKUP_FAILED == "auth_lookup_failed"


def test_dispatch_uses_error_code_constants_not_bare_strings() -> None:
    """Dispatch must reference the enum, not duplicate the literals.
    Catches a regression where someone copies the string instead of
    importing from the enum (and the two then drift)."""
    from bp_router import dispatch

    src = inspect.getsource(dispatch)
    # The constants land in the source; the bare-string forms should
    # NOT be used as `code=` arguments anymore.
    assert "ErrorCode.LLM_PRESET_UNKNOWN" in src
    assert "ErrorCode.LLM_PRESET_NOT_ALLOWED" in src
    assert "ErrorCode.LLM_AUTH_LOOKUP_FAILED" in src
    # No bare-string uses of the LLM error codes.
    assert 'code="preset_unknown"' not in src
    assert 'code="preset_not_allowed"' not in src
    assert 'code="auth_lookup_failed"' not in src


# ---------------------------------------------------------------------------
# M13 — single source of truth for patchable columns + providers
# ---------------------------------------------------------------------------


def test_admin_preset_patchable_imports_from_queries() -> None:
    """Admin API and queries module must share the same frozenset.
    A separate definition let the two drift previously."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841

    from bp_router.api import admin
    from bp_router.db import queries

    # `_PRESET_PATCHABLE` in admin should BE the queries-side constant
    # (re-exported via import alias), not a separately-defined copy.
    assert admin._PRESET_PATCHABLE is queries._PRESET_PATCHABLE_COLUMNS


def test_admin_ui_providers_imports_from_router() -> None:
    """Admin UI dropdown is driven by the router's authoritative list,
    so a future provider added to `SUPPORTED_PROVIDERS` immediately
    surfaces in the dropdown without manual duplication."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841

    from bp_admin.pages import llm_presets as admin_ui
    from bp_router.llm.presets import (
        PROVIDERS_REQUIRING_BASE_URL,
        SUPPORTED_PROVIDERS,
    )

    assert tuple(admin_ui.PROVIDERS) == SUPPORTED_PROVIDERS
    assert admin_ui.PROVIDERS_WITH_BASE_URL is PROVIDERS_REQUIRING_BASE_URL


# ---------------------------------------------------------------------------
# M14 — audit event naming convention
# ---------------------------------------------------------------------------


def test_audit_events_use_entity_action_naming() -> None:
    """No `event="admin.<...>"` strings in the codebase. Convention is
    `<entity>.<action>` (e.g., `invitation.issued` not
    `admin.invitation_issued`).
    """
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_router.api import admin

    src = inspect.getsource(admin)

    # These two were the outliers from the review.
    assert 'event="admin.invitation_issued"' not in src
    assert 'event="admin.task_test"' not in src

    # And the renamed forms ARE present.
    assert 'event="invitation.issued"' in src
    assert 'event="task.test_dispatched"' in src


def test_audit_event_naming_in_security_doc_matches_code() -> None:
    """The doc table in `docs/security.md` must reflect the renamed
    events, not the old ones — otherwise readers get confused about
    which strings will appear in production audit logs."""
    with open(
        "/home/user/backplaned-next/docs/security.md", encoding="utf-8"
    ) as f:
        doc = f.read()

    assert "admin.invitation_issued" not in doc
    assert "admin.task_test" not in doc
    assert "invitation.issued" in doc
    assert "task.test_dispatched" in doc


# ---------------------------------------------------------------------------
# M1 — empty-string api_key doesn't clear the inline secret
# ---------------------------------------------------------------------------


def test_admin_patch_drops_empty_api_key_without_clear_flag() -> None:
    """Source check: the PATCH handler must drop `api_key=""` from the
    raw payload before the truthy mask check so it can't silently null
    out the column without `clear_api_key=true`."""
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from bp_router.api import admin

    src = inspect.getsource(admin.update_llm_preset)

    # The empty-string guard must run BEFORE the `if clear_api_key:`
    # branch (otherwise the explicit-flag check fires on a payload
    # that's already had the string normalised).
    lines = src.split("\n")
    drop_line = next(
        (i for i, l in enumerate(lines) if 'raw.get("api_key") == ""' in l),
        None,
    )
    flag_line = next(
        (i for i, l in enumerate(lines) if "if clear_api_key:" in l),
        None,
    )
    assert drop_line is not None, (
        "PATCH handler must explicitly drop empty-string api_key"
    )
    assert flag_line is not None
    assert drop_line < flag_line, (
        "Empty-string normalisation must run before clear_api_key check"
    )


# ---------------------------------------------------------------------------
# M10 — SDK embed() default model
# ---------------------------------------------------------------------------


def test_sdk_embed_default_model_is_embeddings_preset() -> None:
    """`embed()` defaults must route to an embeddings adapter, not
    the chat-side `default` (which would NotImplementedError)."""
    from bp_sdk.llm import LlmServiceClient

    sig = inspect.signature(LlmServiceClient.embed)
    model_default = sig.parameters["model"].default
    assert model_default == "text-embedding-3-small", (
        f"embed() defaults to {model_default!r}; must point at an "
        "embeddings preset"
    )


def test_sdk_generate_default_model_is_chat_preset() -> None:
    """Sanity: `generate()` keeps `default` (which routes to a chat
    preset). Only `embed()` got moved."""
    from bp_sdk.llm import LlmServiceClient

    sig = inspect.signature(LlmServiceClient.generate)
    model_default = sig.parameters["model"].default
    assert model_default == "default"


def test_sdk_count_tokens_default_unchanged() -> None:
    """`count_tokens()` defaults to `default` because chat presets
    expose token counters; embeddings adapters don't."""
    from bp_sdk.llm import LlmServiceClient

    sig = inspect.signature(LlmServiceClient.count_tokens)
    model_default = sig.parameters["model"].default
    assert model_default == "default"
