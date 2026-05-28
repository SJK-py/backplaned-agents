"""webapp agent — the browser channel process ([webapp.md] §1).

A suite process wearing two hats: a **channel agent** (WS to the router,
used for chat task injection + progress in a later phase) and a **web
server** (FastAPI, this module launches it). On startup it opens the suite
DB pool, builds the router HTTP client, constructs the FastAPI app, and
runs uvicorn as a background task inside the agent's event loop.

HTTP ops authenticate as the *logged-in user* (their own token), so —
unlike the chatbot — the webapp needs no service principal ([webapp.md]
§3). Membership in the `channel` group is what the suite ACL keys task
injection on (`channel/* → l0`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_agents.agents.webapp.app import create_app
from bp_agents.agents.webapp.config import WebappConfig
from bp_agents.agents.webapp.upstream import UpstreamClient
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo
from bp_sdk import Agent

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

WEBAPP_AGENT_ID = "webapp"


agent = Agent(
    info=AgentInfo(
        agent_id=WEBAPP_AGENT_ID,
        description="Browser channel + session manager.",
        groups=["channel", "inbound"],
        capabilities=[
            "channel.webapp",
            "user.auth",
            "file.full",
            "session.history",
            "session.management",
        ],
        hidden=True,
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None
_upstream: UpstreamClient | None = None
_web_server: object | None = None  # uvicorn.Server
_web_task: asyncio.Task | None = None


def _http_url() -> str:
    """Derive the router's HTTP base from its WS url (for the user-token
    control-plane client). Mirrors the chatbot's derivation."""
    url = agent.config.router_url
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :].split("/v1/")[0]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :].split("/v1/")[0]
    return url


@agent.on_startup
async def _startup() -> None:
    global _pool, _upstream, _web_server, _web_task  # noqa: PLW0603
    import uvicorn  # noqa: PLC0415

    _pool = await open_pool(_settings)
    cfg = WebappConfig()  # type: ignore[call-arg]
    _upstream = UpstreamClient(_http_url(), timeout_s=cfg.upstream_timeout_s)
    app = create_app(cfg, upstream=_upstream, pool=_pool)

    server = uvicorn.Server(
        uvicorn.Config(
            app, host=cfg.bind_host, port=cfg.bind_port,
            log_config=None, lifespan="off",
        )
    )
    # The SDK agent owns the process signals (graceful WS drain); don't let
    # uvicorn install its own handlers and race them.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    _web_server = server
    _web_task = asyncio.create_task(server.serve())
    logger.info(
        "webapp_started",
        extra={"event": "webapp_started", "bind": f"{cfg.bind_host}:{cfg.bind_port}",
               "router_url": _http_url()},
    )


@agent.on_shutdown
async def _shutdown() -> None:
    if _web_server is not None:
        _web_server.should_exit = True  # type: ignore[attr-defined]
    if _web_task is not None:
        try:
            await asyncio.wait_for(_web_task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            _web_task.cancel()
    if _upstream is not None:
        await _upstream.aclose()
    if _pool is not None:
        await _pool.close()


if __name__ == "__main__":
    agent.run()
