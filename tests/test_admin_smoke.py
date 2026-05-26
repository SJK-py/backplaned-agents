"""Boot smoke test for the admin sub-app + Bug 4 / TemplateResponse
migration regression pins.

This is the integration-style test the upstream reviewer asked
for after Bug 4: a single `TestClient(create_app(...))` round-trip
that catches the "shadowed by the previous bug" failure mode.
The previous reports went:

  * Bug 1 (`itsdangerous`) shadowed Bug 3 (`state.upstream` unset).
  * Bug 3 shadowed Bug 4 (SessionMiddleware ordering).
  * Bug 4 shadowed the latent TemplateResponse positional-arg
    deprecation that broke every template render.

This test exercises the whole stack so the next regression of
the same shape surfaces in CI on the same PR that introduces
it.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# Boot smoke test — admin sub-app, /login round-trip
# ===========================================================================


def _build_admin_app():
    """Construct the admin app with the bare-minimum config the
    fixture needs. Mirrors what `_mount_admin_ui` does in the
    parent router for the eager `state.upstream` populate (Bug 3
    fix)."""
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
    # Mounted sub-apps don't run their own lifespan — the parent
    # populates `state.upstream` at mount time. Mirror that here so
    # the admin app boots in the same shape it does in production.
    app.state.upstream = UpstreamClient(
        cfg.router_url, timeout_s=cfg.upstream_timeout_s
    )
    return app


def test_admin_login_renders_clean() -> None:
    """End-to-end smoke: GET /login through the full middleware
    stack must return 200 with an HTML form. Catches:
      - Bug 1: bp_admin import failure (would 500 / not import)
      - Bug 3: `state.upstream` unset (AttributeError)
      - Bug 4: SessionMiddleware ordering wrong (AssertionError on
        request.session)
      - TemplateResponse positional-arg deprecation (TypeError on
        unhashable dict)
      - base.html `session.csrf_token` undefined (UndefinedError)
    Any of those fail the request with a 500 here.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _build_admin_app()
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200, (
        f"GET /login returned {r.status_code}; body excerpt: {r.text[:500]!r}"
    )
    assert "<form" in r.text.lower(), (
        "GET /login response doesn't contain a form element — "
        "template render likely failed"
    )
    # Pin the CSRF meta tag is present (base.html → request.session
    # → SessionMiddleware populated request.session).
    assert "csrf-token" in r.text.lower()


