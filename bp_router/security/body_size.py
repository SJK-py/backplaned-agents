"""bp_router.security.body_size — ASGI body-size limit middleware.

Caps the total bytes the application will read from a single HTTP
request body. Without it, an attacker can POST a multi-GB body to any
JSON endpoint (`/v1/auth/login`, admin endpoints, etc.) to OOM the
worker or burn CPU on the JSON parser.

Two layers of defence:
  1. Cheap precheck on `Content-Length` (rejects 413 before reading
     any body bytes).
  2. Wrapped `receive` counts streamed bytes so the cap is enforced
     even when the client omits or lies about `Content-Length`
     (chunked encoding).

WebSocket scope bypasses entirely — uvicorn's `ws_max_size` handles
WS frame caps and the byte stream there isn't a single body. The
`/v1/files` HTTP route is also exempt because it implements its own
streaming size enforcement against the (typically much larger)
upload cap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# Paths that implement their own (larger) streaming size enforcement
# and should bypass the general per-request cap. Extend if you add
# other streaming-upload endpoints.
_EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("/v1/files",)


class BodySizeLimitMiddleware:
    """ASGI middleware capping HTTP request body bytes.

    Returns 413 Payload Too Large when the cap is exceeded, either at
    the Content-Length precheck or during streaming receive. Exempt
    paths under `/v1/files` defer to their own size enforcement.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _EXEMPT_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        # 1. Content-Length precheck.
        cl: int | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    cl = int(value.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    cl = None
                break
        if cl is not None and cl > self.max_bytes:
            await self._send_413(send)
            # Drain the body so the client sees the response cleanly.
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break
            return

        # 2. Wrapped receive — count streamed bytes against the cap.
        received_bytes = 0
        rejected = False

        async def guarded_receive() -> dict[str, Any]:
            nonlocal received_bytes, rejected
            msg = await receive()
            if msg.get("type") == "http.request":
                body = msg.get("body", b"")
                received_bytes += len(body)
                if received_bytes > self.max_bytes:
                    rejected = True
                    # Truncate body to flush; the wrapper below will
                    # short-circuit further reads on the next call.
                    return {"type": "http.disconnect"}
            return msg

        # 3. Defensive `send` wrapper that fires a 413 if the inner
        #    app proceeds despite the disconnect we injected. This
        #    catches an app that swallows http.disconnect and tries
        #    to emit its own response on the truncated body.
        response_started = False

        async def guarded_send(msg: dict[str, Any]) -> None:
            nonlocal response_started
            if rejected and not response_started:
                response_started = True
                await self._send_413(send)
                return
            if msg.get("type") == "http.response.start":
                response_started = True
            await send(msg)

        await self.app(scope, guarded_receive, guarded_send)

        # Post-call fallback: if the inner app exited without
        # responding (rare; can happen on http.disconnect handling),
        # we still need to surface a 413.
        if rejected and not response_started:
            await self._send_413(send)

    @staticmethod
    async def _send_413(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"request body too large"}',
                "more_body": False,
            }
        )
