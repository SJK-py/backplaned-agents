"""bp_agents.agents.webapp.app — FastAPI application factory.

The browser-facing half of the webapp channel ([webapp.md] §1). A
standalone app (its own uvicorn, fronted by the edge proxy): its own
session middleware, auth + CSRF, templates, static. Talks to the router
as the logged-in user via the injected `UpstreamClient`, and reads the
suite DB directly via the injected pool (session badges, history).

`create_app` does NOT own the upstream/pool — the caller (the agent
process, or a test) builds and closes them. This keeps the factory pure
and the resources' lifecycle with whoever runs the event loop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bp_agents.agents.webapp.auth import make_auth_middleware
from bp_agents.agents.webapp.config import WebappConfig
from bp_agents.agents.webapp.csrf import make_csrf_middleware
from bp_agents.agents.webapp.pages import auth_pages, sessions
from bp_agents.agents.webapp.upstream import UpstreamClient

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


def _here() -> Path:
    return Path(__file__).resolve().parent


def _dt_filter(value: Any) -> str:
    """Jinja filter: datetime / ISO string → "YYYY-MM-DD HH:MM UTC"."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return str(value)
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def create_app(
    config: WebappConfig,
    *,
    upstream: UpstreamClient,
    pool: asyncpg.Pool | None = None,
) -> FastAPI:
    """Build the webapp. `upstream` (router HTTP, user-token) is required;
    `pool` (suite DB, for session badges) is optional — handlers degrade
    gracefully when it's None."""
    app = FastAPI(
        title="bp_webapp",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.config = config
    app.state.upstream = upstream
    app.state.pool = pool

    app.state.templates = Jinja2Templates(directory=str(_here() / "templates"))
    app.state.templates.env.filters["dt"] = _dt_filter
    app.mount(
        "/static",
        StaticFiles(directory=str(_here() / "static")),
        name="static",
    )

    # Middleware order — Starlette PREPENDS, so last-added is OUTERMOST.
    # Request path: SessionMiddleware → Auth → CSRF → handler. Session must
    # be outermost so `request.session` is populated before Auth reads it
    # (the bp_admin Bug-4 ordering contract).

    @app.middleware("http")
    async def _csrf_dispatch(request, call_next):  # type: ignore[no-untyped-def]
        mw = make_csrf_middleware()
        return await mw(request, call_next)

    @app.middleware("http")
    async def _auth_dispatch(request, call_next):  # type: ignore[no-untyped-def]
        mw = make_auth_middleware(config, app.state.upstream)
        return await mw(request, call_next)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret.get_secret_value(),
        session_cookie=config.session_cookie_name,
        max_age=config.session_cookie_max_age_s,
        same_site=config.session_cookie_same_site,
        https_only=config.session_cookie_secure,
    )

    app.include_router(auth_pages.router)
    app.include_router(sessions.router)
    return app
