"""bp_router.app — FastAPI application factory and lifespan management.

The lifespan boots the database pool, Redis client, file store, ACL
evaluator, observability exporters, and starts background tasks
(timeout sweep, file GC, ACL hot-reload watcher).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bp_router.settings import Settings, load_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application state — attached to FastAPI's `app.state` for DI in handlers
# ---------------------------------------------------------------------------


class AppState:
    """Shared, long-lived runtime state.

    Attached to `app.state.bp` in `create_app()`. Endpoint handlers and
    WebSocket dispatch reach into this for the DB pool, file store, etc.
    """

    settings: Settings
    db_pool: object  # asyncpg.Pool — typed import deferred to avoid hard dep at import time
    redis: object    # redis.asyncio.Redis | None
    file_store: object  # FileStore (see bp_router.storage)
    socket_registry: object  # SocketRegistry (see bp_router.ws_hub)
    rules: object  # bp_router.acl.RuleSet
    llm_service: object  # LlmService (see bp_router.llm)
    correlation: object  # PendingAcks (see bp_router.correlation)

    # Strong references to fire-and-forget background tasks (resume-window
    # timers, per-LLM-stream pumps, etc.). Without this, asyncio may garbage
    # collect a Task whose only reference was the local in
    # `asyncio.create_task(...)`. Callers should `add_done_callback` to
    # `bg_tasks.discard` so completed tasks are released.
    bg_tasks: set

    # In-process `task_id → asyncio.Event` map. Set by the terminal-state
    # writers (`complete_task`, `cancel_task`, `fail_task`) AFTER the
    # transaction commits. Awaited by `POST /v1/admin/tasks/test` to
    # avoid a 50-ms busy-poll against the DB pool.
    # Same-worker only — a multi-worker deployment whose terminal
    # transition lands on a DIFFERENT worker won't fire the local
    # event; the test endpoint compensates with a 1-s fallback poll.
    # A future PR can swap this for Postgres `LISTEN/NOTIFY` to make
    # the wakeup multi-worker.
    task_terminal_events: dict

    # In-process `task_id → caller_agent_id` cache for `_handle_progress`.
    # Without this, every inbound ProgressFrame ran a fresh
    # `pool.acquire()` + SELECT against `tasks` — at line rate (a
    # chatty agent emitting 100 Progress/s) this saturated the default
    # 10-conn pool. Populated at admit time; evicted on terminal-state
    # transition. Always points at a real agent id (channel agent for
    # root tasks, parent's destination for children, synthetic
    # `admin_console` for admin-tested tasks) — no None sentinel.
    caller_agent_cache: dict

    # In-process index: `task_id → set[asyncio.Task]` of router-side
    # LLM-call Tasks spawned on behalf of that task. `cancel_task`
    # consults this for an O(1) lookup instead of scanning every
    # live socket × every in-flight LLM task (O(M·K)) on every
    # cancel. Populated at `dispatch._handle_llm_request` Task
    # creation, pruned by the Task's done-callback; self-bounded
    # (entries vanish when their Tasks finish).
    llm_tasks_by_task_id: dict

    # Per-user admit-rate token bucket. Backed by Redis when
    # configured; falls back to a per-process dict otherwise. The
    # fallback is correct for single-worker deployments and silently
    # incorrect across replicas — `_redis_required_in_non_dev` in
    # `bp_router.settings` rejects the misconfigured non-dev case at
    # startup so the silent-bypass shape can't ship to staging/prod.
    admit_quota: object  # bp_router.security.rate_limit.TokenBucket

    # WS-handshake thundering-herd guards. The semaphore caps how
    # many handshakes run the DB-heavy section concurrently (sized
    # < db_pool_max_size so a fleet reconnect can't drain the pool);
    # the catalog cache single-flights the O(N) `list_agents` scan
    # so a reconnecting fleet shares one scan per TTL. See
    # `settings.ws_handshake_max_concurrent` /
    # `ws_handshake_catalog_cache_ttl_s`.
    ws_handshake_semaphore: object  # asyncio.Semaphore
    agent_catalog_cache: object  # bp_router.ws_hub._CatalogCache


def spawn_background(state: AppState, coro) -> asyncio.Task:  # type: ignore[no-untyped-def]
    """Schedule `coro`, register on the AppState set, and auto-discard on done.

    Use this instead of bare `asyncio.create_task(...)` for any fire-and-
    forget task that must outlive its caller's local scope.
    """
    task = asyncio.create_task(coro)
    state.bg_tasks.add(task)
    task.add_done_callback(state.bg_tasks.discard)
    return task


def _boot_phase(name: str) -> str:
    """Log a boot-progress breadcrumb and return the phase name.

    Without this, a boot that dies in (say) the DB-pool phase exits
    with a raw asyncpg traceback and no indication of *which*
    dependency was being reached — under a process supervisor that
    is an opaque crashloop. Each phase emits one `boot_phase` line;
    the matching `boot_failed` (logged by `lifespan` on any boot
    exception) names the exact phase, turning a multi-person triage
    into a one-line diagnosis. Cheap: a router boots once.
    """
    logger.info(
        "boot_phase", extra={"event": "boot_phase", "phase": name}
    )
    return name


async def _boot_cleanup(state: AppState) -> None:
    """Best-effort teardown of resources a *partial* boot opened.

    A crashlooping router that opened the DB pool in phase 2 and
    then died in phase 5 would otherwise leak a Postgres pool (and
    maybe a Redis socket) on every restart. Swallow everything —
    we're already on the failure path and about to re-raise.
    """
    pool = getattr(state, "db_pool", None)
    if pool is not None:
        try:
            await pool.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "boot_cleanup_pool_close_failed",
                extra={"event": "boot_cleanup_pool_close_failed"},
                exc_info=True,
            )
    redis = getattr(state, "redis", None)
    if redis is not None:
        try:
            await redis.aclose()
        except Exception:  # noqa: BLE001
            logger.debug(
                "boot_cleanup_redis_close_failed",
                extra={"event": "boot_cleanup_redis_close_failed"},
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot subsystems in dependency order; tear down in reverse."""
    settings = load_settings()
    state = AppState()
    state.settings = settings
    state.bg_tasks = set()
    state.task_terminal_events = {}
    # `caller_agent_cache` is a hot-path lookup avoidance for
    # Progress fan-out (`dispatch._handle_progress` consults it
    # before a per-frame DB query). Bounded LRU because terminal-
    # task eviction (R1 `_notify_task_terminal`) only fires on
    # the same worker that admitted the task; multi-worker
    # deployments leaked entries here pre-R8.
    from bp_router.lru_cache import BoundedLRUDict  # noqa: PLC0415
    state.caller_agent_cache = BoundedLRUDict(
        maxsize=settings.caller_agent_cache_max
    )
    # task_id → set of router-side LLM Tasks (see field docstring).
    # Plain dict: self-bounded by the per-Task done-callback that
    # discards entries, so no LRU cap needed.
    state.llm_tasks_by_task_id = {}
    app.state.bp = state

    # Every phase below sets `current_phase` (via `_boot_phase`,
    # which also logs a breadcrumb) so the single `except` at the
    # bottom can name the exact phase that failed instead of letting
    # uvicorn print a context-free traceback into a crashloop.
    current_phase = "init"
    try:
        # 1. Observability (must be first so subsequent init is traced)
        current_phase = _boot_phase("observability")
        from bp_router.observability import (  # noqa: PLC0415
            configure_logging,
            configure_metrics,
            configure_tracing,
        )

        configure_logging(settings)
        configure_tracing(settings)
        configure_metrics()

        # 2. Database pool
        current_phase = _boot_phase("database_pool")
        from bp_router.db.connection import open_pool  # noqa: PLC0415

        state.db_pool = await open_pool(settings)

        # 3. Redis (optional in dev; required in staging/prod via the
        # `_redis_required_in_non_dev` settings validator).
        current_phase = _boot_phase("redis")
        from bp_router.observability.metrics import (  # noqa: PLC0415
            redis_health,
        )

        if settings.valkey_url:
            from bp_router.db.connection import open_redis  # noqa: PLC0415

            try:
                state.redis = await open_redis(settings)
                redis_health.set(1)
            except Exception:  # noqa: BLE001
                # Redis is *configured* but unreachable AT BOOT. The
                # running router already tolerates Redis flakes
                # everywhere — rate-limit and jti-revocation both
                # fall back to per-process on any Redis exception and
                # flip `redis_health` to 0. Failing the whole boot
                # here is the *only* place that breaks that contract,
                # turning a transient Redis blip into a total router
                # crashloop that also kills the paths needing no
                # Redis at all. Start degraded instead: per-process
                # fallback + `redis_health=0`, which the existing
                # `router_redis_health == 0` alert already pages on.
                # The non-dev "Redis required" invariant
                # (`_redis_required_in_non_dev`) checks the URL is
                # *set*, not *reachable*, so this weakens nothing it
                # enforces.
                state.redis = None
                redis_health.set(0)
                logger.error(
                    "redis_unreachable_at_boot_starting_degraded",
                    extra={"event": "redis_unreachable_at_boot"},
                    exc_info=True,
                )
        else:
            state.redis = None
            redis_health.set(0)
            # Surface the silent-fallback shape so a single-worker dev
            # operator sees what they're getting. The non-dev case is
            # blocked at settings-validator time, so reaching here
            # means the operator opted in.
            logger.warning(
                "redis_disabled_revocation_and_quota_per_process",
                extra={"event": "redis_disabled_per_process_fallback"},
            )

        # 3b. Admit-rate token bucket (uses `state.redis` if set).
        current_phase = _boot_phase("rate_limiters")
        from bp_router.security.rate_limit import (  # noqa: PLC0415
            TokenBucket,
        )

        state.admit_quota = TokenBucket(
            redis=state.redis, prefix="quota:admit"
        )
        # 3c. Login / refresh / change-password rate limits —
        # credential stuffing defence; runs BEFORE argon2 verify so a
        # saturated bucket short-circuits without paying the hash
        # cost.
        state.login_quota = TokenBucket(
            redis=state.redis, prefix="quota:auth"
        )

        # 4. File store
        current_phase = _boot_phase("file_store")
        from bp_router.storage import build_file_store  # noqa: PLC0415

        state.file_store = build_file_store(settings)

        # 5. ACL — load firewall rules from DB. The acl_rules table is
        #    the only source of truth; migration 0001 ships a
        #    bootstrap rule.
        current_phase = _boot_phase("acl_rules")
        from bp_router.acl import Rule, RuleSet  # noqa: PLC0415
        from bp_router.db import queries as _q  # noqa: PLC0415

        async with state.db_pool.acquire() as _conn:  # type: ignore[attr-defined]
            _rule_rows = await _q.list_acl_rules(_conn)
        state.rules = RuleSet([
            Rule(
                rule_id=r.rule_id,
                ord=r.ord,
                name=r.name,
                description=r.description,
                effect=r.effect,  # type: ignore[arg-type]
                user_level=r.user_level,
                caller_pattern=r.caller_pattern,
                callee_pattern=r.callee_pattern,
            )
            for r in _rule_rows
        ])
        logger.info(
            "acl_rules_loaded",
            extra={
                "event": "acl_rules_loaded",
                "rule_count": len(state.rules),
            },
        )

        # 5b. Built-in agents. The `admin_console` synthetic caller
        # backs POST /v1/admin/tasks/test and similar admin-driven
        # admit paths.
        current_phase = _boot_phase("builtin_agents")
        await _ensure_admin_console_agent(state)

        # 5c. First-admin bootstrap. Only fires when
        # both `ROUTER_BOOTSTRAP_ADMIN_EMAIL` and
        # `ROUTER_BOOTSTRAP_ADMIN_PASSWORD` are set. Idempotent: a
        # row with that email already in `users` is left alone — safe
        # to leave the env vars set across restarts.
        await _bootstrap_admin_user(state)

        # 5d. MCP bridge service principal. Only fires when
        # ROUTER_MCP_BRIDGE_SECRET is set. Idempotent + recovery-safe:
        # seeds the fixed `service_mcp` user and (re)arms the env secret
        # as its refresh token on every startup.
        await _bootstrap_mcp_bridge_user(state)

        # 6. LLM service.
        #    Load (or seed-on-first-startup) the `llm_presets` table
        #    into the in-memory preset map. Failure here is non-fatal:
        #    the service falls back to its hard-coded default presets
        #    so the router still starts up if the DB is briefly
        #    unavailable.
        current_phase = _boot_phase("llm_presets")
        from bp_router.llm import LlmService  # noqa: PLC0415

        state.llm_service = LlmService(settings)
        try:
            async with state.db_pool.acquire() as conn:
                n = await state.llm_service.load_presets_from_db(conn)
            logger.info(
                "llm_presets_loaded",
                extra={"event": "llm_presets_loaded", "count": n},
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "llm_presets_load_failed",
                extra={"event": "llm_presets_load_failed"},
            )

        # 7. Correlation + socket registry
        current_phase = _boot_phase("correlation")
        from bp_router.correlation import PendingAcks  # noqa: PLC0415
        from bp_router.ws_hub import (  # noqa: PLC0415
            SocketRegistry,
            _CatalogCache,
        )

        state.correlation = PendingAcks(
            default_timeout_s=settings.pending_ack_timeout_s,
        )
        # Start the reaper that converts expired pending-ack deadlines
        # into `TimeoutError` rejections. Without this,
        # `delivery.deliver_frame`'s `await fut` hangs forever when an
        # agent disconnects mid-flight or simply doesn't ack — the
        # timeout source-of-truth lives here, not at the call site.
        state.correlation.start_reaper()
        state.socket_registry = SocketRegistry()
        # WS-handshake thundering-herd guards (see settings
        # docstrings). Created here (inside the running loop) so the
        # asyncio primitives bind to the right loop.
        state.ws_handshake_semaphore = asyncio.Semaphore(
            settings.ws_handshake_max_concurrent
        )
        state.agent_catalog_cache = _CatalogCache(
            ttl_s=settings.ws_handshake_catalog_cache_ttl_s
        )

        # 8. Background tasks
        current_phase = _boot_phase("background_tasks")
        from bp_router.tasks import start_background_loops  # noqa: PLC0415

        bg_tasks = await start_background_loops(state)
    except BaseException as exc:
        # The definitive diagnostic line. Names the failing phase so
        # a supervisor crashloop is a one-line fix, not a triage.
        # Re-raised after best-effort cleanup: a fatal misconfig
        # SHOULD still exit the process — we make it legible, not
        # silent.
        logger.critical(
            "boot_failed",
            extra={
                "event": "boot_failed",
                "phase": current_phase,
                "error": repr(exc),
            },
            exc_info=True,
        )
        await _boot_cleanup(state)
        raise

    logger.info("router_started", extra={"event": "router_started"})

    try:
        yield
    finally:
        # Graceful shutdown order:
        #
        #   1. Close live WS sockets with code 1001 ("going away") so
        #      clients see a clean close frame instead of a TCP RST.
        #   2. Cancel router-side LLM tasks per socket so we stop
        #      burning provider tokens on requests whose results will
        #      never be delivered.
        #   3. Cancel + await ALL background tasks (the named bg_tasks
        #      from start_background_loops() AND the strong-ref set
        #      populated by spawn_background()) BEFORE closing the
        #      pool, otherwise any in-flight DB use raises
        #      InterfaceError on the way down.
        #   4. Stop the PendingAcks reaper.
        #   5. Close the DB pool.
        #   6. Close Redis.
        await _shutdown_live_sockets(state)
        await _drain_background_tasks(state, bg_tasks)
        await state.correlation.stop_reaper()  # type: ignore[attr-defined]
        # Mounted sub-apps don't run their own lifespan, so we
        # explicitly tear down resources we eagerly populated at
        # mount time. Walks `app.routes` and
        # closes any `state.upstream` we find — generic, so any
        # future mounted sub-app following the same pattern is
        # cleaned up too.
        await _shutdown_mounted_subapps(app)
        await state.db_pool.close()  # type: ignore[attr-defined]
        if state.redis is not None:
            await state.redis.aclose()  # type: ignore[attr-defined]
        logger.info("router_stopped", extra={"event": "router_stopped"})


