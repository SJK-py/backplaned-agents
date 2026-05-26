"""Tests for two admin-UI enhancements:

1. Manual custom-token field on the invitation-issue form.
   Wraps the Phase-1 (#100) F10 bootstrap-friendly token feature
   that's been API-only until now.
2. Target-agent schema panel on the test-task form. Shows
   accepts_schema / accepts_control_schema for the destination
   so the admin knows the expected payload shape before hitting
   Send.

Source-pin style matching the rest of the admin-UI test suite.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Feature 1 — custom invitation token
# ===========================================================================


def test_issue_invitation_handler_accepts_token_form_field() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import invitations

    sig = inspect.signature(invitations.issue_invitation)
    assert "token" in sig.parameters
    # Default empty string so the form can submit without it.
    assert sig.parameters["token"].default.default == ""


def test_issue_invitation_validates_token_length() -> None:
    """UI mirrors the router-side ≥32-char check so the admin sees
    a clean inline error instead of an opaque 422."""
    pytest.importorskip("fastapi")
    from bp_admin.pages.invitations import _validate_token

    assert _validate_token("x" * 31) is not None
    assert "32 characters" in _validate_token("x" * 31)
    assert _validate_token("x" * 32) is None


def test_issue_invitation_validates_token_alphabet() -> None:
    """URL-safe alphabet only: A-Z a-z 0-9 - _."""
    pytest.importorskip("fastapi")
    from bp_admin.pages.invitations import _validate_token

    assert _validate_token("a" * 31 + "!") is not None
    assert "URL-safe" in _validate_token("a" * 31 + "!")
    assert _validate_token("AaBb_-0123" * 4) is None


def test_issue_invitation_forwards_token_to_upstream_when_set() -> None:
    """Source pin: the upstream POST body includes `token` only when
    the form supplied a non-empty value. Default behaviour
    (auto-generated token) is preserved when the field is empty."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import invitations

    src = inspect.getsource(invitations.issue_invitation)
    assert "custom_token = token.strip()" in src
    assert 'body_json["token"] = custom_token' in src
    # The conditional is: only set if non-empty.
    assert "if custom_token:" in src


def test_issue_invitation_redisplay_preserves_token_value() -> None:
    """An error on submit (validation, router 409) must re-render
    the form with the typed token intact — losing it would force
    the admin to re-paste."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import invitations

    src = inspect.getsource(invitations.issue_invitation)
    # The redisplay helper closes over `token` and forwards it.
    assert '"token": token' in src


def test_new_invitation_form_has_default_empty_token() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import invitations

    src = inspect.getsource(invitations.new_invitation_form)
    assert '"token": ""' in src


def test_new_invitation_template_has_custom_token_toggle() -> None:
    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "invitations"
        / "new.html"
    ).read_text()
    # Alpine state toggle.
    assert "useCustomToken" in body
    # Token input lives behind the toggle.
    assert 'name="token"' in body
    # Disabled-when-collapsed + required-when-visible so partial
    # form submission doesn't leak a half-filled value to the
    # upstream.
    assert ':required="useCustomToken"' in body
    assert ':disabled="!useCustomToken"' in body


def test_new_invitation_template_token_alpine_state_seeds_from_form() -> None:
    """If the server re-renders after an error WITH a non-empty
    token, the toggle should start open so the admin sees their
    value. The Alpine init expression reads `form.token`."""
    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "invitations"
        / "new.html"
    ).read_text()
    # Conditional initialiser based on whether the form has a token.
    assert "useCustomToken: {{ 'true' if form.token else 'false' }}" in body


def test_new_invitation_template_uses_url_safe_pattern_for_html5_validation() -> None:
    """HTML5 `pattern` attribute provides client-side validation as
    a UX win before submit."""
    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "invitations"
        / "new.html"
    ).read_text()
    assert 'pattern="[A-Za-z0-9_\\-]{32,}"' in body


# ===========================================================================
# Feature 2 — schema panel on test-task form
# ===========================================================================


def test_test_task_form_passes_agent_schema_lookup_dict() -> None:
    """Source pin: the form handler builds a dict of agent_id →
    { description, capabilities, accepts_schema,
    accepts_control_schema } and passes the DICT (not a pre-
    serialised JSON string) to the template. Template renders
    via Jinja's `tojson` filter which is HTML-attribute-safe.

    Pre-R4 the handler called `json.dumps(...)` and the template
    rendered `{{ ... | safe }}` inside an x-data="" attribute —
    `json.dumps` doesn't escape `"` for HTML-attribute context,
    so an agent's `description` containing `"` broke out and
    injected JS into the admin session. R4 second-pass review."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import test_task

    src = inspect.getsource(test_task.test_task_form)
    assert "agent_schema_lookup" in src
    assert '"accepts_schema"' in src
    assert '"accepts_control_schema"' in src
    # Handler MUST NOT pre-serialise — that's the XSS path.
    assert "json.dumps(agent_schema_lookup)" not in src
    # Dict passed under the un-suffixed key (no `_json` suffix).
    assert '"agent_schema_lookup": agent_schema_lookup' in src


