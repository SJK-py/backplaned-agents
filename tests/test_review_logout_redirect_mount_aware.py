"""Logout redirect is mount-aware.

R4 second-pass review found that `bp_admin/pages/auth_pages.py`
hardcoded `RedirectResponse(url="/admin/login", ...)` in the
POST /logout handler. The other auth handlers in the same file
(`_login_url`, the login form GET) flow through
`bp_admin.asgi_utils.root_path(request)` so the same code
works both when bp-admin is mounted under the router (root_path
`/admin`) and when run standalone (root_path `""`).

Hardcoding the `/admin/login` redirect breaks the standalone
console-script deployment with a 404 right after logout — the
admin clicks Sign Out, gets bounced to `/admin/login` which
doesn't exist on the standalone server.

R5 fix: use `f"{root_path(request)}/login"` to match the rest
of the auth pages.
"""

from __future__ import annotations

import inspect

import pytest


def test_logout_handler_uses_root_path_helper() -> None:
    """Source pin: `logout` calls `root_path(request)` to compute
    the redirect target instead of hardcoding `/admin/login`."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import auth_pages

    src = inspect.getsource(auth_pages.logout)
    # Mount-aware redirect uses the helper.
    assert "root_path" in src
    # f-string built from the helper rather than a raw path.
    assert 'f"{rp}/login"' in src or 'f"{_root_path' in src
    # The hardcoded path is GONE.
    assert 'url="/admin/login"' not in src
