"""chatbot.credentials — the channel's HTTP control-plane client.

The channel acts as a `service` principal (provisioned at onboarding) and
uses its `serviced_by` rights for per-user HTTP ops. `ChannelCredentials`
is a Protocol so the gateway + poller are testable with a fake;
`HttpChannelCredentials` is the real client (token caching + rotation).

Mechanisms ([channel.md] §3): registration submit + serviced-session
discovery ride the SERVICE access token; session open / task cancel ride
a minted PER-USER access token.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from bp_sdk.onboarding import persist_service_token
from bp_sdk.settings import AgentConfig

logger = logging.getLogger(__name__)

# Refresh an access token this many seconds before its stated expiry.
_TOKEN_SKEW_S = 60.0


@dataclass
class ServicedSession:
    user_id: str
    session_id: str
    external_id: str | None
    channel: str | None
    opened_at: datetime


class ChannelCredentials(Protocol):
    async def submit_registration(
        self,
        *,
        channel: str,
        external_id: str,
        requested_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str: ...

    async def list_serviced_sessions(
        self, *, channel: str | None = None, since: datetime | None = None
    ) -> list[ServicedSession]: ...

    async def open_session(
        self, *, user_id: str, metadata: dict[str, Any] | None = None
    ) -> str: ...

    async def close_session(self, *, user_id: str, session_id: str) -> None: ...

    async def cancel_task(self, *, user_id: str, task_id: str) -> None: ...

    async def mint_password_reset_token(self, *, user_id: str) -> str: ...

    async def store_named_file(
        self, *, user_id: str, session_id: str, filename: str, data: bytes,
        mime_type: str | None = None,
    ) -> str: ...

    async def resolve_named_file(
        self, *, user_id: str, session_id: str, name: str
    ) -> str | None: ...

    async def fetch_file(self, *, user_id: str, file_id: str) -> bytes: ...


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HttpChannelCredentials:
    """Real channel HTTP client. Caches the service access token and a
    per-user access token, refreshing each before expiry. Rotated service
    refresh tokens are persisted back to credentials.json."""

    def __init__(self, *, http_url: str, config: AgentConfig) -> None:
        self._http_url = http_url.rstrip("/")
        self._config = config
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._service_access: tuple[str, datetime] | None = None
        # user_id → (refresh_token, access_token, access_expiry)
        self._user_tokens: dict[str, tuple[str, str, datetime]] = {}
        # Serialize token refresh. The router ROTATES a refresh token on use
        # (single-use), so two concurrent refreshes with the same token race —
        # one wins, the other 401s. The chatbot runs concurrent callers (the
        # Telegram + KakaoTalk approval loops, per-user ops), so a refresh
        # must be exclusive; a second caller then reuses the freshly-cached
        # token instead of re-refreshing.
        self._service_lock = asyncio.Lock()
        self._user_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Token plumbing
    # ------------------------------------------------------------------

    async def _refresh(self, refresh_token: str) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._http_url}/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        resp.raise_for_status()
        return resp.json()

    def _service_cached(self) -> str | None:
        if self._service_access is not None:
            token, exp = self._service_access
            if datetime.now(UTC) < exp - timedelta(seconds=_TOKEN_SKEW_S):
                return token
        return None

    async def _service_token(self) -> str:
        cached = self._service_cached()
        if cached is not None:
            return cached
        async with self._service_lock:
            # Double-check: a concurrent caller may have refreshed while we
            # waited on the lock — reuse its token rather than rotate again
            # (which would 401 the loser of the race).
            cached = self._service_cached()
            if cached is not None:
                return cached
            if not self._config.service_refresh_token:
                raise RuntimeError(
                    "channel has no service refresh token (not provisioned at "
                    "onboarding)"
                )
            pair = await self._refresh(self._config.service_refresh_token)
            # Refresh rotates the token — persist the new one so the next
            # process start / refresh uses it.
            persist_service_token(
                self._config,
                refresh_token=pair["refresh_token"],
                expires_at=pair.get("expires_at"),
            )
            exp = _parse_dt(pair["expires_at"])
            self._service_access = (pair["access_token"], exp)
            return pair["access_token"]

    def _user_cached(self, user_id: str) -> str | None:
        cached = self._user_tokens.get(user_id)
        if cached is not None:
            _refresh_tok, access, exp = cached
            if datetime.now(UTC) < exp - timedelta(seconds=_TOKEN_SKEW_S):
                return access
        return None

    async def _user_token(self, user_id: str) -> str:
        cached = self._user_cached(user_id)
        if cached is not None:
            return cached
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = self._user_locks[user_id] = asyncio.Lock()
        async with lock:
            # Double-check after acquiring the per-user lock (same rotation
            # race as the service token, scoped per user).
            cached = self._user_cached(user_id)
            if cached is not None:
                return cached
            stale = self._user_tokens.get(user_id)
            if stale is not None:
                pair = await self._refresh(stale[0])
            else:
                # Mint a fresh per-user refresh token via serviced_by rights.
                minted = await self._mint_user_refresh(user_id)
                pair = await self._refresh(minted)
            exp = _parse_dt(pair["expires_at"])
            self._user_tokens[user_id] = (
                pair["refresh_token"], pair["access_token"], exp
            )
            return pair["access_token"]

    async def _mint_user_refresh(self, user_id: str) -> str:
        token = await self._service_token()
        resp = await self._client.post(
            f"{self._http_url}/v1/admin/users/{user_id}/refresh-tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()["refresh_token"]

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def submit_registration(
        self,
        *,
        channel: str,
        external_id: str,
        requested_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        token = await self._service_token()
        body: dict[str, Any] = {"channel": channel, "external_id": external_id}
        if requested_email:
            body["requested_email"] = requested_email
        if metadata:
            body["metadata"] = metadata
        resp = await self._client.post(
            f"{self._http_url}/v1/registrations",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["registration_id"]

    async def list_serviced_sessions(
        self, *, channel: str | None = None, since: datetime | None = None
    ) -> list[ServicedSession]:
        token = await self._service_token()
        params: dict[str, Any] = {}
        if channel is not None:
            params["channel"] = channel
        if since is not None:
            params["since"] = since.isoformat()
        resp = await self._client.get(
            f"{self._http_url}/v1/admin/serviced-sessions",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        resp.raise_for_status()
        return [
            ServicedSession(
                user_id=r["user_id"],
                session_id=r["session_id"],
                external_id=r.get("external_id"),
                channel=r.get("channel"),
                opened_at=_parse_dt(r["opened_at"]),
            )
            for r in resp.json()
        ]

    async def open_session(
        self, *, user_id: str, metadata: dict[str, Any] | None = None
    ) -> str:
        token = await self._user_token(user_id)
        resp = await self._client.post(
            f"{self._http_url}/v1/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"metadata": metadata or {}},
        )
        resp.raise_for_status()
        return resp.json()["session_id"]

    async def close_session(self, *, user_id: str, session_id: str) -> None:
        """Archive a session (router `DELETE /v1/sessions/{id}`, no purge) on
        the user's behalf — used by `/new` to retire the previous session. A
        404 (already gone / not the user's) is swallowed as a no-op."""
        token = await self._user_token(user_id)
        resp = await self._client.delete(
            f"{self._http_url}/v1/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()

    async def cancel_task(self, *, user_id: str, task_id: str) -> None:
        token = await self._user_token(user_id)
        resp = await self._client.post(
            f"{self._http_url}/v1/tasks/{task_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()

    async def mint_password_reset_token(self, *, user_id: str) -> str:
        """Mint a single-use password-setup token for a serviced user (the
        `/password` command, [channel.md §6]). Uses `serviced_by` rights via
        the service principal; the router gates it (F9)."""
        token = await self._service_token()
        resp = await self._client.post(
            f"{self._http_url}/v1/admin/users/{user_id}/password-reset-tokens",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()["reset_token"]

    # ------------------------------------------------------------------
    # Named file store (session-authed; per-user token)
    # ------------------------------------------------------------------

    async def store_named_file(
        self, *, user_id: str, session_id: str, filename: str, data: bytes,
        mime_type: str | None = None,
    ) -> str:
        """Upload a blob to the session scope and bind it to `filename`.
        Returns the ACTUAL saved name (post-dedup)."""
        token = await self._user_token(user_id)
        headers = {"Authorization": f"Bearer {token}"}
        up = await self._client.post(
            f"{self._http_url}/v1/files",
            headers=headers,
            params={"session_id": session_id},
            files={"file": (filename, data, mime_type or "application/octet-stream")},
        )
        up.raise_for_status()
        sha256 = up.json()["sha256"]
        bind = await self._client.post(
            f"{self._http_url}/v1/files/names",
            headers=headers,
            json={"name": filename, "sha256": sha256, "session_id": session_id},
        )
        bind.raise_for_status()
        return bind.json()["saved_name"]

    async def resolve_named_file(
        self, *, user_id: str, session_id: str, name: str
    ) -> str | None:
        token = await self._user_token(user_id)
        resp = await self._client.get(
            f"{self._http_url}/v1/files/names/resolve",
            headers={"Authorization": f"Bearer {token}"},
            params={"name": name, "session_id": session_id},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["file_id"]

    async def fetch_file(self, *, user_id: str, file_id: str) -> bytes:
        token = await self._user_token(user_id)
        resp = await self._client.get(
            f"{self._http_url}/v1/files/{file_id}",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

