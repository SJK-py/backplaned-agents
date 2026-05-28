"""bp_agents.agents.webapp.upstream — thin wrapper over the router JSON API.

The webapp talks to the router as the *logged-in user* (their own token),
not as a service principal ([webapp.md] §3): login/refresh/logout for the
session cookie, and the session lifecycle endpoints. One HTTP call per
method; token refresh is the caller's responsibility (see `auth`).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Non-2xx response from the router."""

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"upstream {status_code}: {detail!r}")


class UpstreamClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        resp = await self._http.request(
            method, path, headers=headers, json=json, params=params
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001
                detail = resp.text
            raise UpstreamError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- auth ----------------------------------------------------------

    async def login(self, *, email: str, password: str) -> dict[str, Any]:
        return await self.request(
            "POST", "/v1/auth/login", json={"email": email, "password": password}
        )

    async def refresh(self, *, refresh_token: str) -> dict[str, Any]:
        return await self.request(
            "POST", "/v1/auth/refresh", json={"refresh_token": refresh_token}
        )

    async def logout(
        self, *, access_token: str, refresh_token: str | None = None
    ) -> None:
        body: dict[str, Any] = {}
        if refresh_token:
            body["refresh_token"] = refresh_token
        await self.request(
            "POST", "/v1/auth/logout", access_token=access_token, json=body
        )

    # -- sessions (user token) ----------------------------------------

    async def list_sessions(self, *, access_token: str) -> list[dict[str, Any]]:
        return await self.request(
            "GET", "/v1/sessions", access_token=access_token
        )
