"""bp_agents.channel.store — the channel's window onto the router session store.

A channel writes the user's turn *before* any task exists, so it has no task
from which the router could derive scope. That is exactly the gap the store's
steward HTTP surface fills ([../../docs/design/router-managed-session-store.md]
§8): a caller holding the **user's own session JWT** may read any thread in
the session, drive session state, enqueue hand-overs, and take the turn lease
— but it cannot append to any thread, at any time, for any reason. There is no
message-POST endpoint. A channel that wants a user's words in an agent's
context enqueues them and the agent materialises them under its own
authorship.

Two channels hold that authority in different shapes, which is why this is a
seam and not a class:

  * the **chatbot** is a service principal and mints a per-user access token
    through its `serviced_by` rights (`chatbot/credentials.py`);
  * the **webapp** already has the logged-in user's own token in their cookie
    session.

`SessionStore` is what `ChannelCore` needs from either. `HttpSessionStore`
implements it against the router, taking an async `token_for(user_id)` so the
two differ only in that one function.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from bp_protocol.frames import SessionMessage, SessionOpResult

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

# Refusals the caller can resolve by retrying with fresher state, as opposed to
# "you may not do this at all". Mirrors the router's own split so a caller can
# branch on `err.retriable` instead of memorising codes.
_CONFLICT_CODES = frozenset({
    "thread_conflict",
    "version_conflict",
    "floor_regression",
    "session_busy",
    "lease_lost",
    "quota_exceeded",
    "content_too_large",
})


class StoreError(RuntimeError):
    """A refused store operation. `code` is the router's machine code
    (`thread_conflict`, `denied`, …); `retriable` says whether fresher state
    could make the same call succeed."""

    def __init__(self, code: str, *, status: int = 0, op_index: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.op_index = op_index

    @property
    def retriable(self) -> bool:
        return self.code in _CONFLICT_CODES


class SessionStore(Protocol):
    """What `ChannelCore` needs from the router's session store."""

    async def ops(
        self,
        *,
        user_id: str,
        session_id: str,
        ops: list[Any],
        scope: str = "session",
    ) -> list[SessionOpResult]: ...

    async def messages(
        self,
        *,
        user_id: str,
        session_id: str,
        owner: str,
        thread: str = "",
        roles: list[str] | None = None,
        include_retired: bool = False,
        include_hidden: bool = True,
        since_id: int | None = None,
        before_id: int | None = None,
        limit: int = 200,
    ) -> list[SessionMessage]: ...

    async def patch_metadata(
        self, *, user_id: str, session_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def acquire_lease(
        self, *, user_id: str, session_id: str, holder_id: str, ttl_ms: int = 300_000
    ) -> tuple[bool, float]: ...

    async def release_lease(
        self, *, user_id: str, session_id: str, holder_id: str
    ) -> None: ...


TokenFor = Callable[[str], Awaitable[str]]


class HttpSessionStore:
    """The steward endpoints, under a per-user token.

    Every method takes `user_id` rather than a token: the channel engine
    should not know how its host authenticates, and the two hosts differ
    (service `serviced_by` rights vs. the user's own cookie). `token_for` is
    the whole difference between them.
    """

    def __init__(
        self,
        *,
        http_url: str,
        token_for: TokenFor,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._url = http_url.rstrip("/")
        self._token_for = token_for
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token_for(user_id)}"}

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        """Turn a non-2xx into a `StoreError` carrying the router's code.

        The router answers a refused batch with `{"error": <code>,
        "op_index": n}`; anything else (a 404 for a session that isn't the
        caller's, a 5xx) has no code, so the HTTP status stands in."""
        code = f"http_{resp.status_code}"
        index: int | None = None
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001 — a non-JSON body is just opaque
            detail = None
        if isinstance(detail, dict):
            code = str(detail.get("error") or code)
            raw_index = detail.get("op_index")
            index = raw_index if isinstance(raw_index, int) else None
        elif isinstance(detail, str) and detail:
            code = detail
        raise StoreError(code, status=resp.status_code, op_index=index)

    async def ops(
        self,
        *,
        user_id: str,
        session_id: str,
        ops: list[Any],
        scope: str = "session",
    ) -> list[SessionOpResult]:
        """One transactional batch under steward authority.

        Thread-writing ops are not filtered out here — they refuse `denied`
        inside the store, which is the point: there is one authority check,
        in one place, and this client cannot drift from it."""
        body = {
            "ops": [op.model_dump(mode="json") for op in ops],
            "scope": scope,
        }
        resp = await self._client.post(
            f"{self._url}/v1/sessions/{session_id}/ops",
            headers=await self._headers(user_id),
            json=body,
        )
        if resp.status_code >= 400:
            self._raise(resp)
        return [SessionOpResult.model_validate(r) for r in resp.json()["results"]]

    async def messages(
        self,
        *,
        user_id: str,
        session_id: str,
        owner: str,
        thread: str = "",
        roles: list[str] | None = None,
        include_retired: bool = False,
        include_hidden: bool = True,
        since_id: int | None = None,
        before_id: int | None = None,
        limit: int = 200,
    ) -> list[SessionMessage]:
        """The transcript path — cursor-paginated and NOT capped by the
        WebSocket frame budget, unlike a `Read` op. For rendering a whole
        conversation, not for building an agent's context."""
        params: dict[str, Any] = {
            "owner_agent_id": owner,
            "thread_key": thread,
            "include_retired": str(include_retired).lower(),
            "include_hidden": str(include_hidden).lower(),
            "limit": limit,
        }
        if roles:
            params["roles"] = ",".join(roles)
        if since_id is not None:
            params["since_id"] = since_id
        if before_id is not None:
            params["before_id"] = before_id
        resp = await self._client.get(
            f"{self._url}/v1/sessions/{session_id}/messages",
            headers=await self._headers(user_id),
            params=params,
        )
        if resp.status_code >= 400:
            self._raise(resp)
        return [SessionMessage.model_validate(m) for m in resp.json()]

    async def patch_metadata(
        self, *, user_id: str, session_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        """Shallow-merge the session's `metadata` (a null value deletes a
        key). Conversation descriptors — title, channel, chat id — live here
        rather than in a suite table shadowing the router's session row."""
        resp = await self._client.patch(
            f"{self._url}/v1/sessions/{session_id}",
            headers=await self._headers(user_id),
            json={"patch": patch},
        )
        if resp.status_code >= 400:
            self._raise(resp)
        return resp.json().get("metadata", {})

    async def acquire_lease(
        self, *, user_id: str, session_id: str, holder_id: str, ttl_ms: int = 300_000
    ) -> tuple[bool, float]:
        """Take the session's FIFO turn lease. Returns `(granted, retry_after_s)`.

        A steward has no socket the router can push a promotion to, so it
        polls: the 409 carries `Retry-After` derived from the current holder's
        TTL. Ordering is still the router's — the ticket, not the retry
        timing, decides who goes next."""
        resp = await self._client.post(
            f"{self._url}/v1/sessions/{session_id}/lease",
            headers=await self._headers(user_id),
            json={"holder_id": holder_id, "ttl_ms": ttl_ms},
        )
        if resp.status_code == 409:
            retry = resp.headers.get("Retry-After")
            try:
                return False, max(0.5, float(retry)) if retry else 1.0
            except ValueError:
                return False, 1.0
        if resp.status_code >= 400:
            self._raise(resp)
        return True, 0.0

    async def release_lease(
        self, *, user_id: str, session_id: str, holder_id: str
    ) -> None:
        resp = await self._client.delete(
            f"{self._url}/v1/sessions/{session_id}/lease",
            headers=await self._headers(user_id),
            params={"holder_id": holder_id},
        )
        # A lease we no longer hold is already released as far as the caller
        # is concerned; only a real refusal is worth raising.
        if resp.status_code >= 400 and resp.status_code != 404:
            self._raise(resp)


class TokenRegistry:
    """`token_for` for a host that learns the user's token per request.

    The webapp holds the logged-in user's access token in their cookie
    session, so it can only supply one while serving that user. Requests
    refresh the entry; detached follow-ups (session titling, `memory.add`)
    then find it still there. A user with no entry is a bug in the caller,
    not a case to paper over — hence the explicit error rather than a silent
    unauthenticated call."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, datetime | None]] = {}

    def remember(
        self, user_id: str, access_token: str, *, expires_at: datetime | None = None
    ) -> None:
        self._tokens[user_id] = (access_token, expires_at)

    def forget(self, user_id: str) -> None:
        self._tokens.pop(user_id, None)

    async def __call__(self, user_id: str) -> str:
        entry = self._tokens.get(user_id)
        if entry is None:
            raise StoreError("no_user_token")
        return entry[0]
