"""bp_router.ws_hub — WebSocket endpoint and live socket registry.

One socket per agent; supersede semantics: a new Hello with the
same agent_id closes the previous socket with WS close code
**4003** (`reason="superseded"`). The rate-limit close uses
**4029** and auth-failed uses **4001** — three distinct codes in
the 4000-4999 private range so a WS client can tell them apart
without scraping the reason string.

**Resume window semantics.** On a graceful disconnect the
SocketEntry is parked in `SocketRegistry._resume` for
`settings.resume_window_s`. The parked entry's `outbox` (containing
any not-yet-sent frames) and its `inflight_correlations` are
**preserved** — a reconnect within the window picks up exactly
where the old socket left off. Frames bound for the agent
DURING the disconnect gap are NOT queued on the parked entry's
outbox: `bp_router/delivery.py` checks `_live` only and returns
`AgentNotConnected` for offline agents. So the window covers
"resume a socket that already has frames pending in its outbox"
and "preserve frame-level ack futures across the reconnect" but
NOT "queue new frames for delivery". The latter would require
durable persistence — out of scope; see `tasks.py` for the
Result-frame path that already persists task state in the DB.

See `docs/router/protocol.md` §3.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.errors import safe_validator_message
from bp_protocol.frames import (
    ErrorCode,
    ErrorFrame,
    Frame,
    HelloFrame,
    PingFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)
from bp_router.db import queries
from bp_router.observability import metrics
from bp_router.security.jwt import TokenError, is_jti_revoked, verify_agent_token
from bp_router.visibility import available_destinations

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_router.app import AppState
    from bp_router.db.models import AgentRow

logger = logging.getLogger(__name__)


def _merged_hello_agent_info(
    existing_info: dict | None, hello: AgentInfo
) -> tuple[dict, list[str], list[str]] | None:
    """Merge a reconnecting agent's Hello-declared `agent_info` onto its
    stored record, returning `(info_dump, groups, capabilities)` when the
    result differs (so the handshake should persist it), else `None`.

    Honors the SDK's "publish the current snapshot on connect" contract
    ([bp_sdk/agent.py]) so a restart with changed modes / capabilities
    takes effect without re-onboarding. Restricted to the same self-mutable
    fields as `AgentInfoUpdate` (`_AGENT_INFO_MUTABLE_FIELDS`), with
    `agent_id` pinned and the merged shape fully re-validated."""
    from bp_protocol.types import AgentInfo  # noqa: PLC0415
    from bp_router.dispatch import _AGENT_INFO_MUTABLE_FIELDS  # noqa: PLC0415

    existing = dict(existing_info or {})
    hello_dump = hello.model_dump()
    merged = dict(existing)
    for fld in _AGENT_INFO_MUTABLE_FIELDS:
        merged[fld] = hello_dump.get(fld)
    # agent_id is locked to the stored (authenticated) record — never
    # taken from the Hello-declared info.
    merged["agent_id"] = existing.get("agent_id") or hello.agent_id
    if merged == existing:
        return None
    validated = AgentInfo.model_validate(merged)  # defensive; raises on bad shape
    return validated.model_dump(), list(validated.groups), list(validated.capabilities)


# ---------------------------------------------------------------------------
# Per-socket state
# ---------------------------------------------------------------------------


@dataclass
class SocketEntry:
    agent_id: str
    websocket: WebSocket
    session_token: str
    # JWT `jti` claim of the token that authenticated this socket.
    # `/agent/refresh-token` consults this on rotation: if the
    # rotated jti matches the active socket's, the socket is
    # force-closed so the new (post-rotate) connect uses the new
    # token. Empty
    # string means "unknown" — only used in resume reconstruction
    # paths where we trust the prior handshake's checks.
    auth_jti: str = ""
    # The default factory uses the pre-R6 hardcoded 256-frame cap;
    # production callers (`_handshake`) override via the
    # `_new_outbox(settings)` helper so `settings.per_socket_outbox_max`
    # actually takes effect. The default-factory shape is preserved so
    # tests / tooling that build a `SocketEntry` without settings still
    # get a sensible queue.
    outbox: asyncio.Queue[Frame] = field(
        default_factory=lambda: asyncio.Queue(256)
    )
    last_recv: float = 0.0
    last_send: float = 0.0
    inflight_correlations: set[str] = field(default_factory=set)
    llm_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    """correlation_id → router-side asyncio.Task running an LLM call.

    Cancelled by `_on_disconnect` so a dropped client doesn't keep
    consuming provider tokens.
    """
    closed: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SocketRegistry:
    """`agent_id → SocketEntry` with supersede + resume support."""

    def __init__(self) -> None:
        self._live: dict[str, SocketEntry] = {}
        self._resume: dict[str, SocketEntry] = {}

    async def attach(self, entry: SocketEntry) -> SocketEntry | None:
        previous = self._live.pop(entry.agent_id, None)
        self._live[entry.agent_id] = entry
        return previous

    async def detach(
        self,
        agent_id: str,
        *,
        into_resume: bool,
        expected: SocketEntry | None = None,
    ) -> SocketEntry | None:
        """Remove the live socket entry for `agent_id`.

        When `expected` is provided, the pop only fires if the
        current `_live[agent_id]` IS that entry. This guards
        against the supersede-then-old-disconnect race:

            1. Agent reconnects → `attach(new)` → `_live[a]` now
               points at `new`; `previous` (the old socket) is
               returned but still has its own `_send_loop` and
               `_recv_loop` finishing up.
            2. Old socket's `_on_disconnect` runs `detach(a, …)`.
               Without identity check, it pops `_live[a]` — which
               is now `new`! The new live socket is silently
               unregistered; `delivery.py` then sees
               `AgentNotConnected` for inbound frames.

        Identity check via `is` (object identity) — agent_id alone
        isn't enough to distinguish old from new.
        """
        if expected is not None and self._live.get(agent_id) is not expected:
            # The live slot now points at a DIFFERENT entry (the
            # new connection that superseded us). Leave it alone.
            return None
        entry = self._live.pop(agent_id, None)
        if entry is not None and into_resume:
            self._resume[agent_id] = entry
        return entry

    def consume_resume(
        self, agent_id: str, token: str
    ) -> SocketEntry | None:
        entry = self._resume.get(agent_id)
        if entry is None or entry.session_token != token:
            return None
        return self._resume.pop(agent_id)

    def expire_resume(self, agent_id: str, entry: SocketEntry) -> bool:
        """Pop `entry` from the resume map iff it is still the parked one.

        Returns True if popped, False if a fresher reconnect already
        consumed (or replaced) the parked entry. Callers use the boolean
        to decide whether the resume window expired with no reconnect
        and in-flight tasks must therefore be failed.
        """
        parked = self._resume.get(agent_id)
        if parked is not entry:
            return False
        del self._resume[agent_id]
        return True

    def get(self, agent_id: str) -> SocketEntry | None:
        return self._live.get(agent_id)

    def live_agent_ids(self) -> list[str]:
        return list(self._live.keys())

    def __len__(self) -> int:
        return len(self._live)


# ---------------------------------------------------------------------------
# WS endpoint
# ---------------------------------------------------------------------------


def register_ws_endpoint(app: FastAPI) -> None:
    """Register the `/v1/agent` WebSocket endpoint on the FastAPI app."""

    @app.websocket("/v1/agent")
    async def agent_ws(ws: WebSocket) -> None:
        await ws.accept()
        state: AppState = ws.app.state.bp

        # Per-IP handshake rate-limit BEFORE we read the Hello and
        # spend CPU on JWT verify + Redis revocation lookup. A
        # flooding IP otherwise burns auth machinery on every
        # connect attempt. Mirrors `login_rate_limit_per_ip_*` from
        # the HTTP login path.
        try:
            denied = await _handshake_rate_limit_denied(ws, state)
        except Exception:  # noqa: BLE001
            # A misconfigured / unreachable quota backing store
            # must not lock the WS endpoint shut. Log and fail-open.
            logger.exception(
                "ws_handshake_rate_limit_check_failed",
                extra={"event": "ws_handshake_rate_limit_check_failed"},
            )
            denied = False
        if denied:
            # 4029 is the de-facto rate-limit close code in the
            # 4000-4999 private range; 1008 (policy violation) is
            # the spec-blessed alternative. We pick 4029 to be
            # diagnosable in client logs.
            await ws.close(code=4029, reason="rate_limited")
            return

        try:
            entry = await _handshake(ws, state)
        except _HandshakeFailed as exc:
            logger.warning(
                "agent_handshake_failed",
                extra={"event": "agent_handshake_failed", "reason": exc.reason},
            )
            try:
                await ws.send_text(
                    serialize_frame(
                        ErrorFrame(
                            agent_id="router",
                            trace_id="0" * 32,
                            span_id="0" * 16,
                            code=exc.code,
                            message=exc.reason,
                        )
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            await ws.close(code=exc.close_code, reason=exc.reason)
            return

        try:
            await _run_socket(entry, state)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("agent_socket_loop_failed")
        finally:
            await _on_disconnect(entry, state)


# ---------------------------------------------------------------------------
# Handshake rate limiting
# ---------------------------------------------------------------------------


async def _handshake_rate_limit_denied(
    ws: WebSocket, state: AppState
) -> bool:
    """Consume one token from the per-IP handshake bucket. Returns
    True iff the IP is over its budget — the caller closes the WS
    with code 4029 in that case.

    Uses `ws.client.host` directly. The router is the source of
    truth for which IPs to trust; if it sits behind a reverse
    proxy operators must terminate the proxy chain there (or
    extend this helper to read X-Forwarded-For under a separate
    allowlist setting — same pattern as the HTTP login path
    leaves to the operator)."""
    settings = state.settings  # type: ignore[attr-defined]
    rate = settings.ws_handshake_rate_limit_per_ip_per_s
    burst = settings.ws_handshake_rate_limit_per_ip_burst
    if rate <= 0:
        return False  # disabled
    client_host = ws.client.host if ws.client else "unknown"
    bucket_key = f"ws_handshake:ip:{client_host}"
    decision = await state.login_quota.try_consume(  # type: ignore[attr-defined]
        bucket_key,
        rate_per_s=rate,
        burst=burst,
    )
    if decision.allowed:
        return False
    logger.warning(
        "ws_handshake_rate_limited",
        extra={
            "event": "ws_handshake_rate_limited",
            "client_host": client_host,
            "retry_after_s": decision.retry_after_s,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class _HandshakeFailed(Exception):
    def __init__(
        self, reason: str, *, code: str = "auth_failed", close_code: int = 4001
    ) -> None:
        self.reason = reason
        self.code = code
        self.close_code = close_code


def _new_outbox(settings) -> asyncio.Queue[Frame]:  # type: ignore[no-untyped-def]
    """Build a per-socket outbox honouring
    `settings.per_socket_outbox_max`.

    Pre-R6 the SocketEntry default_factory hard-coded `Queue(256)`,
    ignoring the Settings field. Operators raising the field for
    bursty deployments saw no effect on the actual queue cap. The
    helper threads the setting through; tests / tooling that build
    a `SocketEntry` without going through `_handshake` still get
    the dataclass default (256), which is a sensible production
    fallback.
    """
    cap = int(getattr(settings, "per_socket_outbox_max", 256))
    return asyncio.Queue(cap)


class _CatalogCache:
    """Short-TTL single-flight cache of `queries.list_agents`.

    A fleet reconnecting in lockstep (router restart, network blip)
    otherwise issues one full `agents` table scan per handshake —
    O(N) rows × N concurrent handshakes = O(N²) DB + deserialise
    work, plus N pool checkouts that starve every other router DB
    op. This collapses the storm to a single shared scan per
    `ttl_s`: the first miss does the round-trip under `_lock`;
    concurrent waiters re-check after the lock and return the
    freshly-filled list (single-flight — no cache-miss stampede).

    Staleness ≤ `ttl_s` and benign: catalog membership is
    "registered + rule-allowed", not "currently online" (see
    `visibility.available_destinations`), so a peer registered in
    the last `ttl_s` just appears in catalogs a beat late.
    `ttl_s <= 0` disables caching (every call scans fresh).
    """

    def __init__(self, *, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._cached: list[AgentRow] | None = None
        self._expires_at = 0.0

    async def get(self, pool) -> list[AgentRow]:  # type: ignore[no-untyped-def]
        if self._ttl_s <= 0.0:
            async with pool.acquire() as conn:
                return await queries.list_agents(conn)

        loop = asyncio.get_running_loop()
        cached = self._cached
        if cached is not None and loop.time() < self._expires_at:
            metrics.ws_handshake_catalog_cache_total.labels(
                result="hit"
            ).inc()
            return cached

        async with self._lock:
            # Re-check under the lock: a concurrent waiter may have
            # refreshed the cache while we were blocked (this is the
            # single-flight collapse — only the first miss hits DB).
            cached = self._cached
            if cached is not None and loop.time() < self._expires_at:
                metrics.ws_handshake_catalog_cache_total.labels(
                    result="hit"
                ).inc()
                return cached
            metrics.ws_handshake_catalog_cache_total.labels(
                result="miss"
            ).inc()
            async with pool.acquire() as conn:
                agents = await queries.list_agents(conn)
            self._cached = agents
            self._expires_at = loop.time() + self._ttl_s
            return agents


async def _handshake(ws: WebSocket, state: AppState) -> SocketEntry:
    """Read Hello, validate auth, register, send Welcome."""
    settings = state.settings  # type: ignore[attr-defined]
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
    except TimeoutError as exc:
        raise _HandshakeFailed("hello_timeout") from exc

    # Cap the Hello frame size BEFORE parse_frame — an unauthenticated
    # client could otherwise send a giant payload and burn Pydantic
    # CPU. The real memory cap lives in uvicorn's `ws_max_size`
    # (`bp_router/__main__.py:34`); this app-level check is
    # defence-in-depth for deployments that override the uvicorn
    # config.
    #
    # Byte-accurate (not `len(raw)`) because multibyte UTF-8 grows
    # up to 4× — a char-count cap would let a 4× emoji payload slip
    # past a byte cap. But: cheap upper-bound short-circuit first.
    # `len(raw) * 4` is the maximum possible UTF-8 byte count; if
    # that's already under the cap, skip the encode() allocation
    # entirely. The common case is a Hello well under 1 KiB —
    # `len(raw) * 4 < 4096 < 1 MiB` short-circuits before we
    # allocate the encoded bytes.
    if len(raw) * 4 > settings.max_payload_bytes:
        if len(raw.encode("utf-8")) > settings.max_payload_bytes:
            raise _HandshakeFailed(
                "hello_too_large",
                code=ErrorCode.FRAME_INVALID,
                close_code=1009,
            )

    try:
        frame = parse_frame(raw)
    except ValidationError as exc:
        raise _HandshakeFailed(
            f"frame_invalid: {safe_validator_message(exc)}",
            code=ErrorCode.FRAME_INVALID,
            close_code=1002,
        ) from exc

    if not isinstance(frame, HelloFrame):
        raise _HandshakeFailed(
            f"expected Hello, got {frame.type}",
            code=ErrorCode.FRAME_INVALID,
            close_code=1002,
        )

    if frame.protocol_version != PROTOCOL_VERSION:
        raise _HandshakeFailed(
            f"protocol_version mismatch: {frame.protocol_version}",
            code=ErrorCode.PROTOCOL_VERSION,
            close_code=1002,
        )

    try:
        principal = verify_agent_token(
            frame.auth_token,
            secret=settings.jwt_secret.get_secret_value(),
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise _HandshakeFailed(f"auth_failed: {exc}", code=ErrorCode.AUTH_FAILED) from exc

    # Per-jti revocation: HTTP paths consult this after `verify_*` so
    # that a rotated / explicitly-revoked token is rejected before
    # natural expiry. The handshake must do the same — otherwise an
    # agent whose jti was revoked (e.g. via `/agent/refresh-token`)
    # could still reconnect on the old token until exp.
    if await is_jti_revoked(state.redis, principal.jti):  # type: ignore[attr-defined]
        raise _HandshakeFailed("auth_failed: revoked", code=ErrorCode.AUTH_FAILED)

    if principal.agent_id != frame.agent_id:
        raise _HandshakeFailed(
            "auth_failed: token sub does not match Hello.agent_id",
            code=ErrorCode.AUTH_FAILED,
        )

    pool = state.db_pool  # type: ignore[attr-defined]
    # Thundering-herd guard: bound the DB-heavy section so a fleet
    # reconnecting in lockstep can't open one pool checkout per
    # handshake and starve every other router DB op. The per-IP
    # bucket upstream rejects unauthenticated floods; this semaphore
    # bounds the *authenticated* storm (sized < db_pool_max_size).
    # Auth/JWT/jti work above stays unbounded — it touches no pool.
    async with state.ws_handshake_semaphore:  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            agent_row = await queries.get_agent(conn, principal.agent_id)
            # Refresh the published AgentInfo from the Hello so a restart
            # with changed modes / capabilities propagates without
            # re-onboarding (onboarding is otherwise the only writer). Only
            # for an active agent, and only when something actually changed.
            if agent_row is not None and agent_row.status == "active":
                try:
                    refreshed = _merged_hello_agent_info(
                        agent_row.agent_info, frame.agent_info
                    )
                except ValidationError:
                    logger.warning(
                        "agent_info_refresh_invalid",
                        extra={"event": "agent_info_refresh_invalid",
                               "bp.agent_id": principal.agent_id},
                    )
                    refreshed = None
                if refreshed is not None:
                    info_dump, groups, capabilities = refreshed
                    await queries.update_agent_info(
                        conn, principal.agent_id, agent_info=info_dump,
                        groups=groups, capabilities=capabilities,
                    )
                    agent_row = await queries.get_agent(conn, principal.agent_id)
                    logger.info(
                        "agent_info_refreshed_on_connect",
                        extra={"event": "agent_info_refreshed_on_connect",
                               "bp.agent_id": principal.agent_id},
                    )
        if agent_row is None:
            raise _HandshakeFailed(
                f"unknown agent: {principal.agent_id}",
                code=ErrorCode.AUTH_FAILED,
            )
        if agent_row.status != "active":
            raise _HandshakeFailed(
                f"agent {agent_row.status}: {principal.agent_id}",
                code=(
                    ErrorCode.AGENT_REMOVED
                    if agent_row.status == "removed"
                    else ErrorCode.AGENT_SUSPENDED
                ),
            )

        # Resume?
        resumed = None
        if frame.resume_token:
            resumed = state.socket_registry.consume_resume(  # type: ignore[attr-defined]
                principal.agent_id, frame.resume_token
            )

        if resumed is not None:
            entry = SocketEntry(
                agent_id=principal.agent_id,
                websocket=ws,
                session_token=resumed.session_token,
                auth_jti=principal.jti,
                outbox=resumed.outbox,
                inflight_correlations=resumed.inflight_correlations,
            )
        else:
            entry = SocketEntry(
                agent_id=principal.agent_id,
                websocket=ws,
                session_token=secrets.token_urlsafe(24),
                auth_jti=principal.jti,
                outbox=_new_outbox(settings),
            )

        previous = await state.socket_registry.attach(entry)  # type: ignore[attr-defined]
        if previous is not None:
            try:
                # Code 4003 in the 4000-4999 private range, distinct
                # from 4001 (auth_failed). A WS client diagnosing a
                # close on the old socket can now tell "I was
                # superseded" from "my token was rejected" without
                # scraping the reason string. Mirrors the rationale
                # for the 4029 rate-limit close code in `agent_ws`.
                # R6 third-pass review.
                await previous.websocket.close(
                    code=4003, reason="superseded"
                )
            except Exception:  # noqa: BLE001
                pass
            previous.closed.set()

        try:
            from bp_router.observability.metrics import (  # noqa: PLC0415
                ws_connected_agents,
            )

            ws_connected_agents.set(len(state.socket_registry))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        # Build Welcome. `list_agents` is the O(N) scan — it goes
        # through the single-flight cache so a reconnecting fleet
        # shares one scan instead of one per handshake.
        # `update_agent_last_seen` is a per-agent single-row write
        # (not shareable), so it keeps its own cheap checkout.
        all_agents = await state.agent_catalog_cache.get(pool)  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            await queries.update_agent_last_seen(conn, principal.agent_id)

        catalog = available_destinations(
            agent_row,
            all_agents,
            state.rules.rules,  # type: ignore[attr-defined]
            max_tier=settings.acl_max_tier,
        )

        welcome = WelcomeFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            session_id=entry.session_token,
            available_destinations=catalog,
            capabilities=agent_row.capabilities,
            heartbeat_interval_ms=settings.heartbeat_interval_ms,
            max_payload_bytes=settings.max_payload_bytes,
        )
        await ws.send_text(serialize_frame(welcome))

    logger.info(
        "agent_connected",
        extra={
            "event": "agent_connected",
            "bp.agent_id": principal.agent_id,
            "resumed": resumed is not None,
        },
    )
    return entry


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


async def _run_socket(entry: SocketEntry, state: AppState) -> None:
    """Run recv, send, and heartbeat coroutines until any one ends."""
    settings = state.settings  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    entry.last_recv = loop.time()
    entry.last_send = loop.time()

    recv_task = asyncio.create_task(_recv_loop(entry, state))
    send_task = asyncio.create_task(_send_loop(entry))
    hb_task = asyncio.create_task(
        _heartbeat_loop(entry, state, interval_s=settings.heartbeat_interval_ms / 1000)
    )

    tasks = [recv_task, send_task, hb_task]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        # Surface any exception so the caller's `except` blocks can log it.
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception(
                    "ws_loop_exception",
                    extra={"event": "ws_loop_exception", "bp.agent_id": entry.agent_id},
                    exc_info=exc,
                )
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _recv_loop(entry: SocketEntry, state: AppState) -> None:
    from bp_router.dispatch import dispatch_frame  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    while not entry.closed.is_set():
        raw = await entry.websocket.receive_text()
        entry.last_recv = loop.time()

        # Defence-in-depth: the actual frame-size cap is enforced one
        # layer down, in uvicorn's WebSocket protocol via
        # `ws_max_size=settings.max_payload_bytes`
        # (`bp_router.__main__:main`). That makes the protocol library
        # close the connection BEFORE allocating the bytes — the OOM
        # vector is closed at the choke point. This post-receive check
        # exists for deployments that don't run via `bp_router.__main__`
        # (e.g. raw `gunicorn -k uvicorn.workers.UvicornWorker`
        # without `ws_max_size` set) and would otherwise inherit
        # uvicorn's 16 MiB default. Byte-accurate check (NOT
        # `len(raw)`) because a UTF-8 string with multibyte chars is
        # up to 4× its character count in bytes; a char-count cap
        # would let multibyte payloads slip past.
        #
        # R8 perf: cheap upper-bound short-circuit. `len(raw) * 4`
        # is the maximum possible UTF-8 byte count (4 bytes per
        # codepoint). If that's already under the cap, skip the
        # encode() allocation entirely. At 1000 frames/sec with
        # ~1 KiB messages, the pre-R8 unconditional encode was
        # ~1 MB/s of pointless byte allocation feeding the GC.
        # The handshake-size check at line 350 already used this
        # pattern; the recv-loop check missed it.
        if len(raw) * 4 > settings.max_payload_bytes:
            if len(raw.encode("utf-8")) > settings.max_payload_bytes:
                await entry.websocket.close(code=1009, reason="payload_too_large")
                return

        try:
            frame = parse_frame(raw)
        except ValidationError as exc:
            err = ErrorFrame(
                agent_id="router",
                trace_id="0" * 32,
                span_id="0" * 16,
                code=ErrorCode.FRAME_INVALID,
                message=safe_validator_message(exc),
            )
            await entry.outbox.put(err)
            continue

        try:
            await dispatch_frame(state, entry, frame)
        except Exception:  # noqa: BLE001
            logger.exception(
                "dispatch_failed",
                extra={
                    "event": "dispatch_failed",
                    "bp.agent_id": entry.agent_id,
                    "bp.frame.type": frame.type,
                },
            )


async def _send_loop(entry: SocketEntry) -> None:
    loop = asyncio.get_running_loop()
    while not entry.closed.is_set():
        frame = await entry.outbox.get()
        try:
            await entry.websocket.send_text(serialize_frame(frame))
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Re-queue the un-sent frame so a subsequent resume
            # connection picks it up. Without this, a frame already
            # popped from the outbox before `send_text` raised is
            # lost — silent drop for Result/Ack frames the agent
            # was expecting. asyncio.Queue doesn't support
            # push-front, so this introduces slight reordering
            # vs other late frames; acceptable for the protocol
            # (most frames are independent; ordered streams like
            # LlmDelta are scoped per correlation_id where this
            # frame's order vs others doesn't change).
            try:
                entry.outbox.put_nowait(frame)
            except asyncio.QueueFull:
                # Outbox full means we can't preserve the frame.
                # Logged so operators can correlate with downstream
                # "missing terminal" reports.
                logger.warning(
                    "frame_dropped_send_failed_queue_full",
                    extra={
                        "event": "frame_dropped_send_failed_queue_full",
                        "bp.agent_id": entry.agent_id,
                        "frame_type": frame.type,
                    },
                )
            raise
        entry.last_send = loop.time()
        # Module-level metrics import (R8 perf): hottest outbound
        # path — every frame to every socket. See the matching
        # comment in dispatch.dispatch_frame.
        try:
            metrics.frames_total.labels(direction="send", type=frame.type).inc()
        except Exception:  # noqa: BLE001
            pass


async def _heartbeat_loop(
    entry: SocketEntry, state: AppState, *, interval_s: float
) -> None:
    loop = asyncio.get_running_loop()
    misses = 0
    max_misses = 2
    while not entry.closed.is_set():
        await asyncio.sleep(interval_s)
        idle = loop.time() - entry.last_recv
        if idle < interval_s:
            misses = 0
            continue
        # Send Ping; expect Pong via ack-correlation.
        ping = PingFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
        )
        fut = state.correlation.register(  # type: ignore[attr-defined]
            ping.correlation_id, timeout_s=interval_s
        )
        # Track per-socket so the Pong handler can verify the
        # ref_correlation_id belongs to THIS socket. Without the membership add
        # here, an attacker socket could craft a Pong with our cid
        # and resolve our heartbeat future — keeping a wedged peer
        # alive past its heartbeat-timeout eviction.
        entry.inflight_correlations.add(ping.correlation_id)
        # put_nowait so a wedged outbox doesn't make the heartbeat
        # block — the outbox being full is itself a liveness symptom.
        try:
            entry.outbox.put_nowait(ping)
        except asyncio.QueueFull:
            entry.inflight_correlations.discard(ping.correlation_id)
            state.correlation.reject(  # type: ignore[attr-defined]
                ping.correlation_id, RuntimeError("outbox_full")
            )
            misses += 1
            if misses >= max_misses:
                logger.info(
                    "heartbeat_outbox_full",
                    extra={
                        "event": "heartbeat_outbox_full",
                        "bp.agent_id": entry.agent_id,
                    },
                )
                try:
                    await entry.websocket.close(
                        code=4002, reason="outbox_full"
                    )
                except Exception:  # noqa: BLE001
                    pass
                entry.closed.set()
                return
            continue
        try:
            await fut
            misses = 0
        except (TimeoutError, asyncio.CancelledError):
            misses += 1
        finally:
            # Whether the Pong arrived, the future timed out, or the
            # task was cancelled — discard the cid from the per-socket
            # tracker so it doesn't leak. (Resolution from
            # `_handle_pong` doesn't auto-discard; it just resolves
            # the future. Per-socket tracker membership is used by
            # _handle_pong to verify the Pong came from the socket
            # we sent the Ping to.)
            entry.inflight_correlations.discard(ping.correlation_id)
        if misses >= max_misses:
            logger.info(
                "heartbeat_timeout",
                extra={
                    "event": "heartbeat_timeout",
                    "bp.agent_id": entry.agent_id,
                },
            )
            try:
                await entry.websocket.close(
                    code=4002, reason="heartbeat_timeout"
                )
            except Exception:  # noqa: BLE001
                pass
            entry.closed.set()
            return


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


async def _on_disconnect(entry: SocketEntry, state: AppState) -> None:
    """Move into resume window if applicable, else fail in-flight tasks."""
    entry.closed.set()
    settings = state.settings  # type: ignore[attr-defined]

    # Cancel any in-flight LLM router-side tasks for this agent so we
    # stop burning provider tokens on a dead client. Wait briefly for
    # cancellation to propagate — without an await, `task.cancel()`
    # only sets the flag; the next provider-streaming chunk still
    # runs (and is billed) before the CancelledError unwinds.
    in_flight = list(entry.llm_tasks.values())
    for task in in_flight:
        task.cancel()
    entry.llm_tasks.clear()
    if in_flight:
        try:
            # Cap the wait so a wedged provider call can't block the
            # disconnect path indefinitely. 2s is generous for the
            # normal case (cancellation through one await point of
            # the SDK call); a stuck task is left to the dispatcher's
            # `_drain_in_flight` to deal with.
            await asyncio.wait_for(
                asyncio.gather(*in_flight, return_exceptions=True),
                timeout=2.0,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            # `gather(return_exceptions=True)` swallows the per-task
            # CancelledError. Only the wait_for timeout (or a stray
            # outer cancellation) reaches here; either way the
            # disconnect path continues.
            pass

    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            ws_connected_agents,
            ws_disconnects_total,
        )

        ws_connected_agents.set(len(state.socket_registry))  # type: ignore[attr-defined]
        ws_disconnects_total.labels(reason="closed").inc()
    except Exception:  # noqa: BLE001
        pass

    # Update `last_seen_at` so admin dashboards reflect the
    # disconnect time rather than the connect time. Pre-R6 the
    # column was set only on `_handshake` success — a long-running
    # connection that eventually dropped looked "last seen" at the
    # connect moment, off by hours.
    try:
        pool = state.db_pool  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            await queries.update_agent_last_seen(conn, entry.agent_id)
    except Exception:  # noqa: BLE001
        # Best-effort — the agent is gone either way; don't fail the
        # disconnect path on a DB hiccup.
        logger.debug(
            "update_agent_last_seen_on_disconnect_failed",
            exc_info=True,
        )

    # Reject any frame-level pending acks tied to this socket's
    # correlations. Pass the socket's own (small) inflight set
    # directly — `reject_ids` does an O(1) pop per id rather than
    # the pre-R8 O(total_pending) global scan on every disconnect.
    if entry.inflight_correlations:
        rejected = state.correlation.reject_ids(  # type: ignore[attr-defined]
            entry.inflight_correlations
        )
        logger.info(
            "rejected_pending_acks_on_disconnect",
            extra={
                "event": "rejected_pending_acks_on_disconnect",
                "bp.agent_id": entry.agent_id,
                "count": rejected,
            },
        )

    # Park into resume window so a fast reconnect can re-attach.
    # `expected=entry` guards against the supersede race: if a new
    # socket has already attached to the same agent_id while we
    # were running disconnect-cleanup, this detach is a no-op
    # (pre-R6 it would have evicted the new live socket from the
    # registry, breaking inbound frame delivery).
    parked = await state.socket_registry.detach(  # type: ignore[attr-defined]
        entry.agent_id, into_resume=True, expected=entry,
    )
    if parked is None:
        # Already superseded by a newer socket; nothing to do.
        return

    # Schedule the resume window: if no Hello+resume_token arrives in time,
    # fail in-flight tasks for this agent. spawn_background keeps a strong
    # reference so the Task isn't GC'd before it fires.
    from bp_router.app import spawn_background  # noqa: PLC0415

    spawn_background(
        state,
        _resume_window_expiry(entry, state, ttl_s=settings.resume_window_s),
    )


async def _resume_window_expiry(
    entry: SocketEntry, state: AppState, *, ttl_s: int
) -> None:
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    await asyncio.sleep(ttl_s)
    # If a new socket attached in the meantime, the resume entry has been
    # consumed and we have nothing to do.
    if not state.socket_registry.expire_resume(entry.agent_id, entry):  # type: ignore[attr-defined]
        return

    failed = await fail_inflight_for_agent(state, entry.agent_id)
    logger.info(
        "agent_disconnect_finalised",
        extra={
            "event": "agent_disconnect_finalised",
            "bp.agent_id": entry.agent_id,
            "failed_tasks": failed,
        },
    )
