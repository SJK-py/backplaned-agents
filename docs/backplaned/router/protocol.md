# Router — Wire Protocol

> Part 1 of the router design. Covers transport, frame schema, connection
> lifecycle, and correlation. See [`state.md`](./state.md) for the task state
> machine and ACL, and [`storage.md`](./storage.md) for persistence and the
> HTTP API.

## 1. Transport summary

| Channel              | Transport            | Purpose                                                   |
| -------------------- | -------------------- | --------------------------------------------------------- |
| Agent ↔ router       | **WebSocket** (TLS)  | All task delivery, results, progress, control             |
| File-store transfer  | HTTP/1.1 (TLS)       | Bulk file upload (with-grant) / download (presigned where possible) |
| Admin / onboarding   | HTTP/1.1 (TLS)       | Invitation issuance, user mgmt, agent onboarding handshake |
| Webapp UI            | HTTP + WebSocket     | UI consumes the same agent-side WebSocket protocol        |

Rationale recap (see [`overview.md`](../overview.md) §3): one full-duplex
socket per agent multiplexed by `correlation_id` removes per-task TLS/TCP
overhead, removes the need for agents to expose inbound listeners, and
unifies progress streaming with task delivery. Files stay on HTTP because
HTTP semantics (`Range`, streaming, presigned URLs, caching) are a
better fit for bulk bytes than WebSocket framing.

## 2. Frame envelope

Every frame on the WebSocket is a UTF-8 JSON object validated against a
discriminated union on `type`. Frames are individually self-describing —
no implicit state on the receiver beyond the correlation map.

### 2.1 Common header

All frames carry these fields:

| Field            | Type        | Required | Notes                                                          |
| ---------------- | ----------- | -------- | -------------------------------------------------------------- |
| `type`           | enum string | yes      | Discriminator. See §2.2.                                       |
| `protocol_version` | string    | yes      | `"1"` for this spec. Mismatch ⇒ socket closed with code 1002.  |
| `correlation_id` | string      | yes      | UUIDv4 today (UUIDv7 once the stdlib supports it). Used for ack and result matching. |
| `trace_id`       | string      | yes      | OpenTelemetry trace id. Propagated unchanged across the tree.  |
| `span_id`        | string      | yes      | Per-frame span id.                                             |
| `timestamp`      | RFC3339     | yes      | UTC, microsecond precision.                                    |
| `agent_id`       | string      | yes      | Sender's agent_id.                                             |

Per-frame additions are described in §2.2.

### 2.2 Frame types

```
                ┌─────────────────────────────────────────────┐
                │  agent ↔ router frame types                 │
                ├─────────────────────────────────────────────┤
                │  Hello       — auth handshake (first frame) │
                │  Welcome     — handshake response           │
                │  CatalogUpdate — pushed catalog refresh     │
                │  NewTask     — spawn / delegate             │
                │  Result      — terminal task outcome        │
                │  Progress    — interim event                │
                │  Cancel      — abort a task                 │
                │  Error       — protocol-level failure       │
                │  Ack         — receipt acknowledgement      │
                │  Ping/Pong   — heartbeat                    │
                └─────────────────────────────────────────────┘
```

**`Hello`** — first frame on a new socket, agent → router.

```jsonc
{
  "type": "Hello",
  "agent_id": "...",
  "auth_token": "...",          // short-lived JWT or signed bearer
  "sdk_version": "1.0.0",
  "agent_info": { ... },        // AgentInfo payload (see sdk.md)
  "resume_token": "..."         // optional: re-attach in-flight tasks
  // ...common header...
}
```

**`Welcome`** — router → agent, sent only after successful `Hello`.

```jsonc
{
  "type": "Welcome",
  "session_id": "...",          // server-issued, valid until disconnect
  "available_destinations": { ... },   // ACL-filtered tools (compact form)
  "capabilities": [ ... ],      // capability strings the agent provides
  "heartbeat_interval_ms": 20000,
  "max_payload_bytes": 1048576
}
```

