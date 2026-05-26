"""Tests for the registration-queue submitter filter.

Closes the out-of-scope flag from PR #109. Lets operators triage
by `submitted_by_service_user_id` — useful when one channel agent
is misbehaving and flooding the queue.

Source-pin style. End-to-end behaviour through the SQL is
exercised by the existing integration suite (the filter shape is
additive; behaviour over an empty result set is the same as
before).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Query layer
# ===========================================================================


def test_list_pending_registrations_accepts_submitter_filter() -> None:
    """Query helper takes both filters as optional kwargs that
    compose with AND."""
    from bp_router.db import queries

    sig = inspect.signature(queries.list_pending_registrations)
    params = sig.parameters
    assert "channel" in params
    assert "submitted_by_service_user_id" in params
    assert params["submitted_by_service_user_id"].default is None
    assert params["channel"].default is None


def test_list_pending_registrations_builds_where_clause_dynamically() -> None:
    """Source pin: the dual-filter case requires composing WHERE
    clauses rather than a chain of `if/elif` SELECTs. Pinning the
    composition idiom so a future refactor that drops one filter
    branch is caught."""
    from bp_router.db import queries

    src = inspect.getsource(queries.list_pending_registrations)
    # The WHERE clauses are accumulated.
    assert "where: list[str] = []" in src
    # Both filters add to the list when set.
    assert "channel = $" in src
    assert "submitted_by_service_user_id = $" in src
    # AND-joined.
    assert "' AND '.join(where)" in src


def test_list_pending_registrations_orders_newest_first() -> None:
    """Order is part of the contract — the admin UI assumes
    newest-first when rendering the table."""
    from bp_router.db import queries

    src = inspect.getsource(queries.list_pending_registrations)
    assert "ORDER BY requested_at DESC" in src


# ===========================================================================
# Router endpoint
# ===========================================================================


def test_list_registrations_endpoint_accepts_submitter_query_param() -> None:
    """The router's GET /v1/admin/registrations exposes the new
    filter as `submitted_by_service_user_id` (matches the row
    field name / audit log column)."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    sig = inspect.signature(admin.list_registrations)
    assert "submitted_by_service_user_id" in sig.parameters


def test_list_registrations_endpoint_passes_filter_through_to_query() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.list_registrations)
    assert "submitted_by_service_user_id=submitted_by_service_user_id" in src


# ===========================================================================
# Admin UI page
# ===========================================================================


def test_admin_list_page_accepts_submitted_by_query_param() -> None:
    """UI uses the shorter `submitted_by` query name to keep URLs
    readable; converts to the full upstream parameter name when
    forwarding."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    sig = inspect.signature(registrations.list_registrations)
    assert "submitted_by" in sig.parameters


def test_admin_list_page_forwards_to_upstream_full_name() -> None:
    """Source pin: the bridge converts the UI's short
    `submitted_by` to the router's full
    `submitted_by_service_user_id`. Without this the upstream
    filter wouldn't activate."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.list_registrations)
    assert 'params["submitted_by_service_user_id"] = submitted_by' in src


def test_admin_list_page_derives_submitter_options_from_rows() -> None:
    """Dropdown is populated from the rows currently visible — same
    trick the channel filter uses."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.list_registrations)
    assert "submitted_by_options" in src
    assert 'r.get("submitted_by_service_user_id")' in src


def test_admin_list_page_preserves_active_filter_in_dropdown() -> None:
    """If the filter narrows results to zero rows, the dropdown
    must still show the active value — otherwise the operator
    can't tell what filter is in effect or clear it cleanly."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import registrations

    src = inspect.getsource(registrations.list_registrations)
    # The current filter value is appended to the options list
    # when not already present.
    assert "channel not in channels" in src
    assert "submitted_by not in submitters" in src


# ===========================================================================
# Template — list filter form
# ===========================================================================


def _list_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "list.html"
    ).read_text()


def test_list_template_has_submitted_by_select() -> None:
    body = _list_html()
    assert 'name="submitted_by"' in body
    assert 'id="submitted_by"' in body
    assert "All submitters" in body


def test_list_template_renders_active_submitter_option() -> None:
    """The selected option toggles via the same `if opt ==
    submitted_by_filter` pattern as the channel dropdown."""
    body = _list_html()
    assert "submitted_by_filter" in body
    assert "submitted_by_options" in body


def test_list_template_submitter_select_uses_monospace() -> None:
    """user_ids are 30+ chars of `usr_…` — monospace makes them
    legible in the dropdown. Channel slugs stay in the default
    sans face."""
    body = _list_html()
    # Find the submitted_by select block and confirm font-mono is
    # on the element itself, not just somewhere on the page.
    idx = body.index('id="submitted_by"')
    # Walk back to the opening `<select` and forward to the
    # closing `>` to inspect the attributes.
    select_open = body.rindex("<select", 0, idx)
    select_close = body.index(">", idx)
    select_tag = body[select_open : select_close + 1]
    assert "font-mono" in select_tag


def test_table_body_empty_state_messages_handle_both_filters() -> None:
    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "registrations"
        / "_table_body.html"
    ).read_text()
    # All four cells of the (channel × submitted_by) presence
    # matrix produce a sensible empty-state message.
    assert "channel_filter and submitted_by_filter" in body
    assert "elif channel_filter" in body
    assert "elif submitted_by_filter" in body
    # And the original "queue is empty" copy still wins when no
    # filter is set.
    assert "Channel agents submit via" in body
