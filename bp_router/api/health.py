"""bp_router.api.health — Liveness, readiness, and metrics endpoints."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Always returns 200 if the process is up. Suitable for k8s liveness."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readiness(request: Request) -> Response:
    """Returns 200 only if the router can serve requests.

    Checks DB pool reachability and (when configured) Redis.

    Degraded-boot gate: boot is intentionally Redis-tolerant (it
    does NOT crashloop when a configured Redis is unreachable at
    startup — a transient blip shouldn't take the whole router
    down). But in `staging`/`prod` a router running *without* its
    required Redis has JWT revocation and the cross-worker rate
    caps silently failing open (`is_jti_revoked` returns False when
    `state.redis is None`). Serving traffic in that state is a
    fleet-wide security regression the threat model assumes closed.
    So when Redis is *configured* (`settings.valkey_url` set) but
    `state.redis is None` (boot degraded) and the env is non-dev,
    report NOT ready: the process stays up (no crashloop — a
    restart into a healthy Redis recovers it), but the orchestrator
    won't mark the pod ready / complete the rollout / route
    traffic. A genuinely-down required Redis thus becomes
    orchestrator-level fail-fast instead of a silent revocation
    bypass. Dev is unaffected (single-worker, revocation
    best-effort by design).
    """
    state = request.app.state.bp
    settings = state.settings
    if (
        settings.deployment_env in ("staging", "prod")
        and settings.valkey_url is not None
        and state.redis is None
    ):
        return Response(
            content="not ready: redis configured but unavailable "
            "(degraded boot) — revocation/quota would fail open",
            status_code=503,
        )
    try:
        async with state.db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        if state.redis is not None:
            await state.redis.ping()
            # NOTE (R10): deliberately does NOT touch `redis_health`.
            # `/readyz` is polled every few seconds by the
            # orchestrator; authoring the recovery signal here made
            # the gauge oscillate 0→1→0 during flapping Redis, so
            # `router_redis_health == 0` never fired reliably while
            # revocation/quota were silently degrading per-fallback.
            # The gauge is now owned by the Redis-backed subsystems
            # themselves (rate_limit sets 1 on a real successful
            # op, 0 on fallback), so it sticks at 0 until genuine
            # recovery. This probe only reports readiness via the
            # response code.
    except Exception:  # noqa: BLE001
        return Response(content="not ready", status_code=503)
    return Response(content="ok", status_code=200)


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus exposition.

    Bearer-gated when `ROUTER_METRICS_TOKEN` is set; open otherwise
    (required in dev workflows; rejected at startup in staging/prod
    via `_metrics_token_required_in_non_dev`). Compares the supplied
    bearer with `hmac.compare_digest` so a partial match doesn't
    leak via timing."""
    from bp_router.observability.metrics import render_exposition  # noqa: PLC0415

    state = request.app.state.bp
    settings = state.settings
    token_setting = settings.metrics_token
    if token_setting is not None:
        from bp_router.security.jwt import extract_bearer  # noqa: PLC0415
        provided = extract_bearer(request.headers.get("authorization", ""))
        if provided is None:
            return Response(content="unauthorized", status_code=401)
        expected = token_setting.get_secret_value()
        if not hmac.compare_digest(provided, expected):
            return Response(content="unauthorized", status_code=401)
    body = render_exposition()
    return Response(content=body, media_type="text/plain; version=0.0.4")
