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

    async def create_session(
        self, *, access_token: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "POST", "/v1/sessions", access_token=access_token,
            json={"metadata": metadata or {}},
        )

    async def delete_session(
        self, *, access_token: str, session_id: str, purge: bool = False
    ) -> None:
        """Close (archive) the session, or hard-delete it with `purge=True`
        (the router's `DELETE …?purge=true`). 404 is swallowed — an
        already-gone session is a no-op for the caller's intent."""
        try:
            await self.request(
                "DELETE", f"/v1/sessions/{session_id}",
                access_token=access_token,
                params={"purge": "true"} if purge else None,
            )
        except UpstreamError as exc:
            if exc.status_code != 404:
                raise

    # -- files (user token) — stash listing + upload ------------------

    async def list_names(
        self, *, access_token: str, session_id: str | None = None,
        persistent: bool = False,
    ) -> list[str]:
        params: dict[str, Any] = {"persistent": str(persistent).lower()}
        if session_id is not None:
            params["session_id"] = session_id
        body = await self.request(
            "GET", "/v1/files/names", access_token=access_token, params=params
        )
        return body.get("names", []) if body else []

    async def upload_file(
        self, *, access_token: str, filename: str, data: bytes,
        session_id: str | None = None, persistent: bool = False,
        mime_type: str | None = None,
    ) -> str:
        """Upload a blob then bind it to a stash NAME (session or `persist/`),
        the user's own token. Returns the actual saved name (post-dedup)."""
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {} if persistent else {"session_id": session_id or ""}
        up = await self._http.post(
            "/v1/files", headers=headers, params=params,
            files={"file": (filename, data, mime_type or "application/octet-stream")},
        )
        if up.status_code >= 400:
            raise UpstreamError(up.status_code, up.text)
        sha256 = up.json()["sha256"]
        name = f"persist/{filename}" if persistent else filename
        bind_body: dict[str, Any] = {"name": name, "sha256": sha256}
        if not persistent:
            bind_body["session_id"] = session_id
        bind = await self._http.post(
            "/v1/files/names", headers=headers, json=bind_body
        )
        if bind.status_code >= 400:
            raise UpstreamError(bind.status_code, bind.text)
        return bind.json()["saved_name"]

    # -- files (user token) — resolve a produced NAME → bytes ----------

    async def resolve_named_file(
        self, *, access_token: str, session_id: str, name: str
    ) -> str | None:
        resp = await self._http.get(
            "/v1/files/names/resolve",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"name": name, "session_id": session_id},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text)
        return resp.json()["file_id"]

    async def fetch_file(self, *, access_token: str, file_id: str) -> bytes:
        resp = await self._http.get(
            f"/v1/files/{file_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text)
        return resp.content
