"""Thin httpx wrapper around the router's `/v1/admin/mcp-servers`
endpoints.

The bridge supervisor uses this to:
  * `list_mcp_servers()` — poll config table.
  * `issue_service_invitation(token)` — self-issue a per-agent
    invitation on first onboard.
  * `record_tools_refreshed(server_id, tools_cache)` — write back
    after a successful upstream tools/list.

All calls authenticate with the bridge's admin token. Phase 10c
treats the token as long-lived; failures here propagate as
`AdminApiError` and the supervisor surfaces them in logs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AdminApiError(RuntimeError):
    """Wraps non-2xx admin API responses with a meaningful message."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"admin API {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AdminClient:
    """One AdminClient = one router base URL + one admin token.

    Owns an `httpx.AsyncClient`; callers MUST `await aclose()` on
    shutdown."""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {admin_token}"}
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """`GET /v1/admin/mcp-servers` — returns the full row set
        the supervisor reconciles against."""
        resp = await self._client.get(
            f"{self._base_url}/v1/admin/mcp-servers",
            headers=self._headers,
        )
        return self._raise_or_json(resp)

    async def issue_service_invitation(
        self, token: str, *, expires_in_s: int = 3600
    ) -> None:
        """`POST /v1/admin/invitations` with the caller-supplied
        token (Phase 1 #100 F10 feature). Idempotency-Key not used —
        the bridge picks a fresh token each time and we don't need
        retry-safe semantics for a one-shot operation."""
        resp = await self._client.post(
            f"{self._base_url}/v1/admin/invitations",
            headers=self._headers,
            json={
                "level": "service",
                "token": token,
                "expires_in_s": expires_in_s,
            },
        )
        # Status 201 on create. Anything else surfaces as an error.
        if resp.status_code != 201:
            self._raise_or_json(resp)

    async def record_tools_refreshed(
        self, server_id: str, *, tools_cache: dict[str, Any]
    ) -> None:
        """`POST /v1/admin/mcp-servers/{id}/tools-refreshed` —
        atomically writes tools_cache, stamps last_connected_at,
        clears refresh_requested_at."""
        resp = await self._client.post(
            f"{self._base_url}/v1/admin/mcp-servers/{server_id}/tools-refreshed",
            headers=self._headers,
            json={"tools_cache": tools_cache},
        )
        self._raise_or_json(resp)

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