def test_test_task_form_lookup_keyed_by_agent_id() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import test_task

    src = inspect.getsource(test_task.test_task_form)
    # The dict is keyed by agent_id so the Alpine handler can
    # `agentInfo[destination]` directly.
    assert 'agent_schema_lookup[a["agent_id"]]' in src


def _test_task_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "test_task"
        / "form.html"
    ).read_text()


def test_test_task_template_initialises_alpine_lookup() -> None:
    """Template renders the lookup map via Jinja's `tojson` filter
    which is HTML-attribute-safe (escapes `"` to `\\u0022` and `<`
    to `\\u003c` in attribute context, unlike `json.dumps + |safe`
    which left raw `"` to break out of the surrounding attribute)."""
    body = _test_task_html()
    # The Alpine state object carries the lookup map + the bound
    # destination + a computed selected accessor.
    assert "agentInfo:" in body
    assert "agent_schema_lookup | tojson" in body
    # The broken pattern must be entirely gone.
    assert "| safe" not in body
    assert "destination:" in body
    assert "get selected()" in body


def test_test_task_template_destination_input_uses_x_model() -> None:
    """Two-way binding so the schema panel updates as the admin
    types. Replaces the previous static `value=` attribute."""
    body = _test_task_html()
    assert 'x-model="destination"' in body


def test_test_task_template_renders_accepts_schema_when_selected() -> None:
    body = _test_task_html()
    # The schema panel is gated on `selected` (computed property).
    assert 'x-show="selected"' in body
    # Schema rendered via JSON.stringify with indent=2 — matches the
    # admin agent-detail page's pretty-printing.
    assert "JSON.stringify(selected.accepts_schema, null, 2)" in body


def test_test_task_template_renders_both_data_and_control_plane() -> None:
    """Mirrors the Phase 9c agent-detail page: data-plane and
    control-plane each get their own panel with distinct badges."""
    body = _test_task_html()
    assert "accepts_control_schema" in body
    assert "data-plane" in body
    assert "is_control" in body
    # Same palettes as agent-detail (Phase 9c) for cross-page
    # consistency.
    assert "bg-emerald-50" in body
    assert "bg-purple-50" in body


def test_test_task_template_explains_control_plane_not_sent_from_form() -> None:
    """Critical UX cue: the test-task endpoint sends data-plane
    only. The control-plane panel must call this out so the admin
    doesn't fill in a control-shape payload and wonder why the
    router rejects it."""
    body = _test_task_html()
    assert "This form sends data-plane only" in body


def test_test_task_template_handles_missing_schemas_gracefully() -> None:
    """An agent without accepts_schema (it's optional) should
    surface an explanatory note, not an empty panel."""
    body = _test_task_html()
    assert "router skips admit-time schema validation" in body
    assert "No control-plane surface" in body


def test_test_task_template_renders_capabilities_inline() -> None:
    """Capabilities live in the schema panel so the admin can see
    what the target agent does without leaving the page."""
    body = _test_task_html()
    assert "selected.capabilities" in body
    # Capabilities rendered as code-styled chips.
    assert 'x-text="cap"' in body
