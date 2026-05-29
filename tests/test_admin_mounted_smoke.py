"""Mounted-mode smoke + regression tests for the admin BFF.

The admin app supports two deployment shapes:
  - Standalone via the `bp-admin` console script (no mount prefix).
  - Mounted under the router via `parent.mount("/admin", admin_app)`.

The standalone smoke test in `tests/test_admin_smoke.py` covers
the first shape. This file covers the SECOND — production
shape — where `request.url.path` carries the mount prefix
(`/admin/login`, not `/login`). The two have different
mount-awareness requirements that the upstream-bug-5 report
exposed:

  * `auth.PUBLIC_PATHS = {"/login"}` matched the unprefixed path
    in standalone mode but NOT the prefixed path in mounted
    mode → unauthenticated user redirected to `/admin/login` →
    auth middleware sees `/admin/login` ∉ PUBLIC_PATHS →
    redirects to `/admin/login` again → infinite redirect loop.
  * `auth._login_url` hard-coded `/admin/login` as the redirect
    target. Worked in mounted-under-`/admin` deployment but
    404'd standalone.

The fix made both mount-aware via `request.scope["root_path"]`.
The tests below exercise both modes against the same `auth.py`
code path so a future change that re-introduces a mount-prefix
assumption is caught immediately.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Mounted-mode helpers
# ===========================================================================


def _make_mounted_app():
    """Build the admin app the way `bp_router._mount_admin_ui` does
    in production: construct, eagerly populate `state.upstream`
    (since mounted sub-apps don't run their own lifespan), then
    mount under `/admin` on a parent FastAPI app."""
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from fastapi import FastAPI
    from pydantic import SecretStr

    from bp_admin.app import create_app
    from bp_admin.config import AdminConfig
    from bp_admin.upstream import UpstreamClient

    cfg = AdminConfig(
        router_url="http://127.0.0.1:0",
        session_secret=SecretStr("x" * 32),
    )
    admin_app = create_app(cfg)
    admin_app.state.upstream = UpstreamClient(
        cfg.router_url, timeout_s=cfg.upstream_timeout_s
    )
    parent = FastAPI()
    parent.mount("/admin", admin_app)
    return parent


# ===========================================================================
# Bug 5: mount-aware auth middleware
# ===========================================================================


def test_bug5_mounted_login_does_not_redirect_loop() -> None:
    """The exact upstream-bug #5 reproduction. GET /admin/login
    on a mounted admin app must return 200 (rendered login form)
    — NOT a 303 redirect to itself.

    Pre-fix: the auth middleware compared `request.url.path` (=
    `/admin/login`) against `PUBLIC_PATHS = {"/login"}`. Membership
    failed → unauthenticated user redirected to `/admin/login` →
    same check failed → infinite redirect loop. Browsers / curl
    bail at their --max-redirs cap; either way the login form
    is unreachable.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _make_mounted_app()
    with TestClient(app) as client:
        r = client.get("/admin/login", follow_redirects=False)

    assert r.status_code == 200, (
        f"upstream-bug #5 regression: GET /admin/login returned "
        f"{r.status_code}; expected 200 (rendered login form). "
        f"Location header: {r.headers.get('location')!r}. "
        f"This is the exact 303-loop the auth middleware's "
        f"PUBLIC_PATHS check produces when it doesn't account "
        f"for the mount prefix."
    )
    assert "text/html" in r.headers.get("content-type", "")
    assert "<form" in r.text.lower()


def test_bug5_mounted_unauth_root_redirects_to_mounted_login() -> None:
    """Unauthenticated GET /admin/ must redirect to /admin/login,
    NOT to /login (which would 404 since the mount prefix is
    /admin). The redirect target must be a real URL on the
    mounted app, and following it must land on a 200 — proving
    no loop."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _make_mounted_app()
    with TestClient(app) as client:
        r = client.get("/admin/", follow_redirects=False)

    assert r.status_code == 303, (
        f"GET /admin/ unauth: expected 303 redirect to login, "
        f"got {r.status_code}"
    )
    location = r.headers.get("location", "")
    assert location.startswith("/admin/login"), (
        f"Login redirect should target /admin/login (mount-aware); "
        f"got {location!r}. _login_url is hard-coding the path "
        f"instead of building it from request.scope['root_path']."
    )

    # And following the redirect must land on a 200 — proving no
    # loop (which is the upstream-bug #5 failure mode).
    with TestClient(app) as client:
        r2 = client.get(location, follow_redirects=False)
    assert r2.status_code == 200, (
        f"Following login redirect produced {r2.status_code} → "
        f"{r2.headers.get('location')!r}. If non-200 here, the "
        f"redirect loop is back."
    )


def test_bug5_standalone_login_still_works() -> None:
    """Sanity-pin the standalone mode. The fix must work for
    BOTH deployment shapes uniformly. A regression that
    over-corrects (e.g. hard-codes `/admin` as the prefix in
    `_login_url`) would 404 the standalone `bp-admin` console
    script."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
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

    with TestClient(app) as client:
        # GET /login standalone must still render.
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 200, (
            f"Standalone GET /login regressed: {r.status_code}"
        )

        # Unauth root must redirect to /login (NOT /admin/login).
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        location = r.headers.get("location", "")
        assert location.startswith("/login"), (
            f"Standalone unauth redirect should target /login, "
            f"got {location!r}. The fix may be hard-coding /admin "
            f"as a prefix instead of using request.scope['root_path']."
        )


def test_bug5_strip_root_path_helper_handles_unmounted() -> None:
    """Behavioural pin on the helper: when `root_path` is empty
    (standalone mode), the path is returned unchanged."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.asgi_utils import strip_root_path as _strip_root_path

    fake_url = SimpleNamespace(path="/login")
    fake_request = SimpleNamespace(url=fake_url, scope={"root_path": ""})
    assert _strip_root_path(fake_request) == "/login"


def test_bug5_strip_root_path_helper_strips_mount_prefix() -> None:
    """Behavioural pin: when `root_path = "/admin"`, the prefix
    is removed."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.asgi_utils import strip_root_path as _strip_root_path

    fake_url = SimpleNamespace(path="/admin/login")
    fake_request = SimpleNamespace(
        url=fake_url, scope={"root_path": "/admin"}
    )
    assert _strip_root_path(fake_request) == "/login"


def test_bug5_strip_root_path_helper_handles_arbitrary_prefix() -> None:
    """Defence-in-depth: the helper must work for ANY mount
    prefix, not just `/admin`. Catches a regression that
    hard-codes `/admin` somewhere."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.asgi_utils import strip_root_path as _strip_root_path

    fake_url = SimpleNamespace(path="/foo/bar/login")
    fake_request = SimpleNamespace(
        url=fake_url, scope={"root_path": "/foo/bar"}
    )
    assert _strip_root_path(fake_request) == "/login"


def test_bug5_strip_root_path_returns_root_for_exact_prefix() -> None:
    """When the path IS the mount prefix exactly (e.g. GET on
    `/admin` with root_path=`/admin`), stripping leaves an empty
    string — which the helper normalises to `/` so downstream
    code doesn't have to special-case it."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.asgi_utils import strip_root_path as _strip_root_path

    fake_url = SimpleNamespace(path="/admin")
    fake_request = SimpleNamespace(
        url=fake_url, scope={"root_path": "/admin"}
    )
    assert _strip_root_path(fake_request) == "/"


def test_bug5_login_url_uses_root_path_from_scope() -> None:
    """Source pin: `_login_url` must build the URL using
    `request.scope.get("root_path", ...)`, NOT a hard-coded
    string. A regression that re-introduces the hardcoded
    `/admin/login` works for mounted mode but breaks standalone."""
    pytest.importorskip("fastapi")
    from bp_admin import auth as auth_module

    src = inspect.getsource(auth_module._login_url)
    # `_login_url` now derives the prefix via the shared
    # `bp_admin.asgi_utils.root_path` helper instead of reading
    # `request.scope` inline. Either pattern satisfies the
    # contract — it's the prefix-derivation that matters, not
    # the call site.
    assert (
        "_root_path(request)" in src
        or 'request.scope.get("root_path"' in src
    ), (
        "upstream-bug #5 regression: _login_url no longer derives "
        "the mount prefix from the ASGI scope; mount prefix will "
        "diverge from the redirect target"
    )
    # The buggy hardcoded form must NOT be present (allowing for
    # comments that may mention it for context).
    code_only_lines = [
        line for line in src.split("\n")
        if not line.lstrip().startswith("#")
    ]
    code_only = "\n".join(code_only_lines)
    assert '"/admin/login' not in code_only, (
        "upstream-bug #5 regression: _login_url contains a "
        "hardcoded /admin/login string — should be derived "
        "from request.scope['root_path']"
    )


def test_bug5_auth_middleware_uses_strip_root_path() -> None:
    """Source pin: the auth middleware MUST call `_strip_root_path`
    before checking PUBLIC_PATHS. Pin so a regression that
    reverts to `path = request.url.path` is caught."""
    pytest.importorskip("fastapi")
    from bp_admin import auth as auth_module

    src = inspect.getsource(auth_module.make_auth_middleware)
    assert "strip_root_path(request)" in src, (
        "upstream-bug #5 regression: auth middleware no longer "
        "strips the mount prefix before checking PUBLIC_PATHS"
    )
    # The helper now lives in `bp_admin.asgi_utils` (promoted from
    # auth.py per upstream-bug #6 — both auth and CSRF need the same
    # implementation, so it's shared). Citation pin lives there.
    from bp_admin import asgi_utils

    # The shared helper must exist and be sourceable in its new home.
    assert inspect.getsource(asgi_utils.strip_root_path)
