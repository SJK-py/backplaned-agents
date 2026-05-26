# MCP bridge — one Agent per server, one mode per tool

Follow-up to the #235 unified mode-dispatch rework. The MCP bridge
predates modes — it builds **one backplane `Agent` per MCP tool**.
With per-mode `accepts_schema` we can collapse that to **one Agent
per MCP server, one mode per tool**, with no change to externally-
visible LLM tool names.

## 1. The gap today

`bp_mcp_bridge/tool_agent.py` builds **one `Agent` per MCP tool**
(`agent_id = mcp_<server_id>_<tool_name>`). `tool_agent.py:204-217`
pins `accepts_schema = {"call": tool.input_schema}` (a single-mode
map, post-#235) and registers a single `@agent.handler(mode="call")`
that forwards the payload to `mcp_client.call_tool(tool.name,
payload)`. `ServerBridge` (`server_bridge.py:412-432`) spawns one
`asyncio.Task` per tool running its own `agent.run_async()`, tracks
them in `self._running_tools[tool.name]`, and reconciles on
`tools/list_changed` by add / evict / `_update_tool_schema` per
tool.

That layout was correct for the pre-mode SDK (a single
`accepts_schema` per agent), but with #235 + the post-merge
mode-per-tool naming convention `call_<agent>_<mode>`, **every MCP
tool now has a dedicated agent ONLY because the SDK once required
it**. Costs:

- **N agents per server** in the catalog, in the ACL surface, and
  as WS sockets. A server with 30 tools = 30 catalog entries, 30
  invitation tokens, 30 onboardings, 30 credential files, 30 WS
  connections, 30 task lifecycles.
- **N restarts on a schema change** in the worst path
  (`_update_tool_schema` falls back to `_evict_tool` + `_spawn_tool`
  if the in-place update fails — `server_bridge.py:480-481`).
- **Tool-name → agent-id collision risk**: long MCP tool names
  trigger the `agent_id_for` hash-fallback truncation
  (`tool_agent.py:165-182`); ACL rules referencing `@mcp_<server>_
  <tool>` need the hash form, which is opaque and brittle.
- **External naming is already mode-per-tool by accident**:
  `_tool_specs` (post-#235) emits the single-mode bare name
  `call_mcp_<server>_<tool>` for each per-tool agent. Under
  per-server-with-modes that becomes the multi-mode shape
  `call_<agent>_<mode>` = `call_mcp_<server>_<tool>` — identical
  string. Migration preserves every LLM-callable name.

## 2. Goals / non-goals

**Goals**

- One backplane `Agent` per MCP server. One mode per MCP tool.
- `accepts_schema = {tool_name: tool.input_schema, ...}` — full MCP
  schema preserved per mode, validated at the router boundary
  (today's per-tool `accepts_schema={"call": ...}` operator-pin
  generalised).
- **Stable externally-visible LLM tool names**: a model that called
  `call_mcp_<server>_<tool>` keeps calling exactly that string.
- Live add / remove / update of modes on `tools/list_changed`
  without dropping the underlying socket / re-onboarding.
- One invitation, one credential file, one task lifecycle per server
  instead of per tool.

**Non-goals**

- Changing the MCP-client transport / retry / metrics framing.
  Orthogonal.
- Per-tool LLM-visibility (`tool=False` per MCP tool) — *possible*
  follow-up via `non_tool_modes`, but not P1.
- MCP `resources` / `prompts` — separate features, separate
  channel.

## 3. Design — the per-server Agent

One `Agent` per server, constructed from the full tool list at
startup and reshaped at every `tools/list_changed`:

```python
agent = Agent(info=AgentInfo(
    agent_id=f"mcp_{server_id}",
    description=f"MCP server {server_id} ({transport}).",
    groups=list(config.groups),
    capabilities=[
        "mcp.bridge",
        *[f"mcp.tool.{_capability_segment(t.name)}" for t in tools],
    ],
    # Operator-pinned (full per-tool schemas; ALSO updated by
    # `update_info` on tools/list_changed).
    accepts_schema={t.name: t.input_schema for t in tools},
    hidden=not config.expose_to_llm,
))

# One handler per tool, sharing one mcp_client. Closures capture
# tool.name so the dispatch is O(1) by mode.
for t in tools:
    @agent.handler(mode=t.name)
    async def call(ctx, payload: dict, _name=t.name) -> AgentOutput:
        return await _forward(ctx, _name, payload)
```

The handler body is what `tool_agent.py:227-240` already does —
unchanged retry / metrics / `[MCP tool error]` prefix logic, just
keyed by the closure-captured `_name` instead of the
single-tool-agent's identity.

Mode names = MCP tool names verbatim (no normalisation — SDK accepts
any non-empty string; only the *capability* segment needs the
lowercase-underscore grammar, which `_capability_segment` already
handles). External LLM tool names land as `call_mcp_<server>_
<tool>` via `_tool_specs`' multi-mode rule — identical to today's
single-mode bare-name form on the old per-tool agents.

## 4. SDK addition — `Agent.set_modes(...)` for runtime dynamism

The headline reason the bridge is the *only* runtime-mode-mutating
consumer in the codebase. `agent.handler(...)` works post-init via a
plain dict-mutation + `_republish_schemas`, but it has two
shortcomings for the dynamic case:

1. **It only adds.** No paired remove — needed for a tool that
   disappears from `tools/list`.
2. **It doesn't push the `accepts_schema` change to the router.**
   `_republish_schemas` updates `self.info.accepts_schema` in
   memory, but the bridge's operator-pin gate currently skips it
   (the existing `_update_tool_schema` path explicitly calls
   `agent.update_info(...)`).

Add **one** small public method (no other change to the registration
surface):

```python
async def set_modes(
    self,
    modes: Mapping[str, tuple[HandlerFn, dict[str, Any] | None]],
    *,
    non_tool_modes: list[str] | None = None,
) -> None:
    """Atomically REPLACE the agent's mode set + accepts_schema, and
    push the change to the router via AgentInfoUpdate. For runtime
    consumers (the MCP bridge on tools/list_changed); typical
    authors use `@agent.handler` at module load."""
```

`modes[name] = (handler_fn, accepts_schema_or_None)`. The method:
- replaces `self._handlers_by_mode` wholesale,
- sets `self.info.accepts_schema = {name: schema}`,
- sets `self.info.non_tool_modes = non_tool_modes or []`,
- if the agent is connected, calls `update_info(accepts_schema=…,
  non_tool_modes=…)` to broadcast.

Bridge uses **only** `set_modes` for mode mutations; existing
`@agent.handler` stays the typical-author surface. The bridge thus
never reaches into `_handlers_by_mode` directly, and the registration
contract stays single-source-of-truth.

## 5. Reconcile flow under `tools/list_changed`

`ServerBridge` becomes radically simpler:

```python
# was: per-tool spawn/evict/_update_tool_schema diff loop
async def _reconcile(self) -> None:
    new_tools = await self._mcp_client.list_tools()
    await self._agent.set_modes({
        t.name: (_make_handler(self._mcp_client, t.name), t.input_schema)
        for t in new_tools
    })
    self._known_tools = new_tools
```

One call replaces N spawn/evict/update branches. The Agent's task
runs continuously (one `run_async` for the server's lifetime). On
SSE-supported clients the `on_tools_changed` callback drops straight
into `_reconcile`; on poll-based the periodic poll calls the same
function.

A schema-only change is just a new map entry; an add is a new key;
a remove is an absent key. The SDK's `set_modes` raises the right
typed error if a mode collides; the bridge never carries the
"recovery via evict+respawn" branch
(`server_bridge.py:480-481`) — there's nothing to respawn.

`_running_tools`, `_RunningTool`, per-tool `task` tracking,
`_evict_tool`, `_spawn_tool`, `_update_tool_schema`,
`_issue_invitation_if_needed(tool)` → all collapse to a single
per-server agent task plus the `_reconcile` above.

## 6. External naming

The LLM tool name resolves cleanly via the SDK's multi-mode rule:
per-server agent `mcp_<server>`, mode = tool name →
`call_<agent>_<mode>` = `call_mcp_<server>_<tool>`. Same
`resolve_tool_name` → `(agent_id, mode)` end-state at the router.

There is no hash-fallback truncation case: the agent_id is just
`mcp_<server>` (well under 64 chars), and the tool name lives in
the mode portion which has no grammar limit. Long MCP tool names
with weird characters surface in the generated tool string scrubbed
by `_safe_tool_name` (existing `tools.py` behaviour).

## 7. ACL / capabilities

ACL on the bridge becomes per-server. The two pinning surfaces that
matter both line up cleanly with the new shape:

- **Capabilities** are aggregated: the per-server agent carries
  `["mcp.bridge", *[f"mcp.tool.{seg(t)}" for t in tools]]`. Capability-
  pattern ACL rules (`mcp.tool.*`, `mcp.tool.search_*`) keep
  working unchanged — per-tool granularity without per-tool agents.
- **Per-server `groups`** = the operator's `config.groups`, so ACL
  group rules continue to apply to the whole bridged server.
- **`callable_user_levels`** is a per-agent field, which under the
  new shape means per-server. One tier setting covers all the
  server's tools — already how the bridge config worked end-to-end;
  tier-per-tool was never wired and stays out of scope.

Whole-server agent-id rules target `@mcp_<server>` (= the new
agent_id). Per-tool scoping uses the capability patterns above.
Per-tool agent-id rules don't exist in this design — that's the
intended consequence of having one agent per server.

## 8. Onboarding & state

- One invitation token per server. `_issue_invitation_if_needed`
  runs once per ServerBridge instance (idempotent across
  reconciles).
- One persisted credential file per server: `state_dir/mcp_<server>/
  credentials.json`.
- One WS socket per server, not per tool. N-fold reduction in
  router-side socket / outbox / heartbeat overhead for servers
  with many tools.

## 9. Implementation sequence

1. **SDK:** add `Agent.set_modes(...)` (§4); tests:
   replace-then-update_info propagation, atomic replace, removed
   mode no longer dispatches.
2. **Bridge — `tool_agent.py` rewrite**: `build_server_agent` builds
   the per-server `Agent` from the initial tool list;
   `make_tool_handler` factors out the per-tool closure
   (retry + metrics + `[MCP tool error]` marker — unchanged).
3. **Bridge — `server_bridge.py` rewrite**: `_reconcile_tools` calls
   `_apply_tools` which runs `await self._agent.set_modes(...)` plus
   a conditional `update_info(capabilities=…, description=…)` when
   the tool-*name* set changes. `_running_tools`, `_spawn_tool`,
   `_evict_tool`, `_update_tool_schema`, `_schema_hash`,
   `_initial_spawn`, `_tear_down_all_tools`, per-tool invitation
   issuance — all deleted.
4. **Bridge — onboarding/state**: one invitation per server; one
   `state_dir/mcp_<server>/` per server.
5. **Docs:** update `docs/router/protocol.md` (no wire change but
   the agent-per-server pattern bears mention near
   `AgentInfo.accepts_schema`); module docstrings for the new
   layout.
6. **Tests:** bridge-side — initial registration, tools/list_changed
   triggers `set_modes`, add/remove/update tool flows, retry/error
   semantics unchanged per-tool.

## 10. What not to do

- **Don't keep a per-tool agent_id alias** — preserving the dead
  agent_id space (e.g. as redirects) doubles the catalog and the
  ACL surface for no readers; the LLM tool names stay stable
  without it.
- **Don't reach into `agent._handlers_by_mode`** from the bridge.
  Use `set_modes` (§4); the contract that handler registration
  lives behind a public API and `_republish_schemas`/`update_info`
  fire consistently is exactly what was broken when each runtime
  consumer poked internals directly.
- **Don't model tool=False per MCP tool yet.** Out of scope; a
  later operator-config hook (per-tool LLM visibility on the
  `mcp_servers` row) flows through trivially to `non_tool_modes`.

## 11. Open questions

- `agent.set_modes` atomicity vs an in-flight dispatch: a tools/
  list_changed during a tool call. The currently-running handler
  holds its closure; replacing `_handlers_by_mode` doesn't affect
  it. A NEW frame for a now-removed mode lands as `no_handler` —
  correct behaviour, matching today's "tool went away" semantics.
  Worth a pin test.
- Per-tool tier gating: out of scope today (§7), but if it ever
  comes back, `min_user_level` would need to be per-mode (an
  `AgentInfo` shape change beyond this design). Note for a
  potential future RFC.