**`CatalogUpdate`** — router → agent, pushed whenever the catalog
could change for a connected agent: a new agent onboards, an agent
is suspended or evicted, or admin mutates the ACL rule list.
Carries the full catalog snapshot — receivers drop their cached
`available_destinations` and adopt the payload as-is. Disconnects
do **not** trigger `CatalogUpdate`; the resume window absorbs flaps
and admit-time `agent_disconnected` is the existing safety net.

```jsonc
{
  "type": "CatalogUpdate",
  "available_destinations": { ... }    // full replacement
}
```

**`NewTask`** — agent → router (or router → agent on dispatch). Both
spawn (new task tree) and delegate (preserve task_id) use this frame; the
distinction is `task_id == null` vs. set.

```jsonc
{
  "type": "NewTask",
  "task_id": null,                          // null = spawn, str = delegate
  "parent_task_id": "...",                  // null at root
  "destination_agent_id": "gemini_main",
  "user_id": "...",                         // first-class, see overview §P5
  "user_level": "tier1",                    // set by the router on outbound delivery
  "session_id": "...",
  "priority": "normal",                     // "low"|"normal"|"high"
  "deadline": "2026-04-26T12:34:56Z",       // optional hard deadline
  "idempotency_key": null,                  // optional, see §4.3
  "input_mode": null,                       // null = destination's sole mode; required if multi-mode
  "payload": { ... }                        // shape = destination's accepts_schema[input_mode]
}
```

`payload` is opaque to the router. The destination's
`AgentInfo.accepts_schema` is a **per-mode map**
`{mode: <JSON Schema>|null}`; `input_mode` selects which mode this
frame targets — `null` resolves to the destination's sole mode (the
common single-handler case) and is **required** when the destination
exposes several. The router validates `payload` against
`accepts_schema[input_mode]` at admit and rejects a mismatch — or an
unknown / ambiguous mode — with `Error{code:"schema_mismatch"}`
before the task row is created. A `null` schema for a mode is the
explicit "admit without payload validation" escape hatch (used e.g.
by the MCP bridge when an MCP tool advertises no input_schema).
`user_level` is populated by the router from the user's record on
outbound delivery; agents writing inbound `NewTask` frames leave it
empty (the router ignores it on the receive side and looks the
level up itself).

> **Per-mode multiplexing in practice** — the MCP bridge bundles
> *one Agent per MCP server* with *one mode per MCP tool*: each MCP
> tool's `inputSchema` is operator-pinned as the per-mode schema
> (`accepts_schema = {tool_name: tool.input_schema, ...}`), and the
> tool's name doubles as the routing mode. Tool adds / removes /
> schema-updates on `tools/list_changed` flow as a single
> `AgentInfoUpdate` patching `accepts_schema` and `non_tool_modes` —
> no per-tool socket / catalog churn. The externally-visible LLM
> tool name resolves to `call_<agent>_<mode>` =
> `call_mcp_<server>_<tool>`.

**`Result`** — terminal outcome of a task (success, error, timeout,
cancelled). One per task, ever.

```jsonc
{
  "type": "Result",
  "task_id": "...",
  "parent_task_id": "...",
  "status": "succeeded",         // succeeded|failed|cancelled|timed_out
  "status_code": 200,            // numeric, mirrors HTTP semantics
  "output": {                    // AgentOutput-shaped
    "content": "...",
    "files": ["chart.png", "persist/r.pdf"]  // file-store NAMES the
                                              // producer surfaced
  },
  "error": null                  // populated when status != succeeded
}
```

