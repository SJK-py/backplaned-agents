"""MCP transport clients.

Two implementations sharing the same surface:

  * `StreamableHttpMcpClient` (Phase 10b) — POST JSON-RPC requests,
    receive synchronous JSON responses. Simple; works for newer
    MCP servers built against the Streamable HTTP spec.
  * `SseMcpClient` (Phase 10d) — long-lived SSE GET for inbound
    messages, separate POST channel for outbound requests. Used by
    older MCP servers and the canonical reference implementations.

The factory `build_mcp_client(...)` picks based on the row's
`transport` value. Both clients expose `initialize() / list_tools()
/ call_tool() / aclose()` so `ServerBridge` doesn't care which one
it got.

The full `mcp` Python SDK from Anthropic would handle all of the
above and more, but isn't installed in the test env. Hand-rolling
the minimum we need keeps the bridge end-to-end testable in this
codebase without a transitive-dep cascade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from bp_mcp_bridge import metrics

logger = logging.getLogger(__name__)


# MCP protocol version we negotiate against the server. Bumped
# alongside upstream spec changes; servers that don't recognise it
# typically downgrade gracefully.
_MCP_PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """An MCP RPC returned a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclass
class ToolDefinition:
    """One entry from `tools/list`. Mirrors the MCP spec's tool
    schema verbatim — `input_schema` is the upstream
    `inputSchema` field renamed for Python-style consistency."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolResult:
    """One response from `tools/call`. MCP returns a content
    array (each entry may be text / image / resource), plus an
    `isError` flag. The bridge flattens this into AgentOutput
    via `as_agent_output_dict()`."""

    content: list[dict[str, Any]]
    is_error: bool = False

    def as_agent_output_dict(self) -> dict[str, Any]:
        """Convert the MCP content array into a single-string
        `AgentOutput.content`. Concatenates `text` entries; non-
        text entries (images, resources) get a placeholder
        until Phase 10c wires the multimodal envelope (PR #108's
        `document_part` / `image_part` shapes)."""
        chunks: list[str] = []
        for item in self.content:
            t = item.get("type")
            if t == "text":
                chunks.append(item.get("text") or "")
            elif t == "image":
                chunks.append(f"[image:{item.get('mimeType', 'unknown')}]")
            elif t == "resource":
                chunks.append(f"[resource:{item.get('resource', {}).get('uri', '')}]")
            else:
                chunks.append(f"[{t}]")
        return {
            "content": "\n".join(chunks),
            "metadata": {
                "mcp_content": self.content,
                "mcp_is_error": self.is_error,
            },
        }


class StreamableHttpMcpClient:
    """JSON-RPC 2.0 over HTTP. One client = one MCP server.

    `initialize` MUST be called before any other RPC; otherwise
    upstream returns a `not_initialized` error. The bridge calls
    it once on connect; subsequent reconnects re-initialize.

    `aclose()` must be called on shutdown — owns an underlying
    `httpx.AsyncClient` connection pool."""

    def __init__(
        self,
        url: str,
        *,
        auth_kind: str = "none",
        auth_value: str | None = None,
        auth_header_name: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._url = url
        self._headers = self._build_headers(auth_kind, auth_value, auth_header_name)
        # Accept both JSON and SSE content types; the MCP spec
        # allows the server to respond with either even on
        # synchronous calls. Phase 10b consumes only JSON; SSE
        # responses raise a clear error.
        self._headers["Accept"] = "application/json, text/event-stream"
        self._headers["Content-Type"] = "application/json"
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._request_id = 0
        self._initialized = False

    @staticmethod
    def _build_headers(
        kind: str, value: str | None, header_name: str | None
    ) -> dict[str, str]:
        if kind == "none":
            return {}
        if not value:
            raise ValueError(f"auth_value required for auth_kind={kind!r}")
        if kind == "bearer":
            return {"Authorization": f"Bearer {value}"}
        if kind == "header":
            if not header_name:
                raise ValueError("auth_header_name required for auth_kind='header'")
            return {header_name: value}
        raise ValueError(f"unknown auth_kind {kind!r}")

    async def initialize(self) -> dict[str, Any]:
        """First call — negotiates protocol version + capabilities.
        Returns the server's response (capabilities + serverInfo)."""
        result = await self._call(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "bp_mcp_bridge",
                    "version": "0.1.0",
                },
            },
        )
        # Spec: client MUST send a `notifications/initialized`
        # notification after the initialize response is received.
        await self._notify("notifications/initialized", {})
        self._initialized = True
        return result

    async def list_tools(self) -> list[ToolDefinition]:
        """Fetch the server's tool list. The bridge calls this on
        startup AND on a `notifications/tools/list_changed`
        signal (Phase 10c)."""
        if not self._initialized:
            raise RuntimeError("initialize() must be called before list_tools()")
        result = await self._call("tools/list", {})
        tools_raw = result.get("tools") or []
        return [
            ToolDefinition(
                name=t["name"],
                description=t.get("description") or "",
                input_schema=t.get("inputSchema") or {"type": "object"},
            )
            for t in tools_raw
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Invoke `name` with `arguments`. The bridge handler
        forwards `NewTaskFrame.payload` here verbatim — the
        router admit-validated against the tool's input_schema
        already so trust the shape."""
        if not self._initialized:
            raise RuntimeError("initialize() must be called before call_tool()")
        result = await self._call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return ToolResult(
            content=result.get("content") or [],
            is_error=bool(result.get("isError")),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        resp = await self._client.post(self._url, json=body, headers=self._headers)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # Phase 10c will parse the SSE stream. For 10b we treat
            # SSE responses as an error so misconfiguration surfaces
            # clearly rather than hanging.
            raise McpError(
                code=-32603,
                message=(
                    "MCP server responded with SSE stream; "
                    "Phase 10b only supports synchronous JSON responses. "
                    "Set BP_MCP_BRIDGE_TRANSPORT=sse for SSE support (Phase 10c)."
                ),
            )
        try:
            data = resp.json()
        except ValueError as exc:
            # 2xx with a non-JSON body — an empty 200, an HTML error
            # page injected by a reverse proxy, a text/plain blurb.
            # `raise_for_status` already passed so this isn't an HTTP
            # error; without this guard `json()` raises a bare
            # `JSONDecodeError` (a `ValueError`, NOT an httpx type and
            # NOT an McpError), so `tool_agent._is_transient` returns
            # False and the call fails permanently with a cryptic
            # "Expecting value: line 1 column 1". A flaky proxy
            # returning an HTML 200 is exactly a transient condition
            # worth the retry — surface it as -32603 (in
            # `_MCP_TRANSIENT_CODES`).
            raise McpError(
                -32603,
                f"non-JSON response from MCP server "
                f"(content-type={ct!r})",
            ) from exc
        if "error" in data:
            err = data["error"]
            raise McpError(
                code=err.get("code", -32603),
                message=err.get("message", "unknown error"),
                data=err.get("data"),
            )
        return data.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        # JSON-RPC notifications have no `id` and no response.
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        resp = await self._client.post(self._url, json=body, headers=self._headers)
        resp.raise_for_status()


# ===========================================================================
# SSE transport (Phase 10d)
# ===========================================================================
#
# MCP's SSE transport is bidirectional via two HTTP channels:
#
#   1. Long-lived GET to the SSE endpoint (e.g. https://server/sse).
#      Server streams `text/event-stream` events. First event has
#      `event: endpoint\ndata: <postUrl>\n\n` — the URL for client→server
#      requests. Subsequent events have `event: message` carrying
#      JSON-RPC responses + notifications.
#
#   2. POST to the discovered `postUrl` for client→server requests.
#      Server returns 202 Accepted; the actual response comes via the
#      SSE stream, correlated by JSON-RPC `id`.
#
# Implementation notes:
#   - A background asyncio task reads the SSE stream and resolves
#     per-request futures keyed by JSON-RPC id.
#   - `connect()` waits up to 10s for the endpoint event before
#     returning so callers don't race the first POST.
#   - Notifications (`event: message` with no `id`) are currently
#     dropped — tool-list-change handling is a Phase 10e concern.


import asyncio
import json as _json
import logging as _logging

_logger = _logging.getLogger(__name__)


def _default_port(scheme: str) -> int | None:
    """Default port per scheme — used by the SSE endpoint-event
    origin check. Two URLs with the same scheme and hostname must
    still match on `(port or default_port(scheme))` so that
    `https://example.com/sse` and `https://example.com:443/sse`
    aren't treated as different origins."""
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


class SseMcpClient:
    """MCP client over Server-Sent Events transport.

    Same external API as `StreamableHttpMcpClient`. Internally
    runs a background task to consume the SSE stream and
    resolves per-request futures by `id`."""

    _ENDPOINT_WAIT_TIMEOUT_S = 10.0
    _RESPONSE_TIMEOUT_S = 60.0
    # Exponential reconnect backoff; `retry: <ms>` from the server
    # overrides per WHATWG SSE spec.
    _RECONNECT_BACKOFF_INITIAL_S = 1.0
    _RECONNECT_BACKOFF_MAX_S = 60.0

    def __init__(
        self,
        url: str,
        *,
        auth_kind: str = "none",
        auth_value: str | None = None,
        auth_header_name: str | None = None,
        on_tools_changed: callable[[], None] | None = None,
        server_id: str = "unknown",
    ) -> None:
        """`on_tools_changed` (Phase 10f) is invoked when the server
        pushes a `notifications/tools/list_changed` notification on
        the SSE stream. Callable is sync (no async) — typically
        signals an asyncio.Event the bridge's refresh loop reads.

        `server_id` is observability-only — the bounded Prometheus
        label for SSE reconnect metrics. Defaults to `"unknown"`
        so direct instantiation in tests / ad-hoc use doesn't have
        to supply it."""
        self._sse_url = url
        self._server_id = server_id
        self._headers = StreamableHttpMcpClient._build_headers(
            auth_kind, auth_value, auth_header_name
        )
        # SSE GET expects this Accept header; the POST channel
        # uses application/json for the request body.
        self._sse_headers = {**self._headers, "Accept": "text/event-stream"}
        self._post_headers = {**self._headers, "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(timeout=None)
        self._post_url: str | None = None
        self._endpoint_event = asyncio.Event()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stream_task: asyncio.Task | None = None
        self._request_id = 0
        self._initialized = False
        self._closed = False
        self._on_tools_changed = on_tools_changed

    async def initialize(self) -> dict[str, Any]:
        """Start the SSE stream, wait for the endpoint event,
        then send the MCP `initialize` handshake."""
        if self._stream_task is None:
            self._stream_task = asyncio.create_task(
                self._stream_loop(), name="mcp_sse_stream"
            )
        try:
            await asyncio.wait_for(
                self._endpoint_event.wait(),
                timeout=self._ENDPOINT_WAIT_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise McpError(
                code=-32603,
                message=(
                    f"SSE server at {self._sse_url} did not emit an "
                    f"endpoint event within {self._ENDPOINT_WAIT_TIMEOUT_S}s — "
                    "is this a Streamable HTTP server? Check transport setting."
                ),
            ) from exc
        result = await self._call(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "bp_mcp_bridge",
                    "version": "0.1.0",
                },
            },
        )
        await self._notify("notifications/initialized", {})
        self._initialized = True
        return result

    async def list_tools(self) -> list[ToolDefinition]:
        if not self._initialized:
            raise RuntimeError("initialize() must be called before list_tools()")
        result = await self._call("tools/list", {})
        tools_raw = result.get("tools") or []
        return [
            ToolDefinition(
                name=t["name"],
                description=t.get("description") or "",
                input_schema=t.get("inputSchema") or {"type": "object"},
            )
            for t in tools_raw
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._initialized:
            raise RuntimeError("initialize() must be called before call_tool()")
        result = await self._call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return ToolResult(
            content=result.get("content") or [],
            is_error=bool(result.get("isError")),
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                # Stream task raised something other than the
                # cancel we just sent. Tearing down anyway, but
                # log so a real bug isn't silently dropped.
                _logger.exception(
                    "mcp_sse_stream_task_aclose_error",
                    extra={"event": "mcp_sse_stream_task_aclose_error"},
                )
        # Reject any in-flight requests so callers wake up.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    McpError(-32603, "MCP client closed before response arrived")
                )
        self._pending.clear()
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Background SSE stream
    # ------------------------------------------------------------------

    async def _stream_loop(self) -> None:
        """Consume the SSE stream forever, dispatching events.

        Loops with exponential backoff on connection / stream
        errors so a transient network blip doesn't permanently
        break the bridge (the prior shape ran exactly once and
        died on the first exception). Sends `Last-Event-ID` on
        reconnect for at-least-once delivery semantics on servers
        that support it. Cancelled only by `aclose()`.

        In-flight `_pending` futures are NOT failed when the
        stream drops — the corresponding POST may have already
        been accepted by the server, and the response may still
        arrive on the reconnected stream. Pending futures fall
        back to their own `_RESPONSE_TIMEOUT_S` if no response
        ever shows up.
        """
        last_event_id: str | None = None
        backoff = self._RECONNECT_BACKOFF_INITIAL_S
        while not self._closed:
            try:
                headers = dict(self._sse_headers)
                if last_event_id is not None:
                    headers["Last-Event-ID"] = last_event_id
                async with self._client.stream(
                    "GET", self._sse_url, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    # Successful connect — reset backoff so the next
                    # failure starts from the floor again.
                    backoff = self._RECONNECT_BACKOFF_INITIAL_S
                    event_type: str | None = None
                    data_lines: list[str] = []
                    async for raw_line in resp.aiter_lines():
                        line = raw_line.rstrip("\r")
                        if not line:
                            # Event boundary: flush.
                            # SSE spec (WHATWG HTML §9.2.6): default
                            # event type is "message" when no
                            # `event:` line was seen for this event.
                            # Previous shape required event_type to
                            # be non-empty and silently dropped
                            # bare `data:` events — which is the
                            # MOST COMMON event shape for spec-
                            # compliant servers.
                            if data_lines:
                                await self._handle_event(
                                    event_type or "message",
                                    "\n".join(data_lines),
                                )
                            event_type = None
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            # SSE comment / keep-alive — ignore.
                            continue
                        if line.startswith("event:"):
                            event_type = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].lstrip())
                        elif line.startswith("id:"):
                            # WHATWG SSE: tracked for the
                            # Last-Event-ID header sent on reconnect.
                            last_event_id = line[len("id:"):].strip()
                        elif line.startswith("retry:"):
                            # Server-suggested reconnect time (ms).
                            try:
                                ms = int(line[len("retry:"):].strip())
                                backoff = max(0.1, min(
                                    ms / 1000.0,
                                    self._RECONNECT_BACKOFF_MAX_S,
                                ))
                            except ValueError:
                                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "mcp_sse_stream_failed",
                    extra={
                        "event": "mcp_sse_stream_failed",
                        "url": self._sse_url,
                        "error": repr(exc),
                        "reconnect_in_s": backoff,
                    },
                )
            # Stop reconnecting if the client is shutting down.
            if self._closed:
                return
            # The stream connection just died (exception) OR the
            # server closed it cleanly (aiter_lines returned without
            # raising — the `while not self._closed` loop fell
            # through to here). Either way, any in-flight request
            # whose response was expected on THIS stream connection
            # is now stranded:
            #
            #   - SSE-transport MCP servers tie the JSON-RPC response
            #     to the request's SSE *session*. The reconnected
            #     stream is a NEW session (new `endpoint` event, new
            #     session id). Servers almost never replay a response
            #     generated for a dead session onto the new one —
            #     `Last-Event-ID` resumes the *event log*, not
            #     per-request response routing.
            #   - The original optimistic comment ("the response may
            #     still arrive on the reconnected stream") holds only
            #     for the rare server that buffers + replays by event
            #     id. For everyone else the future hangs the full
            #     `_RESPONSE_TIMEOUT_S` (60s) before failing — a
            #     60-second latency cliff on every transient blip.
            #
            # Fail the pending futures with a RETRYABLE McpError
            # (-32603 is in `tool_agent._MCP_TRANSIENT_CODES`), so
            # `_call_tool_with_retry` re-issues the call on the
            # freshly reconnected stream in sub-second time instead
            # of stalling. A late duplicate response for an
            # already-failed id is dropped harmlessly by
            # `_handle_event` (`fut is None or fut.done()`).
            stranded_before = len(self._pending)
            self._fail_pending_for_reconnect()
            # Reaching here means the stream ended (drop or clean
            # close) and we're about to re-GET — one reconnect cycle.
            metrics.sse_reconnects_total.labels(
                server_id=self._server_id,
            ).inc()
            if stranded_before:
                metrics.sse_pending_stranded_total.labels(
                    server_id=self._server_id,
                ).inc(stranded_before)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, self._RECONNECT_BACKOFF_MAX_S)

    def _fail_pending_for_reconnect(self) -> None:
        """Fail every in-flight request future with a retryable
        error because the SSE stream they were correlated to is
        gone. Called on stream drop / clean-close before the
        reconnect sleep. No-op when there are no pending requests
        (the steady-state idle-stream-reconnect case)."""
        if not self._pending:
            return
        stranded = list(self._pending.items())
        _logger.warning(
            "mcp_sse_pending_stranded_on_reconnect",
            extra={
                "event": "mcp_sse_pending_stranded_on_reconnect",
                "url": self._sse_url,
                "stranded_count": len(stranded),
            },
        )
        for rid, fut in stranded:
            if not fut.done():
                fut.set_exception(
                    McpError(
                        -32603,
                        "MCP SSE stream reconnected before the response "
                        "for this request arrived; retry on the new stream",
                    )
                )
            # `_call`'s `finally` pops `rid`; clearing here too keeps
            # the dict tight if `_call` is somehow not awaiting (it
            # always is, but defensive — a stale entry would shadow
            # a re-used request id after `_request_id` wraps, which
            # it never does in practice but the cost of clearing is
            # zero).
            self._pending.pop(rid, None)

    async def _handle_event(self, event_type: str, data: str) -> None:
        """Dispatch one SSE event. `endpoint` sets the POST URL;
        `message` carries JSON-RPC responses or notifications."""
        if event_type == "endpoint":
            # Spec is ambiguous on absolute vs relative; urljoin
            # handles both. Attacker-controlled payload could return
            # an absolute URL pointing at a different origin —
            # the bridge would then POST subsequent requests +
            # bearer credential to that host. Require resolved
            # origin (scheme + host + port) match the SSE URL's;
            # mismatch refuses to set `_post_url` and the next RPC
            # fails with "endpoint URL not yet received".
            from urllib.parse import urljoin, urlparse  # noqa: PLC0415
            candidate = urljoin(self._sse_url, data.strip())
            base = urlparse(self._sse_url)
            cand = urlparse(candidate)
            same_origin = (
                cand.scheme == base.scheme
                and cand.hostname == base.hostname
                and (cand.port or _default_port(cand.scheme))
                    == (base.port or _default_port(base.scheme))
            )
            if not same_origin:
                _logger.warning(
                    "mcp_sse_endpoint_origin_mismatch",
                    extra={
                        "event": "mcp_sse_endpoint_origin_mismatch",
                        "sse_url": self._sse_url,
                        "candidate_url": candidate,
                    },
                )
                return
            self._post_url = candidate
            self._endpoint_event.set()
            return
        if event_type != "message":
            return  # ignore unknown event types
        try:
            msg = _json.loads(data)
        except _json.JSONDecodeError:
            _logger.warning(
                "mcp_sse_invalid_json",
                extra={"event": "mcp_sse_invalid_json", "data": data[:200]},
            )
            return
        # JSON-RPC responses carry `id`; notifications don't.
        msg_id = msg.get("id")
        if msg_id is None:
            method = msg.get("method")
            if (
                method == "notifications/tools/list_changed"
                and self._on_tools_changed is not None
            ):
                # Phase 10f: bridge subscribes to this so server-
                # pushed tool changes trigger the same reconcile
                # path as admin "Refresh tools" clicks.
                try:
                    self._on_tools_changed()
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "mcp_on_tools_changed_callback_failed",
                        extra={
                            "event": "mcp_on_tools_changed_callback_failed",
                        },
                    )
            # Other notifications are silently dropped; future
            # phases can route additional methods here.
            return
        fut = self._pending.pop(msg_id, None)
        if fut is None:
            # We always send integer `id`s, but JSON-RPC 2.0 permits
            # string ids and a non-strict server (or a proxy that
            # JSON-reparses the body) can echo `1` back as `"1"` or
            # `1.0`. A type-mismatched key never matches the int we
            # stored, so the future is NEVER resolved and the caller
            # hangs the full `_RESPONSE_TIMEOUT_S` (60 s) then fails —
            # looking exactly like a dead upstream on EVERY call.
            # Fall back to an int-coerced lookup before giving up.
            coerced: int | None = None
            if isinstance(msg_id, str) and msg_id.lstrip("-").isdigit():
                coerced = int(msg_id)
            elif isinstance(msg_id, float) and msg_id.is_integer():
                coerced = int(msg_id)
            if coerced is not None:
                fut = self._pending.pop(coerced, None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"]
            fut.set_exception(
                McpError(
                    code=err.get("code", -32603),
                    message=err.get("message", "unknown error"),
                    data=err.get("data"),
                )
            )
        else:
            fut.set_result(msg.get("result", {}))

    # ------------------------------------------------------------------
    # Outbound channel
    # ------------------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON-RPC request; await its response on the SSE
        stream via a per-id future."""
        if self._post_url is None:
            raise McpError(-32603, "SSE endpoint URL not yet received")
        self._request_id += 1
        rid = self._request_id
        body = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        # finally so a wait_for timeout doesn't leak the _pending entry.
        try:
            resp = await self._client.post(
                self._post_url, json=body, headers=self._post_headers,
                timeout=30.0,
            )
            # SSE-transport MCP servers typically return 202 Accepted
            # and deliver the response via the stream. Some
            # implementations return 200 with an inline JSON
            # response — handle both.
            if resp.status_code == 200 and resp.content:
                try:
                    data = resp.json()
                except ValueError as exc:
                    # Inline-200 with a non-JSON body (proxy HTML
                    # page, text blurb). Same reasoning as the
                    # StreamableHttp `_call` guard — surface as a
                    # retryable -32603 rather than a bare
                    # JSONDecodeError the retry layer treats as
                    # permanent.
                    raise McpError(
                        -32603,
                        "non-JSON inline response from MCP SSE server",
                    ) from exc
                if "error" in data:
                    err = data["error"]
                    raise McpError(
                        code=err.get("code", -32603),
                        message=err.get("message", "unknown error"),
                        data=err.get("data"),
                    )
                return data.get("result", {})
            resp.raise_for_status()
            return await asyncio.wait_for(fut, timeout=self._RESPONSE_TIMEOUT_S)
        finally:
            self._pending.pop(rid, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """JSON-RPC notification — no `id`, no response correlation."""
        if self._post_url is None:
            raise McpError(-32603, "SSE endpoint URL not yet received")
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        resp = await self._client.post(
            self._post_url, json=body, headers=self._post_headers,
            timeout=30.0,
        )
        resp.raise_for_status()


# ===========================================================================
# Factory
# ===========================================================================


def build_mcp_client(
    transport: str,
    url: str,
    *,
    auth_kind: str = "none",
    auth_value: str | None = None,
    auth_header_name: str | None = None,
    on_tools_changed: callable[[], None] | None = None,
    server_id: str = "unknown",
) -> StreamableHttpMcpClient | SseMcpClient:
    """Pick the right MCP client based on the row's transport.

    Bridge code should call this rather than instantiating either
    class directly — keeps the transport choice in one place.

    `on_tools_changed` is wired up to the SSE client's
    `notifications/tools/list_changed` handler (Phase 10f). The
    Streamable HTTP transport has no equivalent server-push
    channel for this notification, so the kwarg is accepted but
    silently dropped for that transport — callers don't have to
    branch on transport just to handle the option.

    `server_id` is observability-only — threaded into the SSE
    client so reconnect metrics carry the bounded server label."""
    if transport == "streamable_http":
        # Streamable HTTP servers can't push notifications outside
        # an in-flight request — the on_tools_changed callback is
        # accepted for API symmetry with SSE but never fires. This
        # is fine: the supervisor polling path (Phase 10c) still
        # catches admin-driven refreshes.
        return StreamableHttpMcpClient(
            url,
            auth_kind=auth_kind,
            auth_value=auth_value,
            auth_header_name=auth_header_name,
        )
    if transport == "sse":
        return SseMcpClient(
            url,
            auth_kind=auth_kind,
            auth_value=auth_value,
            auth_header_name=auth_header_name,
            on_tools_changed=on_tools_changed,
            server_id=server_id,
        )
    raise ValueError(
        f"unknown MCP transport {transport!r}; "
        f"expected one of: streamable_http, sse"
    )
