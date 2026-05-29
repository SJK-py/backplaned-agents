# SDK — Core

> Part 1 of the agent SDK design. Covers the agent author surface,
> handler model, transport abstraction, frame dispatch, and lifecycle.
> See [`services.md`](./services.md) for the LLM service, file
> handling, progress, and worked examples.

## 1. Role and design philosophy

The SDK is the **only** code an agent author should need to write
against. Transport (HTTP vs. WebSocket), correlation, ack handling,
heartbeat, reconnection, embedded-vs-external dispatch, and ACL
plumbing all live below the SDK surface. Agent code looks the same
whether the agent runs in-process inside the router or as a separate
container in another datacenter.

Five rules govern the surface:

**S1. Handlers, not endpoints.** Agent authors register typed
coroutines. They never call FastAPI, never write `/receive`, never
touch sockets.

**S2. Typed inputs and outputs.** Every handler declares its accepted
input model and its output model. The SDK validates at the boundary
and raises typed errors before the handler runs.

**S3. One context object.** Every handler receives a `TaskContext`
that exposes everything it needs: cancel token, progress emitter,
file manager, LLM service, peer-call helpers, logger, trace context.
No globals, no singletons reachable from handler code.

**S4. Embedded vs. external is a deployment flag.** The agent author
writes `@agent.handler` once. A config switch decides whether the
agent runs inside the router process or stands up its own WebSocket
client.

**S5. Failures are values.** Handler exceptions become typed `Result`
frames with appropriate status codes. The SDK does not let a stray
exception bring down the agent loop.

## 2. Minimal agent

This is the entire surface required to run a working agent:

```python
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

agent = Agent(
    info=AgentInfo(
        agent_id="echo",
        description="Echoes the prompt back, in uppercase.",
        groups=["rank2"],
        capabilities=["text.transform.uppercase"],
    ),
)

@agent.handler
async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
    return AgentOutput(content=payload.prompt.upper())

if __name__ == "__main__":
    agent.run()
```

That's it. Onboarding (first run only), reconnect, ack, heartbeat,
trace propagation, ACL hand-off, and graceful shutdown are all
handled by `agent.run()`. Runnable, progressively richer agents that
exercise the whole surface live in `examples/test_drive/` (see §7).

The handler's input model is **inferred from the second positional
argument's type annotation** (`payload: LLMData` here); it drives
boundary validation and supplies the default **mode** name
(`LLMData`). Dispatch itself is by mode key, not by the Python type
(see the multi-handler note below). `AgentInfo` carries identity (`agent_id`, `description`,
`groups`, `capabilities`, `documentation_url`, `hidden`) plus the
auto-derived `accepts_schema` (per-mode map), `produces_schema`, and
`non_tool_modes`; the JSON-schema fields validate `NewTask.payload` /
`Result.output` at the boundary.

**Return typing (strict, and asymmetric).** If the handler is
annotated `-> SomeModel`, a return that is NOT an instance of that
model (e.g. a bare `dict`) raises `HandlerError` and FAILS the task —
the strict check deliberately surfaces wrong-typed returns the old
silent coerce-cascade used to mask. `return None`, however, yields an
empty `AgentOutput()` and the task SUCCEEDS. This `dict`→fail /
`None`→empty asymmetry is intentional; annotate `-> AgentOutput` (or
`-> Any`) to opt out of the strict check and keep permissive
coercion. Files are returned out-of-band by NAME — `return
AgentOutput(content=..., files=["chart.png"])` — where each entry is
a router-managed file-store name (`{filename}` or
`persist/{filename}`) the producer stashed with
`ctx.files.store(...)`. The names ride inside `output.files`; the
bytes never cross the frame (see `bp_protocol.types.AgentOutput`).

**The two standard types** (`bp_protocol.types`) — the default I/O for
LLM-backed agents, referenced throughout these docs:

```python
class LLMData(BaseModel):              # the common inbound payload
    prompt: str
    agent_instruction: str | None = None   # → a system Message (NOT provider_options)
    context: str | None = None             # optional extra context blob

class AgentOutput(BaseModel):          # what handlers return
    content: str | None = None
    files: list[str] = []              # file-store NAMES (not bytes) — §7 / services.md §2
    metadata: dict[str, Any] = {}      # free-form (token usage, citations, …)
```

