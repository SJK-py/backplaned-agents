"""Thin httpx wrapper around the router's `/v1/admin/mcp-servers`
endpoints, authenticated as the `service_mcp` service principal.

The bridge supervisor uses this to:
  * `list_mcp_servers()` — poll the config table.
  * `record_tools_refreshed(server_id, tools_cache)` — write back
    after a successful upstream tools/list.

Auth (unified with every other service principal): the bridge holds a
refresh token — seeded from `ROUTER_MCP_BRIDGE_SECRET` (the env secret) and
rotated on use — which it exchanges at `/v1/auth/refresh` for short-lived
access tokens. The rotated refresh token is persisted to `state_dir` so the
bridge resumes across restarts; if the persisted token is rejected (wiped
volume, expiry), it falls back to the env secret, which the router re-arms on
startup. The bridge does NOT mint invitations — admin actions stash a
short-TTL onboarding invitation on each `mcp_servers` row instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Refresh the access token this many seconds before it expires, so an
# in-flight request never races the boundary.
_ACCESS_REFRESH_BUFFER_S = 60.0


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AdminApiError(RuntimeError):
    """Wraps non-2xx admin API responses with a meaningful message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"admin API {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AdminClient:
    """One AdminClient = one router base URL + the `service_mcp` credential.

    Owns an `httpx.AsyncClient`; callers MUST `await aclose()` on shutdown."""

    def __init__(
        self,
        base_url: str,
        *,
        refresh_token: str,
        state_dir: Path,
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._env_refresh_token = refresh_token
        self._token_path = state_dir / "service_token.json"
        self._client = httpx.AsyncClient(timeout=timeout_s)
        # Resume a rotated refresh token across restarts; else bootstrap from
        # the env secret.
        self._refresh_token = self._load_refresh_token() or refresh_token
        self._access_token: str | None = None
        self._access_expiry: datetime | None = None
        # Serialize refreshes: the refresh token rotates single-use, so two
        # concurrent exchanges with the same token would race (one wins, the
        # other invalidates the family).
        self._refresh_lock = asyncio.Lock()

    # -- token persistence --------------------------------------------------

    def _load_refresh_token(self) -> str | None:
        try:
            data = json.loads(self._token_path.read_text())
        except (OSError, ValueError):
            return None
        tok = data.get("refresh_token")
        return tok if isinstance(tok, str) and tok else None

    def _persist_refresh_token(self, token: str) -> None:
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(json.dumps({"refresh_token": token}))
        except OSError:
            # Non-fatal: we keep the rotated token in memory for this run.
            # On restart we'll fall back to the env secret (router-armed).
            logger.warning(
                "mcp_bridge_token_persist_failed",
                extra={"event": "mcp_bridge_token_persist_failed"},
            )

    # -- auth ---------------------------------------------------------------

    async def _ensure_access(self) -> None:
        now = datetime.now(UTC)
        fresh = (
            self._access_token is not None
            and self._access_expiry is not None
            and (self._access_expiry - now).total_seconds() > _ACCESS_REFRESH_BUFFER_S
        )
        if fresh:
            return
        async with self._refresh_lock:
            # Re-check under the lock: a concurrent caller may have refreshed.
            now = datetime.now(UTC)
            if (
                self._access_token is not None
                and self._access_expiry is not None
                and (self._access_expiry - now).total_seconds()
                > _ACCESS_REFRESH_BUFFER_S
            ):
                return
            await self._refresh(self._refresh_token, allow_env_fallback=True)

    async def _refresh(self, token: str, *, allow_env_fallback: bool) -> None:
        resp = await self._client.post(
            f"{self._base_url}/v1/auth/refresh",
            json={"refresh_token": token},
        )
        if resp.status_code != 200:
            # A stale persisted token → retry once with the env secret, which
            # the router re-arms on startup (recovery after a wiped volume).
            if allow_env_fallback and token != self._env_refresh_token:
                logger.info(
                    "mcp_bridge_refresh_fallback_to_env",
                    extra={"event": "mcp_bridge_refresh_fallback_to_env"},
                )
                await self._refresh(
                    self._env_refresh_token, allow_env_fallback=False
                )
                return
            self._raise_or_json(resp)
        data = resp.json()
        self._access_token = data["access_token"]
        self._access_expiry = _parse_dt(data["expires_at"])
        self._refresh_token = data["refresh_token"]
        self._persist_refresh_token(self._refresh_token)

    async def _request(
        self, method: str, path: str, **kw: Any
    ) -> httpx.Response:
        await self._ensure_access()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        resp = await self._client.request(
            method, f"{self._base_url}{path}", headers=headers, **kw
        )
        if resp.status_code == 401:
            # Access token rejected mid-life — force one refresh + retry.
            self._access_token = None
            await self._ensure_access()
            headers = {"Authorization": f"Bearer {self._access_token}"}
            resp = await self._client.request(
                method, f"{self._base_url}{path}", headers=headers, **kw
            )
        return resp

    # -- admin API ----------------------------------------------------------

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """`GET /v1/admin/mcp-servers` — the full row set to reconcile."""
        return self._raise_or_json(await self._request("GET", "/v1/admin/mcp-servers"))

    async def record_tools_refreshed(
        self, server_id: str, *, tools_cache: dict[str, Any]
    ) -> None:
        """`POST /v1/admin/mcp-servers/{id}/tools-refreshed` — atomically
        writes tools_cache, stamps last_connected_at, clears the pending
        invitation + refresh request."""
        self._raise_or_json(
            await self._request(
                "POST",
                f"/v1/admin/mcp-servers/{server_id}/tools-refreshed",
                json={"tools_cache": tools_cache},
            )
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _raise_or_json(resp: httpx.Response) -> Any:
        if 200 <= resp.status_code < 300:
            if resp.content:
                return resp.json()
            return None
        try:
            detail = resp.json().get("detail") or resp.text
        except Exception:  # noqa: BLE001
            detail = resp.text or "(no body)"
        raise AdminApiError(resp.status_code, str(detail)[:500])