`output.files` is a list of file-store **NAMES** (`{filename}` for
the session stash, `persist/{filename}` for the user-wide persistent
stash) the producer chose to surface. They ride inside the typed
`output` as plain strings — there is no separate `attachments`
channel and no per-file `feed_llm` flag. Bytes never cross a frame:
a peer in the same `(user_id, session_id)` reaches a named file via
the `File*` frames (upload-with-grant, signed-URL download), and an
LLM-orchestrating parent threads the names into its next provider
call with `Message.tool_response_from_result(...)` — each becomes a
`{"file_ref": {"name": …}}` part the ROUTER resolves (the modality
is inferred from the named blob's mime type).

**`Progress`** — interim event during long-running tasks. Replaces the
current SSE channel.

```jsonc
{
  "type": "Progress",
  "task_id": "...",
  "event": "thinking",           // thinking|tool_call|tool_result|chunk|status
  "content": "...",              // free-form, may stream tokens
  "metadata": { ... }            // event-specific (tool name, token index, etc.)
}
```

**`Cancel`** — request abort of an in-flight task. Propagates to all
descendants. Recipient responds with a `Result` of `status="cancelled"`.

```jsonc
{
  "type": "Cancel",
  "task_id": "...",
  "reason": "user_aborted"       // short, machine-readable
}
```

The router authorises `Cancel` against the task tree: the calling
agent must be the assignee of the target task or any of its
ancestors. A `Cancel` for an unrelated task tree is dropped silently
(audit-logged) — no error response, so a malicious agent can't
enumerate other users' task IDs by timing the response shape.

`Cancel` also has an LLM-call abort mode where `task_id` is omitted
and `ref_correlation_id` references a still-streaming `LlmRequest`
on the same socket. The router then cancels just that one
provider-side asyncio task. Authorisation is implicit — the LLM-task
map is per-socket, so an agent can only abort its own in-flight
calls.

**`Error`** — protocol-level failure (validation, auth, ACL deny). Not
used for task-level failures (which use `Result` with non-2xx
`status_code`).

```jsonc
{
  "type": "Error",
  "code": "acl_denied",          // see §5 for the catalog
  "message": "...",
  "ref_correlation_id": "...",   // the offending frame, if applicable
  "retryable": false
}
```

**`Ack`** — receipt acknowledgement. Sent by the receiver of a
`NewTask`, `Result`, or `Cancel` frame, with `correlation_id` matching
the original. See §5 for semantics.

```jsonc
{
  "type": "Ack",
  "ref_correlation_id": "...",
  "accepted": true,
  "reason": null                 // populated when accepted=false
}
```

**`Ping` / `Pong`** — heartbeat. Either side may send `Ping`; receiver
must reply with `Pong` carrying the same `correlation_id`. See §4.4.

**`AgentInfoUpdate`** — patch-update the agent's published surface
mid-flight (Phase 10e). Agent → router. Optional fields:
`description`, `groups`, `capabilities`, `accepts_schema`,
`produces_schema`, `produces_files`, `non_tool_modes`, `hidden`,
`documentation_url`. Only non-None fields are applied; `agent_id`
is locked at the WS-authenticated value. Router serialises
concurrent updates against the agent row via `SELECT ... FOR
UPDATE` inside one `conn.transaction()` so two frames can't
lost-update each other. Rate-limited per-agent
(`agent_info_update_rate_limit_per_agent_*`, default 1/s burst 5);
saturation Ack with `reason="rate_limited"`. Successful update
broadcasts a `CatalogUpdate` to every connected agent so peers'
`peers.visible()` reflects the change without reconnect.

### 2.2.1 LLM-channel frames

Three frames carry router-mediated LLM calls. Wire-level handshake:

```
agent ─── LlmRequest ────────────► router
agent ◄── LlmDelta (×N, optional) router    # streaming
agent ◄── LlmResult ───────────── router    # always — success or error
```

Each `LlmDelta` and `LlmResult` carries `ref_correlation_id` matching
the originating `LlmRequest.correlation_id`, so multiple concurrent
calls on one socket stay disambiguated.

**`LlmRequest`** — kick off a call.

```jsonc
{
  "type": "LlmRequest",
  "kind": "generate",            // generate | embed | count_tokens
  "preset": "claude-haiku",      // preferred — bundle name from llm_presets
  "model": "default",            // legacy alias; preset wins when both set
  "messages": [...],             // chat-style messages (or text=[...] for embed)
  "tools": [...],                // optional tool specs
  "tool_choice": "auto",         // auto | none | required | {function_name}
  "temperature": 0.7,            // overrides preset default when set
  "max_tokens": 1024,
  "stream": true,                // when true, expect LlmDelta events
  "provider_options": {...},     // REPLACES preset's default_provider_options
  "user_id": "usr_...",          // for tier-gate lookup
  "task_id": "tsk_..."           // optional — telemetry / metrics correlation
}
```

**`LlmDelta`** — incremental output (streaming). Streamed in order
between the `LlmRequest` and the terminating `LlmResult`. Exactly one
of `text`, `tool_call`, `reasoning_block`, `finish_reason`, or `usage`
is populated per delta.

```jsonc
{
  "type": "LlmDelta",
  "ref_correlation_id": "...",   // matches LlmRequest.correlation_id
  "text": "Hello",               // text chunk (mutually exclusive with others)
  "tool_call": {...},            // a finalised tool call
  "thought": false,              // true if `text` is reasoning, not answer
  "thought_signature": "...",    // Gemini round-trip signature
  "reasoning_block": {...},      // Anthropic / OpenAI reasoning block
  "finish_reason": null,         // populated on the final delta only
  "usage": null                  // populated on the final delta only
}
```

**`LlmResult`** — terminal. One per request, regardless of streaming.
For non-streaming calls carries the full text + tool_calls. For
streaming calls, the per-delta state has already been streamed; the
result still carries `usage`, `finish_reason`, and the aggregated
`reasoning_blocks` for round-trip support.

```jsonc
{
  "type": "LlmResult",
  "ref_correlation_id": "...",
  "text": "...",                 // full text (non-streaming) or "" (streaming)
  "tool_calls": [...],
  "finish_reason": "stop",       // stop | length | tool_calls | content_filter | error
  "usage": {...},
  "thought_summary": null,
  "thought_signature": null,
  "reasoning_blocks": [...],     // round-trip blocks for the next assistant turn
  "vectors": [[...]],            // populated only for kind="embed"
  "total_tokens": 0,             // populated only for kind="count_tokens"
  "error": {                     // populated only on failure
    "code": "preset_unknown",    // see §5
    "message": "..."
  }
}
```

`Cancel` may abort an in-flight `LlmRequest` mid-stream by setting
`ref_correlation_id` to the request's correlation id and leaving
`task_id` unset. The router cancels the per-socket asyncio task; no
further deltas are emitted. Authorisation is implicit — the LLM-task
map is per-socket, so an agent can only abort its own in-flight calls.

### 2.3 Validation rules

- All frames are validated against Pydantic models at the router edge
  before any business logic runs. Invalid frames are responded to with
  `Error{code:"frame_invalid"}` and the offending frame is dropped.
  The error `message` is bounded at 200 chars via
  `safe_validator_message` so a misbehaving custom validator can't
  leak unbounded input fragments back to the sender.
- `payload` for `NewTask` is validated against the destination agent's
  declared `accepts` schema (see `sdk.md`). Schema mismatch ⇒
  `Error{code:"schema_mismatch"}` with the validation report; the frame
  does **not** create a task row.
- `protocol_version` mismatch ⇒ socket closed with WebSocket close
  code 1002 and a final `Error{code:"protocol_version"}` frame.
- Frame size limit: `max_payload_bytes` (default 1 MiB) advertised in
  `Welcome`. Larger frames close the socket with code 1009. **The
  Hello frame is bounded too** — oversized Hello closes before
  `parse_frame` to defend against unauthenticated parse-CPU
  exhaustion (`hello_too_large` reason, code 1009).
- Per-IP handshake rate limit on `/v1/agent` (default 5/s, burst 20).
  Saturation closes the socket with WebSocket close code **4029**
  (`reason="rate_limited"`) BEFORE the JWT verify, so a flooding
  IP can't burn HMAC + Redis lookups. Tunable via
  `ws_handshake_rate_limit_per_ip_per_s` / `_burst`; set rate=0 to
  disable.

## 3. Connection lifecycle

```
   agent                                router
     │                                    │
     ├── HTTP POST /onboard ─────────────►│   (one-time, invitation token)
     │◄── OnboardResponse ────────────────┤   (auth_token, agent_id, …)
     │                                    │
     ├── WS UPGRADE /v1/agent ───────────►│
     │◄── 101 Switching Protocols ────────┤
     │                                    │
     ├── Hello ──────────────────────────►│
     │                                    │   verify token, register socket
     │◄── Welcome ────────────────────────┤
     │                                    │
     │  ╔════════════════ steady state ═══╗
     │  ║                                  ║
     │  ║  NewTask, Result, Progress,      ║
     │  ║  Cancel, Ack, Ping/Pong          ║
     │  ║                                  ║
     │  ╚══════════════════════════════════╝
     │                                    │
     │  (disconnect — see §4.5)           │
     ▼                                    ▼
```

### 3.1 Onboarding

External agents perform a one-time HTTP POST to `/v1/onboard` with an
invitation token (issued by an admin). The router responds with
`{agent_id, auth_token, ...}`. This handshake stays on HTTP because:

- It happens once per agent, not per session.
- It needs human-mediated invitation flow.
- Failure modes (token expired, already used) are simpler to surface
  in HTTP semantics.

The `auth_token` is short-lived (default 24h) and refreshed automatically
by the SDK via a `/v1/agent/refresh-token` endpoint.

If the invitation was flagged `provisions_service_user`, the response
additionally carries a co-located `level=service` principal
(`service_user_id` + `service_refresh_token`) so a channel / gateway
agent gets its own service identity at first boot — see
[`security.md` §3.2](../security.md).

### 3.2 Connect / Hello

The agent opens a WebSocket to `/v1/agent` and immediately sends `Hello`
with its `auth_token`. The router:

1. Validates the token (signature + expiry + DB lookup).
2. Verifies the `agent_id` is in the expected state (registered, not
   suspended).
3. If a previous socket for this `agent_id` is still mapped, that socket
   is closed with code **4003** (`reason="superseded"`) before the new
   one is registered. Only one live socket per `agent_id`. (4003 is
   distinct from 4001 auth-failed and 4029 rate-limited — three
   separate codes in the 4000–4999 private range so a client can
   tell them apart without scraping the reason string.)
4. Replies with `Welcome` carrying the agent's ACL-filtered destinations
   and runtime parameters.

### 3.3 Resume semantics (optional)

If the agent supplies `resume_token` in `Hello`, and the token matches a
recently-disconnected session whose in-flight tasks were not yet failed,
the router re-attaches the socket and **does not** fail those tasks. The
resume window is short (default 30 s) and configurable per deployment.
This is opt-in — the simple path is "drop = fail in-flight, reconnect
fresh."

### 3.4 Heartbeat

After `Welcome`, both sides start a heartbeat timer. The router sends a
`Ping` every `heartbeat_interval_ms` of socket idle time; the agent
responds with `Pong` echoing the `correlation_id`. Two missed pings ⇒
the router closes the socket with code **4002** (`reason="heartbeat_timeout"`)
and the disconnect path runs (§3.5). Agents may also initiate `Ping`.

Code **4002** is also used when the heartbeat `Ping` itself cannot be
enqueued because the agent's outbox is saturated
(`reason="outbox_full"`) — same code, distinct reason: a persistently
full outbox is functionally an unresponsive peer.

Close-code summary: **1002** (bad/oversized-after-decode first frame,
protocol-version mismatch), **1009** (Hello/payload too large),
**4001** (auth failed), **4002** (heartbeat_timeout / outbox_full),
**4003** (admin lifecycle: `superseded`, `agent_reprovision`, `agent_reset`,
`agent_suspended`, `agent_evicted` — distinguished by `reason`), **4029**
(handshake rate-limited).

**Client self-heal on credential invalidation.** When the router rejects the
agent's token on credential grounds — **4001** `auth_failed` at the handshake,
or **4003** `agent_reprovision` / `agent_reset` on a live socket — the SDK
transport DROPS the persisted token (`credentials.json`) and re-onboards with
its `invitation_token` before reconnecting (`bp_sdk.onboarding.reonboard_with_invitation`).
This is what recovers an agent whose stored token was signed by a since-rotated
`ROUTER_JWT_SECRET`: it can't detect a stale signature locally (no secret), so
`onboard_or_resume` would otherwise resume the dead token forever. The retry is
bounded (`reonboard_max_attempts`, reset on a successful handshake) so a
non-recoverable agent — no invitation, **evicted** (`agent_evicted`, terminal),
or a spent single-use invitation — can't hot-loop the onboard endpoint. The
other 4003 reasons are deliberately excluded: `agent_suspended` is an
intentional stop, `superseded` means a newer socket won (the token is fine).

### 3.5 Disconnect

On any disconnect (clean close, error, heartbeat timeout, supersede):

1. Remove the socket from the in-memory `agent_id → WebSocket` registry.
2. If resume window applies (§3.3), park the entry in a "pending
   resume" structure with TTL.
3. Otherwise, fail every in-flight task currently assigned to this
   `agent_id` with `status="failed"`, `status_code=503`, error
   `agent_disconnected`. Propagate result frames to parents.
4. Emit a `disconnect` audit event with the close code and reason.

## 4. Correlation model

Two distinct correlation needs, handled separately:

**Frame-level acks** — confirms that a peer received and accepted (or
rejected) a specific frame. Carried by the `Ack` frame, matched by
`correlation_id`. Lives entirely in process memory.

**Task-level lifecycle** — confirms eventual completion of a task.
Carried by the `Result` frame, matched by `task_id`. Persisted in the
`tasks` table; survives router restarts via the durable state machine
(see [`state.md`](./state.md)).

### 4.1 Frame-level ack flow

```
   sender                              receiver
     │                                    │
     ├── NewTask{correlation_id=X} ──────►│
     │                              (validate, enqueue work)
     │◄── Ack{ref_correlation_id=X} ──────┤
     │   (Future for X resolves)
```

The sender registers a Future keyed by `correlation_id` before sending,
awaits with a configurable timeout (default 30 s, mirroring the current
HTTP behaviour), and resolves on `Ack`. Timeout ⇒ the Future is rejected
with `ack_timeout` and the task that prompted the frame is failed via
the same path used for transport errors today.

The router does not need a second pending-ack system on top of SQLite —
the existing task table + `timeout_sweep` already carry task-level
correlation. The frame ack is in-memory only.

### 4.2 Task-level lifecycle flow

```
   parent agent                  router                   child agent
        │                          │                          │
        ├── NewTask(t=null) ──────►│                          │
        │◄── Ack ──────────────────┤   (task_id assigned)     │
        │                          ├── NewTask(t=T1) ────────►│
        │                          │◄── Ack ──────────────────┤
        │                          │                          │
        │                          │     … work happens …     │
        │                          │                          │
        │                          │◄── Progress(t=T1) ───────┤
        │◄── Progress(t=T1) ───────┤                          │
        │                          │                          │
        │                          │◄── Result(t=T1) ─────────┤
        │                          ├── Ack ──────────────────►│
        │◄── Result(t=T1) ─────────┤                          │
        ├── Ack ──────────────────►│                          │
```

The router fans Progress frames out to subscribers (the parent and any
UI listeners). Result is delivered exactly once to the parent agent;
the router persists it before fan-out so a router crash mid-fan-out
does not lose the result.

### 4.3 Idempotency

`NewTask` from agents may carry an optional `idempotency_key`
(string). The router deduplicates per **`(caller_agent_id, user_id,
idempotency_key)`** (the `tasks_idempotency_unique` constraint): a
second `NewTask` with the same key, from the same caller agent for the
same user, does not create a new task. Safe retries on flaky networks.
The key is not visible to the destination agent.

The dedup is **per caller agent**, so a *different* caller agent that
reuses the same key string for the same user gets its OWN task — it
never receives another agent's result (no cross-agent leak).

The dedup is **permanent** — there is no expiry window. A key is
effectively single-use for the lifetime of its task row: reusing it
always replays the original outcome (below) rather than starting a
fresh task. Callers that want a fresh task per logical operation must
use a fresh key (e.g. a UUID); a deterministic/semantic key is a
*deliberate* "run this at most once, ever" contract. (A future GC
sweep could free keys past a TTL to add a true reuse window; until
then, treat keys as permanent.)

The dedup response depends on the existing task's state:

- **Still in flight** (QUEUED / RUNNING / WAITING_CHILDREN): the
  `Ack` carries the existing `{task_id}`; the retry joins the live
  task and its eventual terminal `Result` fans out normally.
- **Already terminal** (SUCCEEDED / FAILED / CANCELLED / TIMED_OUT):
  the original terminal `Result` was fanned out exactly once, to the
  original requester, and is never re-emitted. So the router *also*
  reconstructs and re-emits that terminal `Result` after the `Ack`
  on the retrying socket — otherwise the canonical
  "retry-after-ack-timeout" caller would hang waiting for a frame
  that can never come. The replayed `Result` carries the retry's
  trace/span. When the stored row has a NULL `status_code` (router-
  synthesised terminals — notably CANCELLED), the replay uses the
  faithful per-status code the original fan-out used (CANCELLED →
  499, TIMED_OUT → 504, FAILED → 500, SUCCEEDED → 200), never 0.

A rare benign double-deliver (the non-locking dedup read racing the
original fan-out on the same socket) is absorbed by the SDK's
`PendingMap` bounded buffer — see `docs/sdk/core.md` §6.

### 4.4 Ordering guarantees

Per-socket: WebSocket guarantees in-order delivery within a single
connection. The router preserves that ordering when forwarding to the
destination socket (no reordering, no fan-in interleaving across
sources).

Across sockets: no ordering guarantee. Two parents writing to the same
child see their `NewTask` frames interleaved in unspecified order.
Agents must not assume cross-source ordering.

### 4.5 Backpressure

The router maintains per-socket send queues bounded by
`per_socket_outbox_max` (default 256 frames). When full:

- For **`Progress`** frames: drop oldest (best-effort delivery).
- For **`NewTask` / `Result`**: apply backpressure to the producer by
  awaiting queue space, with a deadline. On deadline, the originating
  task is failed with `backpressure_timeout`.

Bound the queue, drop or coalesce on overflow — never let one slow
peer bloat router memory.

## 5. Error code catalog

The strings the router uses in `Error.code` and as `AdmitError.code`
(the latter visible to clients via the admin test endpoint and the
admit reject Ack). Authoritative source: `bp_protocol.frames.ErrorCode`.

| Code                  | Where it surfaces                                              | Retryable? |
| --------------------- | -------------------------------------------------------------- | ---------- |
| `protocol_version`    | Hello with mismatched `protocol_version` — socket close 1002.  | no         |
| `frame_invalid`       | Frame fails Pydantic validation at the router edge.            | no         |
| `auth_failed`         | JWT verification failed; `agent_id` mismatch.                  | no         |
| `auth_expired`        | JWT past `exp`.                                                | no         |
| `agent_suspended`     | Hello from an agent whose row is `status='suspended'`.         | no         |
| `agent_removed`       | Hello from an agent whose row is `status='removed'` (terminal). | no        |
| `agent_not_found`     | Admit references an unknown destination (or caller).           | no         |
| `agent_disconnected`  | Admit reaches a known agent with no live socket.               | yes (after agent reconnects) |
| `session_unknown`     | Admit's `(user_id, session_id)` pair points at no row.         | no         |
| `session_closed`      | Admit's session row has `closed_at` set.                       | no (open a new session) |
| `acl_denied`          | Admit ACL check rejects the call.                              | no         |
| `acl_grant_invalid`   | (reserved — current grammar has no grants)                     | n/a        |
| `schema_mismatch`     | Admit payload fails `accepts_schema[input_mode]`, or `input_mode` is unknown / ambiguous (multi-mode + none given). | no         |
| `quota_exceeded`      | Admit `Ack{accepted:false}` — per-(user, level) admit-rate token bucket exhausted (carries `retry_after_s`). | yes (after `retry_after_s`) |
| `backpressure_timeout`| Outbox couldn't drain within deadline; task fails.             | yes        |
| `ack_timeout`         | Per-frame ack didn't arrive in time.                           | yes        |
| `internal_error`      | Unexpected router-side failure.                                | yes        |

LLM-channel errors (returned in `LlmResultFrame.error.code`, not as a
top-level `ErrorFrame`):

| Code                       | Where it surfaces                                                                                | Retryable? |
| -------------------------- | ------------------------------------------------------------------------------------------------ | ---------- |
| `preset_unknown`           | `LlmRequest.preset` (or legacy `model`) names no preset in `llm_presets`.                        | no         |
| `preset_not_allowed`       | Caller's user_level fails the preset's `min_user_level` tier gate.                               | no         |
| `auth_lookup_failed`       | DB unreachable while resolving the caller's user_level for a tier-gated preset.                  | yes        |
| `upstream_timeout`         | Provider timed out (`APITimeoutError`, `DeadlineExceeded`).                                      | **yes**    |
| `upstream_rate_limited`    | Provider returned 429 / `RateLimitError`. `Retry-After` mirrored into `error.retry_after_seconds`. | **yes**  |
| `upstream_unavailable`     | Provider 5xx / connection error (`InternalServerError`, `ServiceUnavailable`).                   | **yes**    |
| `upstream_invalid_request` | 400 — bad prompt, oversized message, malformed tool spec.                                        | no         |
| `upstream_auth_failed`     | 401/403 — wrong API key, expired credential.                                                     | no         |
| `upstream_content_filter`  | Provider blocked the prompt or response on content-policy grounds.                               | no         |
| `upstream_quota_exhausted` | Account-level quota out (admin must rotate / upgrade).                                           | no         |
| `stream_interrupted`       | Connection dropped after deltas had been delivered. Partial output is in the agent's iterator.   | no         |

**`error.retriable` flag.** `LlmResultError` carries an explicit
`retriable: bool` derived from the code via `RETRIABLE_LLM_CODES`
(`bp_protocol.frames`). SDK retry policies can switch on the bool
without enumerating codes; dashboards filter by code for cause
breakdowns. The router computes the bool from the code at frame
construction time so the wire flag and the typed code can't drift.

**`error.retry_after_seconds`** mirrors HTTP `Retry-After` from
rate-limited upstreams (`upstream_rate_limited`). SDK retry policy
honours it verbatim, capped at the configured `max_backoff_s`.

> **Status (PR #1 of the M6 sequence):** the `upstream_*` and
> `stream_interrupted` codes are RESERVED in the wire vocabulary but
> not yet emitted by the router. PR #2 wires per-provider classifiers
> for non-streaming generate / embed / count_tokens; PR #3 wires the
> streaming setup-retry loop and emits `LlmDeltaMeta` deltas during
> backoff. See `docs/design/llm-retriable-errors.md`.

**Streaming retry-pending hints (`LlmDelta.meta`).** When the router's
streaming setup-retry loop pauses between attempts, it emits a
status-only `LlmDelta` carrying a `LlmDeltaMeta` payload so UI
clients can show a spinner during the backoff:

```jsonc
{
  "type": "LlmDelta",
  "ref_correlation_id": "...",
  "text": null, "tool_call": null, "finish_reason": null, "usage": null,
  "meta": {
    "kind": "retry_pending",
    "attempt": 2,            // 1-indexed; the just-failed attempt
    "max_attempts": 3,
    "retry_after_seconds": 5.2,
    "reason_code": "upstream_rate_limited"
  }
}
```

**Mutual-exclusivity invariant.** When `meta` is set, every content
field on the same `LlmDelta` (`text`, `tool_call`, `finish_reason`,
`usage`, `thought_signature`, `reasoning_block`, `thought`) MUST be
None / False. The router enforces this at frame construction; SDK
clients reject malformed deltas during validation. Clients that
don't care about the spinner can `if delta.meta: continue` — the
retry remains transparent.
