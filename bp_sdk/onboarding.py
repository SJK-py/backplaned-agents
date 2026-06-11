"""bp_sdk.onboarding — First-run agent registration and token refresh."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from bp_protocol.types import AgentInfo
    from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)


# Onboard retry policy. Invitation tokens are single-use, so a 4xx is
# terminal (the token is already burned or the payload is malformed)
# and retry would just waste time. 5xx / transport errors are
# transient — the router may be restarting — and warrant a bounded
# backoff: attempts at 0, 2, 4, 8, 16 seconds (total ~30s before fail).
_ONBOARD_MAX_ATTEMPTS: int = 5
_ONBOARD_BACKOFF_BASE_S: float = 2.0


def _credentials_path(config: AgentConfig):  # type: ignore[no-untyped-def]
    return config.state_dir / "credentials.json"


def _onboard_http_url(config: AgentConfig) -> str:
    if config.onboard_url:
        return config.onboard_url.rstrip("/")
    # Derive: ws[s]://host/v1/agent → http[s]://host
    url = config.router_url
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    if "/v1/" in url:
        url = url.split("/v1/")[0]
    return url.rstrip("/")


_UNSET: Any = object()


def _persist_credentials(
    config: AgentConfig,
    *,
    agent_id: Any = _UNSET,
    auth_token: Any = _UNSET,
    expires_at: Any = _UNSET,
    service_user_id: Any = _UNSET,
    service_refresh_token: Any = _UNSET,
    service_token_expires_at: Any = _UNSET,
) -> None:
    # Merge-write: load the current file and overlay only the fields
    # explicitly passed (`_UNSET` = leave as-is). This is what stops the
    # agent-token refresh loop — which knows nothing about the service
    # credential — from wiping `service_*` on every rotation.
    #
    # Atomic: write tmp, chmod 0600 before rename, then os.replace so the
    # final path either contains the previous value or the new one — no
    # truncated JSON in between. A crash mid-write would otherwise leave
    # credentials.json unparseable; the next start would fall back to
    # re-onboarding, and invitation tokens are single-use, so the agent
    # would never come up again without admin intervention.
    config.state_dir.mkdir(parents=True, exist_ok=True)
    creds_path = _credentials_path(config)
    current: dict[str, Any] = {}
    if creds_path.exists():
        try:
            current = json.loads(creds_path.read_text())
        except Exception:  # noqa: BLE001
            current = {}
    for key, val in (
        ("agent_id", agent_id),
        ("auth_token", auth_token),
        ("expires_at", expires_at),
        ("service_user_id", service_user_id),
        ("service_refresh_token", service_refresh_token),
        ("service_token_expires_at", service_token_expires_at),
    ):
        if val is not _UNSET:
            current[key] = val
    payload = json.dumps(current, indent=2)
    tmp_path = creds_path.with_suffix(creds_path.suffix + ".tmp")
    tmp_path.write_text(payload)
    try:
        tmp_path.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp_path, creds_path)


def persist_service_token(
    config: AgentConfig,
    *,
    refresh_token: str,
    expires_at: str | None,
) -> None:
    """Persist a rotated service refresh token back to credentials.json
    and update `config`.

    The service refresh token rotates on every `/v1/auth/refresh`
    (single-use rotation), so the consumer must call this after each
    rotation; otherwise a restart reloads the stale token and the next
    refresh is rejected."""
    config.service_refresh_token = refresh_token
    config.service_token_expires_at = expires_at
    _persist_credentials(
        config,
        service_refresh_token=refresh_token,
        service_token_expires_at=expires_at,
    )


async def onboard_or_resume(info: AgentInfo, config: AgentConfig) -> None:
    """Ensure config.auth_token is set. Persists creds across restarts.

    1. If `state_dir/credentials.json` exists with a valid token, load it.
    2. Else perform `POST /v1/onboard` using `invitation_token`.
    3. Persist the result with permissions 0600.
    """
    creds_path = _credentials_path(config)
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text())
            token = data.get("auth_token")
            expires_at = data.get("expires_at")
            if token and (expires_at is None or _is_future(expires_at)):
                config.auth_token = token
                config.service_user_id = data.get("service_user_id")
                config.service_refresh_token = data.get(
                    "service_refresh_token"
                )
                config.service_token_expires_at = data.get(
                    "service_token_expires_at"
                )
                return
        except Exception:  # noqa: BLE001
            logger.exception("credentials_load_failed")

    if not config.invitation_token:
        raise RuntimeError(
            "Agent has no auth_token and no invitation_token. Set "
            "AGENT_INVITATION_TOKEN to the token issued by the router admin."
        )

    base = _onboard_http_url(config)
    data = await _post_onboard_with_retry(
        base=base,
        invitation_token=config.invitation_token,
        agent_info=info.model_dump(),
    )

    config.auth_token = data["auth_token"]
    config.service_user_id = data.get("service_user_id")
    config.service_refresh_token = data.get("service_refresh_token")
    config.service_token_expires_at = data.get("service_token_expires_at")
    _persist_credentials(
        config,
        agent_id=data["agent_id"],
        auth_token=data["auth_token"],
        expires_at=data.get("expires_at"),
        service_user_id=config.service_user_id,
        service_refresh_token=config.service_refresh_token,
        service_token_expires_at=config.service_token_expires_at,
    )


async def reonboard_with_invitation(info: AgentInfo, config: AgentConfig) -> bool:
    """Drop a router-rejected credential and re-onboard with the invitation.

    Called by the WS transport when the router closes on CREDENTIAL grounds —
    4001 `auth_failed` (e.g. a `ROUTER_JWT_SECRET` rotated out from under the
    persisted token) or 4003 `agent_reprovision` / `agent_reset`. The agent
    can't tell a stale-signature token from a good one locally (it has no
    secret), so `onboard_or_resume` would happily RESUME the dead token; this
    deletes `credentials.json` first so the next onboard goes through the
    invitation instead.

    Returns True iff a fresh `auth_token` was obtained. Returns False — without
    raising — when re-onboard is impossible (no `invitation_token`). A terminal
    onboard (409 evicted, or 403 for a spent single-use invitation) raises out
    of `_post_onboard_with_retry`; the caller treats any raise as "not
    recovered" and backs off, so a genuinely-stuck agent can't hot-loop.
    """
    if not config.invitation_token:
        logger.warning(
            "reonboard_no_invitation",
            extra={"event": "reonboard_no_invitation", "bp.agent_id": info.agent_id},
        )
        return False
    # Purge the rejected token so onboard_or_resume can't resume it. The
    # invitation is the only trust anchor from here.
    creds_path = _credentials_path(config)
    try:
        creds_path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "credentials_purge_failed",
            extra={"event": "credentials_purge_failed", "bp.agent_id": info.agent_id},
        )
    config.auth_token = None
    await onboard_or_resume(info, config)
    return config.auth_token is not None


async def _post_onboard_with_retry(
    *,
    base: str,
    invitation_token: str,
    agent_info: dict,
) -> dict:
    """POST /v1/onboard with bounded exponential backoff.

    4xx → fail fast (the invitation is already consumed or the payload is
    malformed; retry won't fix either). 5xx and transport errors retry up
    to `_ONBOARD_MAX_ATTEMPTS` with `2 ** n` second backoff, then give up.
    """
    payload = {"invitation_token": invitation_token, "agent_info": agent_info}
    last_exc: Exception | None = None
    for attempt in range(_ONBOARD_MAX_ATTEMPTS):
        if attempt > 0:
            await asyncio.sleep(_ONBOARD_BACKOFF_BASE_S ** attempt)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{base}/v1/onboard", json=payload)
            if 400 <= resp.status_code < 500:
                body_preview = resp.text[:500]
                raise RuntimeError(
                    f"onboard rejected with {resp.status_code}: {body_preview}"
                )
            if resp.status_code >= 500:
                logger.warning(
                    "onboard_router_5xx",
                    extra={
                        "event": "onboard_router_5xx",
                        "attempt": attempt + 1,
                        "status_code": resp.status_code,
                    },
                )
                last_exc = RuntimeError(f"router returned {resp.status_code}")
                continue
            return resp.json()
        except httpx.HTTPError as exc:
            logger.warning(
                "onboard_transient_failure",
                extra={
                    "event": "onboard_transient_failure",
                    "attempt": attempt + 1,
                    "error": repr(exc),
                },
            )
            last_exc = exc
            continue
    raise RuntimeError(
        f"onboard failed after {_ONBOARD_MAX_ATTEMPTS} attempts; "
        f"last error: {last_exc!r}"
    )


def _is_future(iso: str) -> bool:
    # Coerce naive timestamps to UTC so the comparison never raises on the
    # tz-aware paths the router emits.
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:  # noqa: BLE001
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt > datetime.now(UTC)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def decode_token_exp(token: str) -> int | None:
    """Read the `exp` claim from a JWT without verifying the signature.

    The SDK doesn't have the signing secret — it only needs the timing to
    schedule a proactive refresh. PyJWT would also accept
    `options={"verify_signature": False}`, but doing it by hand keeps
    the SDK's runtime dep set lean.

    Returns the unix-seconds expiry, or None if the token is malformed.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


async def refresh_token(config: AgentConfig) -> int | None:
    """Rotate the agent's bearer token via POST /v1/agent/refresh-token.

    Updates `config.auth_token` and persists credentials.json on success.
    Returns the new expiry (unix seconds) or None on failure. Callers
    treat None as "transient — retry shortly".
    """
    token = config.auth_token
    if not token:
        return None

    creds_path = _credentials_path(config)
    agent_id = None
    if creds_path.exists():
        try:
            agent_id = json.loads(creds_path.read_text()).get("agent_id")
        except Exception:  # noqa: BLE001
            pass
    if agent_id is None:
        # Fall back to decoding the JWT's `sub` claim for agent_id.
        try:
            parts = token.split(".")
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            agent_id = payload.get("sub")
        except Exception:  # noqa: BLE001
            logger.warning(
                "refresh_token_no_agent_id",
                extra={"event": "refresh_token_no_agent_id"},
            )
            return None
    if agent_id is None:
        return None

    base = _onboard_http_url(config)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/agent/refresh-token",
                json={"agent_id": agent_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "refresh_token_http_failed",
            extra={"event": "refresh_token_http_failed", "error": repr(exc)},
        )
        return None

    new_token = data.get("auth_token")
    new_expires_at = data.get("expires_at")
    if not new_token:
        return None

    config.auth_token = new_token
    _persist_credentials(
        config,
        agent_id=agent_id,
        auth_token=new_token,
        expires_at=new_expires_at,
    )
    new_exp = decode_token_exp(new_token)
    logger.info(
        "agent_token_refreshed",
        extra={
            "event": "agent_token_refreshed",
            "bp.agent_id": agent_id,
            "expires_at": new_expires_at,
        },
    )
    return new_exp


def schedule_seconds_until_refresh(token: str, *, min_buffer_s: int = 60) -> float:
    """How long to wait before refreshing `token`.

    Refreshes at exp - max(min_buffer_s, ttl/10) so that the buffer scales
    with token lifetime (long-lived → refresh well before expiry; short-
    lived → refresh closer to expiry but still with a sensible margin).

    Returns 0 when the token is already past refresh, or a small positive
    value when the buffer is bigger than the remaining lifetime.
    """
    exp = decode_token_exp(token)
    if exp is None:
        return 300.0  # unknown — recheck later
    now = time.time()
    remaining = exp - now
    if remaining <= 0:
        return 0.0
    buffer = max(min_buffer_s, remaining / 10)
    return max(0.0, remaining - buffer)