async def _shutdown_live_sockets(state: AppState) -> None:
    """Close every live WS socket and cancel + await its router-side
    LLM tasks.

    Best-effort: errors per socket are logged but never propagated, so a
    misbehaving close doesn't block shutdown of other sockets. LLM
    tasks are awaited (with `return_exceptions=True`) so they reach
    `cancelled` state before the event loop closes — otherwise
    cancelled-but-pending tasks generate warnings during interpreter
    teardown.
    """
    registry = state.socket_registry  # type: ignore[attr-defined]
    live_ids = registry.live_agent_ids()
    cancelled_llm: list[asyncio.Task] = []
    for agent_id in live_ids:
        entry = registry.get(agent_id)
        if entry is None:
            continue
        # Cancel in-flight provider calls so we don't keep streaming
        # bytes for a connection we're about to drop.
        for _cid, task in list(entry.llm_tasks.items()):
            if not task.done():
                task.cancel()
                cancelled_llm.append(task)
        entry.llm_tasks.clear()
        entry.closed.set()
        try:
            await entry.websocket.close(code=1001, reason="router_shutdown")
        except Exception:  # noqa: BLE001
            logger.debug(
                "shutdown_socket_close_failed",
                extra={
                    "event": "shutdown_socket_close_failed",
                    "bp.agent_id": agent_id,
                },
                exc_info=True,
            )
    if cancelled_llm:
        await asyncio.gather(*cancelled_llm, return_exceptions=True)


