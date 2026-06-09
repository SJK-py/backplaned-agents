"""bp_admin.upstream — thin wrapper over the router's JSON API.

The BFF never touches router internals; everything goes through this
client. Each method is a single endpoint call with simple status-code
handling. Token refresh is the caller's responsibility (see
`bp_admin.auth`); this module just makes one HTTP call per invocation.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote as _urlquote

import httpx

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Non-2xx response from the router. `detail` is whatever the router
    returned in `detail` (usually a string, sometimes a dict for
    `AdmitError`-shaped responses)."""

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

    # ------------------------------------------------------------------
    # Auth — the BFF needs these for login / refresh / logout flows.
    # Other endpoints are called ad-hoc by page handlers.
    # ------------------------------------------------------------------

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

    async def change_password(
        self, *, access_token: str, current_password: str, new_password: str
    ) -> None:
        """Change the logged-in admin's own password (`POST
        /v1/auth/change-password`): confirms `current_password`, then sets
        `new_password`. The router revokes the caller's tokens on success, so
        the BFF session must be dropped + re-login afterwards. Returns 204."""
        await self.request(
            "POST", "/v1/auth/change-password", access_token=access_token,
            json={
                "current_password": current_password,
                "new_password": new_password,
            },
        )

    # ------------------------------------------------------------------
    # Admin endpoints — thin wrapper that prepends `/v1/admin` and
    # attaches the bearer token. Page handlers call this directly with
    # path strings; we don't add per-endpoint methods because the
    # endpoint surface is large and stable.
    # ------------------------------------------------------------------

    async def admin_request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self.request(
            method,
            f"/v1/admin{_safe_path(path)}",
            access_token=access_token,
            json=json,
            params=params,
        )


# Page handlers build admin paths with f-strings like
# `/users/{user_id}` and `/agents/{agent_id}/suspend`. Router-side regex
# grammars constrain those IDs to URL-safe characters today, but a
# regression there shouldn't translate into upstream path injection or
# `..` traversal here. URL-encode each path segment as defense in depth.
#
# We treat the URL up to the first `?` as the path; query strings come
# in via `params=` and httpx encodes those itself.
_PATH_SAFE = "/-_.~"


def _safe_path(path: str) -> str:
    """URL-encode each segment of the path, preserving slashes.

    `quote(s, safe='/-_.~')` lets `/` through (segment separators) but
    encodes everything that could change the path semantics — `..`,
    `?`, `#`, control chars, non-ASCII. Already-encoded sequences (e.g.
    `%2F`) are left alone via `safe='%'`.
    """
    if not path:
        return path
    # Split off any embedded query string (rare; admin paths are bare).
    head, sep, tail = path.partition("?")
    encoded = _urlquote(head, safe=_PATH_SAFE + "%")
    return encoded + sep + tail