Agents are free to declare their own Pydantic payload models (the
mode is the model's class name — see §3); `LLMData` is just the
conventional shape channel/orchestrator agents forward.

## 3. `Agent` object

The top-level entry point. One per process for external agents; one
per registered agent for embedded agents (multiple may co-exist in the
router process).

```python
class Agent:
    def __init__(
        self,
        info: AgentInfo,
        *,
        config: AgentConfig | None = None,
    ) -> None: ...

    # Bare `@agent.handler`, or parameterised
    # `@agent.handler(mode="…", tool=False)`.
    def handler(
        self,
        fn: HandlerFn | None = None,
        *,
        mode: str | None = None,    # default: payload model's class name
        tool: bool = True,          # False → control-plane (non_tool_modes)
        description: str | None = None,  # per-mode tool description (see below)
    ) -> HandlerFn | Callable[[HandlerFn], HandlerFn]: ...

    def on_startup(self, fn: Callable[[], Awaitable[None]]) -> None: ...
    def on_shutdown(self, fn: Callable[[], Awaitable[None]]) -> None: ...

    # Patch the published AgentInfo mid-flight (AgentInfoUpdate).
    async def update_info(self, *, description: str | None = None,
                          groups=None, capabilities=None,
                          accepts_schema=None, produces_schema=None,
                          non_tool_modes=None, hidden=None,
                          documentation_url=None) -> None: ...

    def run(self) -> None: ...                    # blocking; for external
    async def run_async(self) -> None: ...        # for embedded
    async def aclose(self) -> None: ...
```

`AgentConfig` is a Pydantic-Settings model loaded from environment
variables (prefix `AGENT_`) by default; pass `config=AgentConfig(...)`
to override programmatically. Every field maps to `AGENT_<FIELD>`:

| Env var | Type | Default | Purpose |
| --- | --- | --- | --- |
| `AGENT_ROUTER_URL` | str | `ws://localhost:8000/v1/agent` | Router WS endpoint (external agents). |
| `AGENT_INVITATION_TOKEN` | str | — | One-time onboarding token (first run only). |
| `AGENT_AUTH_TOKEN` | str | — | Persisted agent JWT; auto-written after onboarding. |
| `AGENT_STATE_DIR` | path | `./agent_state` | Where `credentials.json` (0600) is persisted. |
| `AGENT_ONBOARD_URL` | str | — | Override the onboarding HTTP endpoint. |
| `AGENT_EMBEDDED` | bool | `false` | Run in-process inside the router (no WS / onboarding). |
| `AGENT_PENDING_RESULTS_TIMEOUT_S` | float | `60.0` | Cap on awaiting a peer/LLM result. |
| `AGENT_PENDING_ACKS_TIMEOUT_S` | float | `30.0` | Cap on awaiting a frame ack. |
| `AGENT_PROGRESS_BUFFER_SIZE` | int | `256` | Outbound queue depth before progress coalescing. |
| `AGENT_RECONNECT_INITIAL_BACKOFF_S` | float | `0.5` | First reconnect backoff. |
| `AGENT_RECONNECT_MAX_BACKOFF_S` | float | `30.0` | Reconnect backoff ceiling. |
| `AGENT_RECV_CONSECUTIVE_FAILURES_MAX` | int | `16` | Consecutive recv failures before the agent gives up (→ non-zero exit, §8). |
| `AGENT_WS_MAX_RECEIVE_BYTES` | int | `2097152` (2 MiB) | Hard ceiling on an incoming WS message (the `websockets` `max_size`); MUST be raised in lockstep when the router's `max_payload_bytes` is raised — see §7. |
| `AGENT_LOG_LEVEL` | str | `INFO` | Root log level. |

A single `Agent` may register multiple handlers via `@agent.handler`.
Each handler has an explicit **mode** name (default: its payload
model's class name; override with `@agent.handler(mode="…")`).
Dispatch is an O(1) lookup of `NewTaskFrame.input_mode` against the
unified mode registry — never a structural guess at the payload
shape, so routing is deterministic and independent of registration
order. `input_mode = None` resolves to the sole handler when there
is exactly one; ambiguous (>1) or unknown → `no_handler`. An agent
that handles only one shape needs one decorator and callers can omit
`mode`. A `dict`-input handler has no model name to default from, so
it **requires** an explicit `mode=`; two handlers resolving to the
same mode raise at registration (fail fast, no silent shadowing).

`@agent.handler(description="…")` sets that mode's **per-mode tool
description**, published as `AgentInfo.mode_descriptions[mode]`. A
calling LLM sees it on the generated `call_<agent>[_<mode>]` tool
instead of the agent-level `description` — use it on a multi-mode
agent so each tool reads distinctly. `mode=` and `tool=` compose with
it freely (e.g. `@agent.handler(mode="summarize", tool=False)`).

`@agent.handler(tool=False)` registers a **control-plane** mode:
still validated and dispatched normally, but its mode is listed in
`AgentInfo.non_tool_modes` so `build_tools` never advertises it to
tool-using models (slash commands / channel ops). There is no
separate control or delegation registry — a handler reads
`ctx.delegating_agent_id` if it needs delegation-aware behaviour.

`AgentInfo.accepts_schema` is auto-derived as a per-mode map
`{mode: <JSON Schema>|null}` (`null` = a `dict`-input mode the
router admits without payload validation). `build_tools` emits one
tool per **tool-visible** mode: `call_<agent_id>` when exactly one
mode is tool-visible, `call_<agent_id>_<mode>` when several. The
single-mode bare name is display-only — it still carries that mode:
`spawn_from_tool_call` / `tools.resolve_tool_name` round-trip
`call_<agent_id>` back to `(agent_id, <ModeName>)`, so the
dispatched `input_mode` is the model-named mode (e.g. `"LLMData"`),
never dropped. *Default mode* means the mode IS the payload model's
class name, not the absence of a mode. A tool that genuinely
carries `input_mode=None` (with a permissive `{}` schema) appears
ONLY when the agent publishes no per-mode `accepts_schema` map at
all — legacy / operator-cleared / an unpinned `dict`-input agent; a
typed `@agent.handler` always derives a model-named mode + schema
and so never lands in that fallback. An
agent whose modes are *all* `tool=False` exposes **no** LLM tools
at all — it is absent from `build_tools` output entirely (not
advertised under a permissive fallback). `tool=False` is a
tool-visibility control, not access control: a caller that names
the mode explicitly (`spawn(..., mode="…")`) can still reach it
subject to ACL — see [`../acl.md`](../acl.md) §8.

## 4. `TaskContext`

The argument every handler receives. Stable surface; new fields are
additive across SDK versions.

```python
class TaskContext:
    task_id: str
    parent_task_id: str | None
    user_id: str
    user_level: str               # admin | service | tierN — used for tool filtering
    session_id: str
    trace_id: str
    span_id: str

    # Set when the router carried an existing task_id forward — i.e.
    # this invocation is a delegation. None on a plain spawn. There
    # is no separate delegation handler; branch on this if you care.
    delegating_agent_id: str | None

    cancel_token: CancelToken     # .cancelled / .reason / .raise_if_cancelled() / await .wait()
    log: logging.Logger           # pre-bound with trace_id / task_id / agent_id
    progress: ProgressEmitter
    files: FileStash
    llm: LlmServiceClient
    peers: PeerClient

    deadline: datetime | None
    extras: dict[str, Any]        # free-form; SDK plugins stash data here

    def child_span(self, name: str) -> AbstractContextManager: ...
    def metric(self, name: str, value: float, **labels: str) -> None: ...
```

- `cancel_token` is checked by the SDK in every `await` helper it
  exposes. Handler code that does its own loops should call
  `ctx.cancel_token.raise_if_cancelled()` periodically.
- `user_level` is the principal level of the session this task was
  admitted under (`admin | service | tierN`). The SDK's tool builder
  filters outbound LLM tool schemas by `callable_user_levels`
  against this value (see `services.md` §5).
- `log` is pre-bound with `trace_id`, `task_id`, `agent_id`. Every
  log line is automatically correlated.
- `progress` (`services.md` §3) emits `Progress` frames.
- `files` is the per-task `FileStash` (`services.md` §2) — the
  handle on the router-managed named file store.
- `llm` (`services.md` §1) is the LLM service handle.
- `peers` (§7) lets a handler call other agents.
- `delegating_agent_id` is the previous executor's id when this task
  was delegated to you (vs. a fresh spawn). Delegation is not a
  separate handler/registry — the same mode handler runs; read this
  to branch (e.g. skip a greeting on a hand-off).

Construction is internal to the SDK; handlers never instantiate it.

## 5. Transport abstraction

A `Transport` is the layer between the framed protocol and the wire.
Two built-in implementations:

**`WebSocketTransport`** — for external agents. Maintains one
WebSocket to the router. On startup: dial, send `Hello`, await
`Welcome`. On shutdown: drain pending acks, send close. Reconnects
with exponential backoff (jittered) on transport errors. Resume token
(`protocol.md` §3.3) is offered automatically when reconnect occurs
within the resume window.

**`InProcessTransport`** — for embedded agents. Frames are passed via
asyncio `Queue` to the router's dispatch loop in the same process.
No serialization, no network. The router routes outbound frames to
the `InProcessTransport` of the destination embedded agent (or to
its WebSocket if external).

Both implement:

```python
class Transport(Protocol):
    async def send(self, frame: Frame) -> None: ...
    async def recv(self) -> Frame: ...
    async def close(self) -> None: ...
    def update_catalog(self, catalog: dict[str, dict[str, Any]]) -> None: ...
    @property
    def is_connected(self) -> bool: ...
```

`update_catalog` is called by the dispatcher when the router pushes
a `CatalogUpdate` frame; `WebSocketTransport` mutates the cached
`Welcome.available_destinations` in place, `InProcessTransport` is
a documented no-op.

Selection happens in `Agent.__init__` from `config.embedded`. Agent
code is unaware of which is in use.

## 6. Frame dispatch and correlation

The SDK runs three coroutines per agent:

1. **Receive loop** — `await transport.recv()`, classify by `type`,
   route to:
   - `NewTask` → handler invocation
   - `Result` for our pending peer calls → resolve correlated Future
   - `Cancel` → trigger cancel token on the matching task
   - `Progress` for our peer calls → forward to subscriber
   - `Ack` → resolve send-side Future
   - `Ping` → respond `Pong`
2. **Send queue drainer** — pulls frames from the agent's outbound
   queue, transmits, registers ack Futures with timeouts.
3. **Heartbeat** — sends `Ping` on idle, fails the socket on missed
   `Pong` (external transport only).

The SDK maintains a `_pending_acks: dict[correlation_id, Future]` and
a `_pending_results: dict[correlation_id, Future]`. The first holds
frame-level send acks; the second holds task-level outcomes for peer
calls. They are independent and time out independently.

Orphan entries (peer never replies, socket dropped before resume) are
reaped by a per-second sweep that fails expired Futures with a typed
error. Entries have per-key TTLs (`pending_acks_timeout_s`,
`pending_results_timeout_s` from `AgentConfig`). The *pending-future*
map itself is not count-capped, so a misbehaving peer that never acks
grows it until the TTL fires — bounded by `arrival_rate × TTL` (the
TTL is seconds to a minute); tighten the TTLs if it ever matters.

The companion **early-resolve buffer** (`PendingMap._buffered`, used
when a value arrives before its `register()` — e.g. the idempotent-
replay ack→register→result race) **is** hard-capped:
`BUFFER_MAX_SIZE` (default 1024) with FIFO eviction of the oldest
entry plus the same TTL sweep. A flood of unmatched early values
therefore cannot grow memory unbounded — it self-evicts. (This is
the bound the router's idempotent-replay double-deliver safety
relies on; see `protocol.md` §4.3.)

## 7. Peer calls (`ctx.peers`)

How a handler invokes another agent.

```python
class PeerClient:
    async def spawn(
        self,
        destination_agent_id: str,
        payload: BaseModel | dict[str, Any],   # dict: passed as-is (LLM-tool path)
        *,
        wait: bool = True,
        stream: bool = False,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        mode: str | None = None,
    ) -> ResultFrame | SpawnStream | str: ...   # await before `async with` when stream=True

    async def delegate(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        priority: TaskPriority = TaskPriority.NORMAL,
        mode: str | None = None,
    ) -> None: ...

    # Dispatch an LLM-emitted tool call. Resolves the build_tools
    # tool name back to (agent_id, mode); payload = tool_call.args.
    async def spawn_from_tool_call(self, tool_call, *, wait=True,
                                   stream=False, timeout_s=None,
                                   ...) -> ResultFrame | SpawnStream | str: ...

    def visible(self, *, for_user_level: str | None = None) -> dict[str, dict[str, Any]]: ...
    async def find(self, capability: str) -> list[AgentInfo]: ...
    async def describe(self, agent_id: str) -> AgentInfo: ...
    # In-handler shortcut for Agent.update_info (same kwargs).
    async def update_agent_info(self, **patch) -> None: ...
```

- `spawn` creates a child task. Three call forms (by `wait`/`stream`
  — distinct from the destination's `mode` routing key):
  - `wait=True, stream=False` (default): awaits the child `Result`.
  - `wait=True, stream=True`: returns a `SpawnStream` async-iterating
    over child `ProgressFrame`s; await `stream.result()` for the
    terminal frame.
  - `wait=False`: returns the assigned `task_id` immediately —
    fire-and-forget. The child runs detached; the parent is not
    parked or joined against it and terminates on its own handler
    return. Only `wait=True` makes the parent wait (the handler
    blocks on the child `Result`, so the parent task stays
    `RUNNING`); `WAITING_CHILDREN` is reserved and not currently
    entered by any code path.

  Files produced by the child arrive as NAMES on
  `result.output.files` (router-managed file store). A peer in the
  same `(user_id, session_id)` reaches a named file directly —
  `await ctx.files.read(name)` for the bytes, or
  `ctx.files.llm_ref(name)` to show one to an LLM (the router
  resolves it). The names round-trip inside `output`; there is no
  separate `attachments` channel.
- `delegate` hands the current task to another agent. The current
  handler should return after delegating; the SDK suppresses the
  default `Result` so the delegated agent is the one to terminate the
  task.
- `mode` (both `spawn` and `delegate`) selects which of the
  destination's registered modes the payload targets — the router
  validates against that mode's schema and the destination
  dispatches to that handler. `None` ⇒ the destination's sole mode
  (fine for a single-handler agent); for a multi-mode destination it
  is required (omitting it is rejected at admit, never silently
  mis-routed). `spawn_from_tool_call` fills it automatically from the
  per-mode tool name via `tools.resolve_tool_name`.
- **Payload size.** `spawn`/`delegate` payloads ride a single WS
  frame, capped at `WelcomeFrame.max_payload_bytes` (1 MiB default).
  `image_part()`/`document_part()` base64-inline their bytes (≈+33%)
  into that payload, so a ~750 KiB file already approaches the cap.
  An over-cap frame raises `FrameTooLargeError` synchronously from
  the `spawn`/`delegate` call (before send — it never reaches the
  router, so no socket churn). Send large media out-of-band instead:
  `name = await ctx.files.store(...)` and let the child reach it by
  name (a peer in the same user+session shares the stash). To feed a
  file to the **LLM** without inlining bytes, use
  `ctx.files.llm_ref(name)` and let the router resolve it
  (`services.md` §1.4).

  *Raising the cap.* `max_payload_bytes` is operator-tunable on the
  router. Raising it requires raising three other limits **in
  lockstep** or oversize frames will hit a lower ceiling and 1009
  the socket instead of failing gracefully: (1) the SDK's
  `AGENT_WS_MAX_RECEIVE_BYTES` to `≥ 2 × max_payload_bytes` (the 2×
  is the envelope/encoding headroom the default uses — header
  fields + JSON keys/escaping must fit alongside the payload, and
  the soft `FrameTooLargeError` cap must fire before the hard 1009);
  (2) the router-side ASGI server's `ws_max_size` (uvicorn
  `--ws-max-size` or equivalent); (3) any L7 proxy / ALB / ingress
  body-size limit in front of the router. Until those all move
  together, an over-cap router→agent frame produces a hard socket
  teardown (lost in-flight state, reconnect churn) instead of the
  typed-error path.
- `visible` returns the cached catalog from the last `Welcome` /
  `CatalogUpdate`, filtered by `callable_user_levels` against the
  active task's user level by default. Pass `for_user_level=None` to
  bypass the filter (e.g. for admin tooling).
- `find` and `describe` consult `visible()` and so inherit its
  filtering. See [`../acl.md`](../acl.md) §7 for the catalog format.
- `spawn_from_tool_call` closes the LLM-tool loop: a model emits a
  call against a `build_tools`-published tool (`call_<agent>[_<mode>]`),
  and this resolves it back to the right `(agent_id, mode)` and
  spawns with `tool_call.args` as the payload (raises `ValueError`
  if the name isn't a published `call_…` tool). `update_agent_info`
  patches your own published surface at runtime (e.g. an MCP bridge
  re-publishing a changed input schema).

**Streaming a child (the safe form).** Always use `async with await …`
— it `aclose()`s the subscription on early break/error (the bare form
leaks until the correlation timeout). `spawn` is a coroutine, so
`await` it before entering the `async with`:

```python
async with await ctx.peers.spawn(dest, payload, stream=True) as s:
    async for pf in s:            # child ProgressFrames
        ctx.progress.status(f"child:{pf.event}")
    result = await s.result()     # terminal ResultFrame
```

**Letting the model call peers (the tool loop).** `build_tools`
(`services.md` §5) and `spawn_from_tool_call` share one naming
scheme, so a model-chosen tool always round-trips to the right
agent/mode:

```python
messages = [Message(role="user", content=prompt)]
while True:
    resp = await ctx.llm.generate(messages, tools=specs)
    messages.append(Message.assistant_from_response(resp))  # round-trips signatures
    if not resp.tool_calls:
        break
    for tc in resp.tool_calls:
        child = await ctx.peers.spawn_from_tool_call(tc)
        messages.append(Message.tool_response(
            tool_call_id=tc.id, name=tc.name,
            response=(child.output.content if child.output else "")))
```

Runnable, end-to-end versions of every pattern in this doc live in
`examples/test_drive/`: **echo_agent** (handler fundamentals: modes,
typed errors, progress, cancellation, files, hooks), **caller_agent**
(discovery, streaming spawn, typed peer errors, delegation),
**gemini_agent** (the full LLM surface + the tool loop above),
**orchestration_agent** (data- vs control-plane modes).

## 8. Lifecycle

```
   process start
        │
        ├── Agent.__init__         (load AgentConfig, build transport)
        │
        ├── on_startup hooks       (user code)
        │
        ├── transport.connect      (WS dial + Hello, or in-process attach)
        │
        ├── receive / send / hb    (concurrent)
        │
        │   … steady state …
        │
        ├── shutdown signal        (SIGTERM / agent.aclose)
        │
        ├── stop accepting NewTask
        ├── drain in-flight handlers
        │   (cancel_token tripped immediately; at the grace_s
        │    deadline — default 30 — the handler task is ALSO
        │    hard-cancelled. An uncooperative handler then unwinds
        │    via asyncio.CancelledError, which still emits a
        │    terminal CANCELLED Result (status_code 499) before
        │    re-raising — the parent never hangs to correlation
        │    timeout.)
        │
        ├── on_shutdown hooks
        │
        └── transport.close
```

The SDK installs a SIGTERM/SIGINT handler that enters graceful
shutdown. Embedded agents inherit the router's lifespan and shut down
when the router does.

**Unrecoverable transport death → non-zero exit.** When the receive
loop exhausts `AgentConfig.recv_consecutive_failures_max` consecutive
failures (auth permanently rejected, a dead transport supervisor, a
decode bug) it raises `TransportPermanentlyFailed`. `Agent.run()`
maps that to a non-zero process exit (`SystemExit(1)`) **after**
running the normal teardown — so a fleet supervisor (`systemd
Restart=on-failure`, k8s, etc.) sees the failure and restarts the
agent. A bare `return` here would exit 0 and an exit-code
orchestrator would silently never restart a permanently-dead agent.
Embedded agents call `run_async()` directly and receive the raised
`TransportPermanentlyFailed` to handle within their host process
(no `SystemExit`). Shutdown also tears down the correlation reapers
and rejects any still-pending peer/LLM/ack futures, so nothing hangs
to its full correlation timeout on the way out.

## 9. Onboarding

External agents handle first-run onboarding automatically:

```python
agent.run()  # if no auth_token in AgentConfig:
             #   prompt for invitation_token via env or stdin,
             #   POST /v1/onboard, persist auth_token to disk,
             #   then proceed to connect.
```

Token persistence path is `${AGENT_STATE_DIR}/credentials.json`,
permissions `0600`. Refresh is automatic via
`POST /v1/agent/refresh-token` before expiry.

If the onboarding invitation was flagged `provisions_service_user`, the
`OnboardResponse` also carries a co-located `level=service` principal
(`security.md` §3.2). The SDK persists it into the same
`credentials.json` (merge-write — the agent-token refresh loop never
clobbers it) and exposes `config.service_user_id` /
`config.service_refresh_token`; a channel / gateway agent uses that
credential for its HTTP control-plane work. The service refresh token
rotates via `/v1/auth/refresh` like any user token — persist each
rotation with `bp_sdk.onboarding.persist_service_token`.

Embedded agents skip onboarding — the router registers them at
import time using a deployment-trusted in-process credential.

## 10. Errors

The raise-to-set-status and peer/LLM types are importable from
`bp_sdk` directly; `FrameTooLargeError` and
`TransportPermanentlyFailed` live in `bp_sdk.errors`. Three groups:
ones you **raise** to set the result status, ones you **catch** from
`ctx.peers` / `ctx.llm` calls, and one **terminal** process-level
signal.

### 10.1 Raise these to control the result status

`InputValidationError` is also raised by the SDK itself when the
inbound payload fails the handler's model **before** your code runs.
Any other exception (not below) is logged at ERROR and surfaces as
`status_code=500`; the agent loop continues.

```python
class HandlerError(Exception):        status_code = 500   # base
class InputValidationError(HandlerError): status_code = 400
class PermissionDeniedError(HandlerError): status_code = 403
class NotFoundError(HandlerError):        status_code = 404
class CancellationError(HandlerError):    status_code = 499
class UpstreamError(HandlerError):        status_code = 502
```

`CancellationError` is normally **raised into your awaits by the
SDK** when the task is cancelled (also at the shutdown grace
deadline) — let it propagate; the SDK emits the terminal CANCELLED
result. `raise UpstreamError(...)` to map a downstream failure (LLM /
peer / storage) to a clean `502` instead of an opaque `500`.

### 10.2 Catch these from `ctx.peers` / `ctx.llm`

A handler that orchestrates peers or calls the LLM should handle:

| Exception | Raised when | Typical handling |
| --- | --- | --- |
| `SpawnRejected` | Child admit refused (ACL / schema / quota / depth). | Return a graceful result or pick another peer. |
| `ResultTimeout` | Child didn't reach a terminal state within `timeout_s`. | Surface "downstream timed out". |
| `AckTimeout` | The router never acked the spawn/delegate frame. | Treat as transport flake; retry / fail soft. |
| `PeerCallError` | Generic peer failure (e.g. destination not visible). | Inspect message; fail soft. |
| `UnexpectedResponse` | Child returned a shape the caller didn't expect. | Defensive — log + fail soft. |
| `LlmCallError` | LLM provider/router call failed (after `RetryPolicy`). | `raise UpstreamError(...) from exc`. |
| `FrameTooLargeError` | `spawn`/`delegate` payload exceeds the frame cap — raised **synchronously from the call**, before send. | Move bulk to `ctx.files.store()` (see §7); for LLM media use `ctx.files.llm_ref(name)` (`services.md` §1.4). |

`FrameTooLargeError` is a `ValueError` (deliberately loud, not a
transport hiccup). The rest are SDK-specific types.

### 10.3 Terminal

`TransportPermanentlyFailed` — the recv loop exhausted
`recv_consecutive_failures_max` (unrecoverable transport). You do
**not** catch this in a handler; it escapes `run_async` and
`Agent.run()` maps it to `SystemExit(1)` so a supervisor restarts
the agent (§8). Embedded hosts receive the raised exception.