async def _drain_background_tasks(
    state: AppState, named_bg_tasks: list[asyncio.Task]
) -> None:
    """Cancel + await every tracked background task.

    Two sources: the named loop tasks returned by
    `start_background_loops` and the strong-ref set populated by
    `spawn_background`. The set may grow even during this call (the
    cancelled WS sockets above can spawn a final cleanup task), so
    snapshot once.
    """
    pending = list(named_bg_tasks) + list(state.bg_tasks)
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _shutdown_mounted_subapps(app: FastAPI) -> None:
    """Close `state.upstream` on every mounted sub-app.

    Mounted sub-apps don't get their own `lifespan` invocation
    (FastAPI runs only the top-level app's), so resources eagerly
    populated at mount time need to be torn down by the parent
    Best-effort: per-subapp errors are logged and swallowed so a
    misbehaving sub-app can't block shutdown of others.
    """
    for route in app.routes:
        sub = getattr(route, "app", None)
        if sub is None:
            continue
        upstream = getattr(getattr(sub, "state", None), "upstream", None)
        if upstream is None:
            continue
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001
            logger.debug(
                "mounted_subapp_aclose_failed",
                extra={
                    "event": "mounted_subapp_aclose_failed",
                    "path": getattr(route, "path", "?"),
                },
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Built-in agents
# ---------------------------------------------------------------------------


ADMIN_CONSOLE_AGENT_ID = "admin_console"


async def _bootstrap_admin_user(state: AppState) -> None:
    """Seed the first admin user from env vars.

    Without this, a fresh template clone has no admin and no
    documented way to create one — `POST /v1/admin/users` requires
    an existing admin (Depends(require_admin)), and the admin UI
    can't be logged into without a user. Operators historically
    had to write a one-off Python script using `hash_password` +
    direct DB writes, which requires source-tree knowledge.

    Both `ROUTER_BOOTSTRAP_ADMIN_EMAIL` and
    `ROUTER_BOOTSTRAP_ADMIN_PASSWORD` must be set
    (`Settings._bootstrap_admin_pair_consistent` rejects half-set
    configs at startup). Idempotent — a row with the configured
    email already in `users` is left alone, so leaving the env
    vars set across restarts is safe.
    """
    settings = state.settings  # type: ignore[attr-defined]
    email = getattr(settings, "bootstrap_admin_email", None)
    password = getattr(settings, "bootstrap_admin_password", None)
    if email is None or password is None:
        return  # not configured

    from bp_router.db import queries as _q  # noqa: PLC0415
    from bp_router.security.passwords import hash_password  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        existing = await _q.get_user_by_email(conn, email)
        if existing is not None:
            # WARNING (not INFO) because operators routinely set
            # ROUTER_BOOTSTRAP_ADMIN_PASSWORD expecting it to rotate
            # the password. It DOESN'T — this path is idempotent by
            # email only; the password is left untouched. Loud is
            # better than silent here.
            logger.warning(
                "bootstrap_admin_password_not_updated",
                extra={
                    "event": "bootstrap_admin_password_not_updated",
                    "email": email,
                    "user_id": existing.user_id,
                    "reason": (
                        "user already exists; bootstrap is idempotent "
                        "by email only. Use POST /v1/auth/change-password "
                        "or the admin UI to rotate."
                    ),
                },
            )
            return
        user = await _q.insert_user(
            conn,
            email=email,
            level="admin",
            auth_kind="password",
            auth_secret_hash=hash_password(password.get_secret_value()),
        )
    logger.info(
        "bootstrap_admin_created",
        extra={
            "event": "bootstrap_admin_created",
            "email": email,
            "user_id": user.user_id,
        },
    )


async def _bootstrap_mcp_bridge_user(state: AppState) -> None:
    """Seed the MCP bridge's fixed `service_mcp` principal from
    `ROUTER_MCP_BRIDGE_SECRET`.

    Idempotent and recovery-safe: ensures an email-less `level=service` user
    `service_mcp` exists (mirroring the auto-provisioned `usr_service_*`
    agents) and (re)arms the env secret as its refresh token on EVERY startup,
    so the bridge can present it to `/v1/auth/refresh` for short-lived access
    tokens (rotating + persisting like any other service principal). Re-arming
    keeps the env secret a valid recovery credential after a wiped bridge
    volume. Unset secret = bridge not provisioned (returns early)."""
    import hashlib  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from bp_router.db import queries as _q  # noqa: PLC0415
    from bp_router.principals import MCP_BRIDGE_USER_ID  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    secret = getattr(settings, "mcp_bridge_secret", None)
    if secret is None:
        return  # not configured

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await _q.get_user_by_id(conn, MCP_BRIDGE_USER_ID)
            if existing is None:
                await _q.insert_user(
                    conn,
                    user_id=MCP_BRIDGE_USER_ID,
                    email=None,
                    level="service",
                    auth_kind="api_key",
                    auth_secret_hash=None,
                )
            elif existing.level != "service" or existing.deleted_at is not None:
                # A non-service or soft-deleted row under the reserved name is a
                # conflict we refuse to silently reuse (mirrors onboard.py).
                logger.warning(
                    "mcp_bridge_user_conflict",
                    extra={
                        "event": "mcp_bridge_user_conflict",
                        "user_id": MCP_BRIDGE_USER_ID,
                        "level": existing.level,
                    },
                )
                return
            token_hash = hashlib.sha256(
                secret.get_secret_value().encode("utf-8")
            ).hexdigest()
            await _q.arm_refresh_token(
                conn,
                token_hash=token_hash,
                user_id=MCP_BRIDGE_USER_ID,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.refresh_token_ttl_s),
            )
    logger.info(
        "mcp_bridge_user_bootstrapped",
        extra={
            "event": "mcp_bridge_user_bootstrapped",
            "user_id": MCP_BRIDGE_USER_ID,
        },
    )


