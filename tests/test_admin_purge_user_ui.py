"""Admin UI — the permanent-erase (purge) affordance on the user detail page.

Inspection-style (mirrors the other admin-UI tests): asserts the BFF route,
the params=purge=true upstream call, and the guarded template markup without
standing up the app/auth harness.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def test_users_router_has_purge_route_with_purge_param() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages import users

    src = inspect.getsource(users)
    assert '@router.post("/{user_id}/purge"' in src
    # Routes to the router's purge path via the query param.
    assert '"DELETE",' in src
    assert '{"purge": "true"}' in src


def test_detail_template_has_guarded_purge_form() -> None:
    html = Path("bp_admin/templates/users/detail.html").read_text()
    # Form posts to the purge handler, gated on not-already-purged.
    assert "{% if not user.purged_at %}" in html
    assert 'action="/admin/users/{{ user.user_id }}/purge"' in html
    # Type-to-confirm guard: submit disabled until "ERASE" is typed.
    assert "typed !== 'ERASE'" in html
    # And a distinct "purged" badge.
    assert "purged" in html


def test_user_view_exposes_purged_at() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import UserView

    assert "purged_at" in UserView.model_fields
