"""bp_admin.app — FastAPI application factory for the admin BFF.

Mount under `/admin` from the router (or any other host). The app is
self-contained: its own session middleware, its own static files, its
own Jinja2 templating env. The only outbound dependency is the
`UpstreamClient` it uses to talk to the router's JSON API.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bp_admin.auth import make_auth_middleware
from bp_admin.config import AdminConfig
from bp_admin.csrf import make_csrf_middleware
from bp_admin.pages import (
    account,
    acl,
    agents,
    audit,
    auth_pages,
    dashboard,
    invitations,
    llm_presets,
    mcp_servers,
    registrations,
    test_task,
    users,
)
from bp_admin.upstream import UpstreamClient

logger = logging.getLogger(__name__)


def _here() -> Path:
    return Path(__file__).resolve().parent


def _dt_filter(value: Any) -> str:
    """Jinja filter: ISO datetime → "YYYY-MM-DD HH:MM UTC".

    Accepts a `datetime`, an ISO string (with or without trailing 'Z'),
    or None. Returns "" for None/empty and the original string when
    parsing fails — never raises in the template.
    """
    if value is None or value == "":
        return ""
    dt: datetime
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value)
        # `datetime.fromisoformat` doesn't accept 'Z' until Python 3.11.
        # Normalise to '+00:00' for portability.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the upstream HTTP client at startup, close it at shutdown."""
    config: AdminConfig = app.state.config
    app.state.upstream = UpstreamClient(
        config.router_url, timeout_s=config.upstream_timeout_s
    )
    logger.info(
        "admin_started",
        extra={
            "event": "admin_started",
            "router_url": config.router_url,
            "deployment_env": config.deployment_env,
        },
    )
    try:
        yield
    finally:
        await app.state.upstream.aclose()
        logger.info("admin_stopped", extra={"event": "admin_stopped"})


def create_app(config: AdminConfig | None = None) -> FastAPI:
    """Build the admin BFF app. Designed to be mounted under `/admin`
    from the router via `app.mount("/admin", admin_app)`."""
    config = config or AdminConfig()  # type: ignore[call-arg]

    app = FastAPI(
        title="bp_admin",
        version="0.1.0",
        lifespan=_lifespan,
        # No OpenAPI docs for the admin BFF; it's HTML, not a JSON API.
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = config

    # Templates and static files live inside the package.
    app.state.templates = Jinja2Templates(directory=str(_here() / "templates"))
    app.state.templates.env.filters["dt"] = _dt_filter
    app.mount(
        "/static",
        StaticFiles(directory=str(_here() / "static")),
        name="static",
    )

    # Middleware stack — Starlette's `add_middleware` PREPENDS to
    # `user_middleware`, so the LAST-added middleware ends up
    # OUTERMOST (first to see the request, last to return the
    # response). Order on the request path is therefore the
    # reverse of the registration order below:
    #
    #   request → SessionMiddleware → Auth → CSRF → handler
    #
    # SessionMiddleware populates `request.session`. Auth gates the
    # request on the session and short-circuits unauth users. CSRF
    # runs last so its token check sees an authenticated session
    # and can read the form body without racing the handler
    # (`request.form()` caches).
    #
    # Bug-4 (upstream report) was that SessionMiddleware was
    # registered FIRST in the source — which under Starlette's
    # prepend semantics put it INNERMOST, so Auth ran before
    # SessionMiddleware populated `request.session` and crashed
    # with `AssertionError: SessionMiddleware must be installed
    # to access request.session`. The previous comment block
    # claimed "later-added is innermost", which was inverted from
    # actual Starlette behaviour.
    #
    # The boot-smoke test in `tests/test_admin_smoke.py` exercises
    # this end-to-end via TestClient so a future re-ordering that
    # breaks the contract surfaces in CI immediately.

    # CSRF — innermost. Registered FIRST so it ends up closest to
    # the handler.
    @app.middleware("http")
    async def _csrf_dispatch(request, call_next):  # type: ignore[no-untyped-def]
        mw = make_csrf_middleware()
        return await mw(request, call_next)

    # Auth — middle. Sits between Session and CSRF.
    @app.middleware("http")
    async def _auth_dispatch(request, call_next):  # type: ignore[no-untyped-def]
        upstream: UpstreamClient = app.state.upstream
        mw = make_auth_middleware(config, upstream)
        return await mw(request, call_next)

    # Session cookie state — registered LAST so it ends up
    # OUTERMOST. Auth depends on `request.session` being populated;
    # this ordering guarantees Session has run by the time Auth
    # reads it.
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret.get_secret_value(),
        session_cookie=config.session_cookie_name,
        max_age=config.session_cookie_max_age_s,
        same_site=config.session_cookie_same_site,
        https_only=config.session_cookie_secure,
    )

    # Routers.
    app.include_router(auth_pages.router)
    app.include_router(account.router, prefix="/account")
    app.include_router(dashboard.router)
    app.include_router(users.router, prefix="/users")
    app.include_router(agents.router, prefix="/agents")
    app.include_router(invitations.router, prefix="/invitations")
    app.include_router(registrations.router, prefix="/registrations")
    app.include_router(acl.router, prefix="/acl/rules")
    app.include_router(audit.router, prefix="/audit")
    app.include_router(test_task.router, prefix="/test-task")
    app.include_router(llm_presets.router, prefix="/llm/presets")
    app.include_router(mcp_servers.router, prefix="/mcp-servers")

    return app


def main() -> None:
    """Standalone entry-point. `bp-admin` console script — runs the BFF
    on its own port talking to a remote router. For same-process
    deployment use `bp-router`, which mounts the admin app
    automatically when `ROUTER_SERVE_ADMIN_UI=true` (the default)."""
    import uvicorn  # noqa: PLC0415

    config = AdminConfig()  # type: ignore[call-arg]
    uvicorn.run(
        create_app,
        factory=True,
        host=config.bind_host,
        port=config.bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