async def _ensure_admin_console_agent(state: AppState) -> None:
    """Idempotent: insert the synthetic `admin_console` agent if absent.

    Used as the synthetic caller for `POST /v1/admin/tasks/test`. Marked
    `hidden=true` so the SDK's tool builder skips it; the bootstrap ACL
    rules in migration 0001 protect the `admin` group from external
    invocation.
    """
    from bp_protocol.types import AgentInfo  # noqa: PLC0415
    from bp_router.db import queries as _q  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        existing = await _q.get_agent(conn, ADMIN_CONSOLE_AGENT_ID)
        if existing is not None:
            return
        info = AgentInfo(
            agent_id=ADMIN_CONSOLE_AGENT_ID,
            description="Built-in synthetic caller for admin-only admit paths.",
            groups=["admin"],
            capabilities=[],
            hidden=True,
        )
        await _q.insert_agent(
            conn,
            agent_id=info.agent_id,
            kind="embedded",
            capabilities=info.capabilities,
            groups=info.groups,
            agent_info=info.model_dump(),
        )
    logger.info(
        "admin_console_agent_ensured",
        extra={"event": "admin_console_agent_ensured"},
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the FastAPI app. Routers and middleware are registered here."""
    settings = load_settings()
    app = FastAPI(
        title="bp_router",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Body-size cap FIRST so it precedes lifespan-injected middleware.
    # Exempts `/v1/files` (which has its own streaming cap).
    from bp_router.security.body_size import (  # noqa: PLC0415
        BodySizeLimitMiddleware,
    )
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes
    )

    # HTTP routers (LLM calls go over the WebSocket frame channel —
    # see bp_router.dispatch._handle_llm_request and bp_protocol's
    # LlmRequest/LlmDelta/LlmResult frames.)
    from bp_router.api import (  # noqa: PLC0415
        admin,
        auth,
        files,
        health,
        onboard,
        registrations,
        sessions,
        tasks,
    )

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/v1/auth", tags=["auth"])
    app.include_router(sessions.router, prefix="/v1/sessions", tags=["sessions"])
    app.include_router(tasks.router, prefix="/v1/tasks", tags=["tasks"])
    app.include_router(files.router, prefix="/v1/files", tags=["files"])
    app.include_router(onboard.router, prefix="/v1", tags=["onboard"])
    app.include_router(
        registrations.router, prefix="/v1/registrations", tags=["registrations"]
    )
    app.include_router(admin.router, prefix="/v1/admin", tags=["admin"])

    # WebSocket endpoint
    from bp_router.ws_hub import register_ws_endpoint  # noqa: PLC0415

    register_ws_endpoint(app)

    # Admin web UI (bp_admin) — same-process mount under /admin.
    # Disable via ROUTER_SERVE_ADMIN_UI=false to deploy split-process.
    settings = load_settings()
    if settings.serve_admin_ui:
        _mount_admin_ui(app, settings)

    return app


def _mount_admin_ui(app: FastAPI, settings: Settings) -> None:
    """Mount bp_admin under /admin. Imported lazily so deployments
    that don't install the `admin` extra (jinja2) can still run the
    router."""
    try:
        from bp_admin.app import create_app as create_admin_app  # noqa: PLC0415
        from bp_admin.config import AdminConfig  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "admin_ui_unavailable",
            extra={
                "event": "admin_ui_unavailable",
                "reason": "bp_admin / jinja2 not installed; pip install backplaned[admin]",
            },
        )
        return

    if settings.admin_session_secret is None:
        # Settings validator should have already rejected this, but be
        # defensive in case someone bypasses validation in tests.
        logger.error(
            "admin_ui_secret_missing",
            extra={"event": "admin_ui_secret_missing"},
        )
        return

    admin_config = AdminConfig(
        router_url=f"http://127.0.0.1:{settings.bind_port}",
        session_secret=settings.admin_session_secret,
        session_cookie_secure=(settings.deployment_env == "prod"),
        deployment_env=settings.deployment_env,
        log_level=settings.log_level,
    )
    admin_app = create_admin_app(admin_config)
    # Pre-populate `admin_app.state.upstream` because Starlette /
    # FastAPI does NOT run a mounted sub-app's `lifespan` — it only
    # runs the top-level app's. Without this eager construction
    # every `/admin/*` request 500s on
    # `AttributeError: 'State' object has no attribute 'upstream'`
    # the first time the auth middleware tries to read it
    # The standalone `bp-admin` console-script path still uses
    # `_lifespan` in `bp_admin.app`.
    from bp_admin.upstream import UpstreamClient  # noqa: PLC0415

    admin_app.state.upstream = UpstreamClient(
        admin_config.router_url,
        timeout_s=admin_config.upstream_timeout_s,
    )
    app.mount("/admin", admin_app)
    logger.info(
        "admin_ui_mounted",
        extra={
            "event": "admin_ui_mounted",
            "path": "/admin",
            "router_url": admin_config.router_url,
        },
    )