def test_admin_root_redirects_unauthenticated_user_to_login() -> None:
    """End-to-end: GET / with no session cookie must redirect to
    the login page. This file boots the admin app STANDALONE (no
    mount prefix), so the redirect target is `/login` — NOT
    `/admin/login`. The mounted-mode equivalent lives in
    `tests/test_admin_mounted_smoke.py` (review item upstream
    Bug-5 made `_login_url` mount-aware via
    `request.scope["root_path"]`, so the same code path works
    in both deployment shapes)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = _build_admin_app()
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
    assert r.status_code in (303, 302), (
        f"GET / returned {r.status_code}; expected redirect to login. "
        f"Body excerpt: {r.text[:500]!r}"
    )
    location = r.headers.get("location", "")
    # Standalone mode: redirect target is `/login`, no prefix.
    assert location.startswith("/login"), (
        f"Expected redirect to /login (standalone mode); got {location!r}"
    )


# ===========================================================================
# Bug 4: SessionMiddleware registration order
# ===========================================================================


def test_bug4_session_middleware_added_after_auth_and_csrf() -> None:
    """Source pin: `SessionMiddleware` MUST be added AFTER the
    `@app.middleware("http")` decorators in `create_app`. Starlette
    PREPENDS to `user_middleware`, so last-added is outermost.
    SessionMiddleware needs to be outermost so it runs BEFORE the
    auth middleware reads `request.session`. The previous
    `add_middleware(SessionMiddleware, ...)`-first ordering put
    Session INNERMOST and crashed Auth with
    `AssertionError: SessionMiddleware must be installed to access
    request.session` (upstream-bug #4)."""
    pytest.importorskip("fastapi")
    from bp_admin import app as admin_app_module

    src = inspect.getsource(admin_app_module.create_app)
    # The auth + csrf middleware decorators must appear BEFORE
    # the SessionMiddleware add_middleware call.
    auth_idx = src.find("_auth_dispatch")
    csrf_idx = src.find("_csrf_dispatch")
    session_add_idx = src.find("add_middleware(\n        SessionMiddleware")
    if session_add_idx == -1:
        session_add_idx = src.find("add_middleware(SessionMiddleware")
    assert auth_idx > 0, "auth middleware no longer registered"
    assert csrf_idx > 0, "csrf middleware no longer registered"
    assert session_add_idx > 0, (
        "upstream-bug #4 regression: SessionMiddleware no longer "
        "registered via add_middleware"
    )
    assert auth_idx < session_add_idx, (
        "upstream-bug #4 regression: SessionMiddleware is registered "
        "BEFORE _auth_dispatch — Starlette will put Session "
        "INNERMOST and Auth will crash on request.session"
    )
    assert csrf_idx < session_add_idx, (
        "upstream-bug #4 regression: SessionMiddleware is registered "
        "BEFORE _csrf_dispatch"
    )


def test_bug4_session_middleware_outermost_in_built_stack() -> None:
    """Behavioural pin: walking the built `app.user_middleware`
    list, `SessionMiddleware` MUST be at INDEX 0 — Starlette
    prepends each `add_middleware` call to `user_middleware`, so
    the last-added ends up at index [0], which corresponds to
    the OUTERMOST runtime position. SessionMiddleware needs to
    be outermost so it has populated `request.session` by the
    time the auth middleware reads it."""
    pytest.importorskip("fastapi")
    from starlette.middleware.sessions import SessionMiddleware

    app = _build_admin_app()
    middleware_classes = [m.cls for m in app.user_middleware]
    assert SessionMiddleware in middleware_classes, (
        "upstream-bug #4: SessionMiddleware not in the user_middleware "
        "stack at all"
    )
    # Index 0 = first slot in user_middleware = OUTERMOST at runtime
    # (Starlette wraps each subsequent middleware INSIDE the previous
    # one in iteration order).
    assert middleware_classes[0] is SessionMiddleware, (
        f"upstream-bug #4 regression: SessionMiddleware is at index "
        f"{middleware_classes.index(SessionMiddleware)} of "
        f"{len(middleware_classes)}; must be at index 0 (outermost). "
        f"Stack: {[c.__name__ for c in middleware_classes]}"
    )


# ===========================================================================
# TemplateResponse migration — Starlette ≥1.0 positional-arg form
# ===========================================================================


def test_template_response_calls_use_new_positional_form() -> None:
    """Starlette ≥1.0 changed `TemplateResponse(name, context, ...)`
    to `TemplateResponse(request, name, context, ...)`. Old-form
    calls misinterpret args (the dict context becomes `name`),
    triggering `TypeError: unhashable type: 'dict'` deep in
    Jinja2's template cache lookup. Pin source-level so a future
    handler that copies the old pattern is caught immediately.

    The migration: every `templates.TemplateResponse(` call must
    have `request` as the FIRST positional arg.
    """
    pytest.importorskip("fastapi")
    from pathlib import Path

    bp_admin = Path(__file__).parent.parent / "bp_admin"
    offenders: list[tuple[str, int, str]] = []
    import re

    # Match the call opener and the first positional arg on the
    # next line. Old form: first arg is a string literal. New form:
    # first arg is `request` (no quotes).
    pat = re.compile(
        r"templates\.TemplateResponse\(\s*\n\s*(\"[^\"]+\"|'[^']+')",
    )
    for py in bp_admin.rglob("*.py"):
        text = py.read_text()
        for m in pat.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append((str(py.relative_to(bp_admin)), line_no, m.group(1)))

    assert not offenders, (
        f"upstream Bug-4 follow-up regression: {len(offenders)} "
        f"TemplateResponse call(s) still use the old positional "
        f"form `templates.TemplateResponse(\"name\", ...)` — "
        f"Starlette ≥1.0 expects `request` as the first arg. "
        f"Offenders:\n"
        + "\n".join(f"  {p}:{ln}  starts with {arg}" for p, ln, arg in offenders)
    )


def test_base_template_uses_request_session_not_bare_session() -> None:
    """Source pin: `base.html` and any partial it includes MUST
    reference `request.session.*` — NOT the bare `session.*`
    convention. Bare `session` requires every TemplateResponse
    call to remember `"session": request.session` in its context;
    `request.session` is always available because Starlette
    injects `request` into the template context automatically.

    Pinning this avoids the trap where a new template that
    extends `base.html` works in dev but 500s in production
    because some handler forgot the `session` context key."""
    from pathlib import Path

    templates_dir = Path(__file__).parent.parent / "bp_admin" / "templates"
    offenders: list[tuple[str, int, str]] = []
    for tpl in templates_dir.rglob("*.html"):
        for line_no, line in enumerate(tpl.read_text().splitlines(), 1):
            stripped = line.strip()
            # Look for unqualified `session.` — `request.session.`
            # is fine, anything else isn't.
            for token in ("{{ session.", "{% if session.", "{% set session"):
                if token in line and "request.session" not in line:
                    offenders.append((
                        str(tpl.relative_to(templates_dir)), line_no, stripped
                    ))
                    break

    assert not offenders, (
        "Templates referencing bare `session.*` instead of "
        "`request.session.*`. Every TemplateResponse call would "
        "need to remember `\"session\": request.session` in its "
        "context; `request.session` is always available. "
        "Offenders:\n"
        + "\n".join(f"  {p}:{ln}  {s}" for p, ln, s in offenders)
    )
