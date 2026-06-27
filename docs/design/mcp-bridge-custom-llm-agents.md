# MCP bridge — operator-defined custom LLM agents

Follow-up to `mcp-bridge-per-server-mode-per-tool.md`. The bridge
today provisions one backplane `Agent` per MCP server, projecting an
upstream tool surface onto the backplane. This adds a *second* kind
of bridge-provisioned agent: a **custom LLM agent** — an operator
authors a system prompt, a user-prompt template, a parameter list and
a model preset in the admin UI, and the bridge stands up a single-mode
backplane `Agent` whose handler runs an LLM completion instead of
forwarding to an MCP `tools/call`.

v1 is **single-completion only** (no agent loop, no tools given to the
custom agent). The data model and handler are shaped so the loop is a
purely additive follow-up (§9), but it is explicitly out of v1 scope.

## 1. Why the bridge

The bridge's reusable value here is **not** the MCP machinery — it's
the provisioning + hosting substrate, all of which a custom LLM agent
needs verbatim:

- `Supervisor` (`bp_mcp_bridge/supervisor.py`) runs a reconcile loop
  (`poll_interval_s`, default 30s) that diffs a DB row set against an
  in-memory `_active` map and spawns / restarts / evicts one
  `asyncio.Task` per row, each hosting a live backplane `Agent`.
- `AdminClient` (`bp_mcp_bridge/admin_client.py`) holds the
  `service_mcp` refresh token, exchanges it for short-lived access
  tokens, and survives a state-dir wipe via env fallback.
- Per-row provisioning: the router mints a short-TTL
  `pending_invitation_token` at create time
  (`_mint_mcp_pending_invitation`, `bp_router/api/admin.py`); the
  bridge consumes it on the next poll to onboard the agent at
  `/v1/agent`.
- One credential file per agent under `state_dir/<agent_id>/`,
  metrics, graceful reconcile on config change.

A custom LLM agent is, structurally, *just another backplane `Agent`
connection provisioned from a DB row*. The **only** thing that differs
from `ServerBridge` is the handler body: `ctx.llm.generate(...)`
instead of `mcp_client.call_tool(...)`. Everything in the bullet list
above is reused unchanged.

### Dependency boundary (the one constraint)

The bridge depends on **`bp_sdk` + `bp_protocol` only** — verified: no
`bp_agents` import anywhere under `bp_mcp_bridge/`. The canonical agent
loop `run_llm_loop` lives in `bp_agents/common/loop.py` and pulls in
`bp_agents.common.{progress,tools}`, which would drag the whole agent
suite (and its Postgres / pyarrow deps) into the bridge container.

Every primitive the custom-agent handler needs is already in `bp_sdk`:
`ctx.llm.generate` (`bp_sdk/llm.py`), `ctx.files` /
`FileStash.store` (`bp_sdk/files.py`), and — for the v2 loop —
`ctx.peers.spawn_from_tool_call` (`bp_sdk/peers.py`),
`resolve_tool_name` (`bp_sdk/tools.py`), `bp_sdk.file_tools`. So the
bridge stays `bp_agents`-free; v1 needs nothing beyond `ctx.llm`, and
v2 reimplements a ~60-line loop on SDK primitives rather than importing
`run_llm_loop`.

## 2. Goals / non-goals

**Goals (v1)**

- One backplane `Agent` per custom-agent row, `agent_id = custom_<id>`,
  hosted by the same supervisor that hosts MCP servers.
- Operator authors, per row: model preset, groups, capabilities,
  string parameters, system prompt, user-prompt template, and an
  "output as file" toggle.
- Single mode. The parameter list **is** the mode's `accepts_schema`
  (an object of `string` properties); the LLM caller fills it.
- Handler = one `ctx.llm.generate(preset=…, prompt=[system, user])`
  call, prompts rendered by substituting validated params into the
  templates.
- Clean reuse of the existing invitation / reconcile / credential /
  metrics path — no new hosting code.

**Non-goals (v1)**

- The agent loop and any tools given to the custom agent (file access,
  peer-agent calls). Designed for in §9, deferred.
- Non-string parameters. Strings only, by request — keeps the
  param→schema mapping and prompt templating trivial and injection-safe.
- Per-row provider/sampling knobs beyond what the chosen preset
  carries. Sampling lives in the preset; the custom agent picks a
  preset by name.
- Sharing `run_llm_loop` with `bp_agents` (would breach §1's boundary).

## 3. Data model — the `custom_agents` table

Parallels `mcp_servers` (`bp_router/db/models.py`,
`bp_router/db/queries.py`). One row per custom agent. v1 columns:

```sql
CREATE TABLE custom_agents (
    agent_id      text PRIMARY KEY
                  CHECK (agent_id ~ '^custom_[a-z][a-z0-9_]*$'),
    description   text NOT NULL DEFAULT '',
    preset_name   text NOT NULL REFERENCES llm_presets(name),
    system_prompt text NOT NULL DEFAULT '',
    user_prompt   text NOT NULL DEFAULT '',
    -- ordered list of {name, description, required:bool}; name matches
    -- ^[a-z][a-z0-9_]*$ so it is a safe $-template key and JSON-schema
    -- property. ALL params are type "string" (v1 non-goal).
    parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
    groups        jsonb NOT NULL DEFAULT '[]'::jsonb,
    capabilities  jsonb NOT NULL DEFAULT '[]'::jsonb,
    expose_to_llm boolean NOT NULL DEFAULT true,
    output_as_file boolean NOT NULL DEFAULT false,
    enabled       boolean NOT NULL DEFAULT true,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text REFERENCES users(user_id),

    -- provisioning, mirrors mcp_servers exactly
    pending_invitation_token      text,
    pending_invitation_expires_at timestamptz
);
```

Notes:

- `agent_id` stores the **full** `custom_<id>` string; the admin form
  takes the bare `<id>` and prefixes, mirroring how MCP agents are
  `mcp_<server_id>`. The `custom_` prefix is the namespace the request
  asked for and the ACL handle (§7).
- `preset_name` is a real FK. A delete/rename of a referenced preset is
  blocked by the FK — surfaced as a clear admin error rather than a
  runtime "unknown preset" at dispatch.
- `parameters` is operator-ordered (affects only display); names are
  grammar-checked at write time so they are safe template keys.
- The v2 loop columns (`agent_loop_enabled`, `max_rounds`,
  `file_access`, `peer_tools_enabled`) are **not** in the v1 migration;
  §9 lists them as the additive follow-up migration.

Mirror the `mcp_servers` plumbing: `CustomAgentRow` /
`CustomAgentView` (the view masks nothing — no secrets here) in
`bp_router/db/models.py`; `insert/get/list/update/delete_custom_agent`
in `bp_router/db/queries.py`; a `_CUSTOM_AGENT_SELECT_COLS` constant.

## 4. Provisioning flow

Additive to the supervisor — it gains a second desired-state source and
a second bridge-task type:

1. **Admin create** → `POST /v1/admin/custom-agents` validates
   (`agent_id` grammar, preset FK, groups/capabilities grammar, param
   names, template placeholders ⊆ declared params — §6), inserts the
   row, and **immediately mints a pending invitation** via the same
   helper shape as `_mint_mcp_pending_invitation` (service-level,
   ~600s TTL).
2. **Supervisor poll** also calls
   `admin_client.list_custom_agents()` (`GET /v1/admin/custom-agents`).
   Diff against `_active`; new `agent_id` → `_start` a
   `CustomAgentBridge` task; removed → cancel; `config_signature()`
   changed → restart. Identical control flow to MCP servers.
3. **`CustomAgentBridge`** (new, sibling of `ServerBridge`, but far
   smaller — no MCP client, no `tools/list`, no `tools/list_changed`):
   builds the `Agent` (§5), reads `pending_invitation_token`, onboards
   at `/v1/agent`, and **stays connected**. There is no upstream to
   reconcile against; the only thing that restarts it is a config
   change picked up by the supervisor diff.

`config_signature()` for a custom agent hashes the prompt/param/preset/
groups/capabilities/flags so an admin edit triggers a clean restart
(the agent re-onboards with the new `AgentInfo` + handler). This reuses
the existing supervisor restart-on-signature-change branch.

## 5. The custom-agent `Agent`

Single mode. The parameter list becomes the mode's `accepts_schema`:

```python
MODE = "main"  # single mode; never appears in the external tool name

def _accepts_schema(params: list[Param]) -> dict:
    return {MODE: {
        "type": "object",
        "properties": {
            p.name: {"type": "string", "description": p.description}
            for p in params
        },
        "required": [p.name for p in params if p.required],
        "additionalProperties": False,
    }}

agent = Agent(info=AgentInfo(
    agent_id=row.agent_id,                  # custom_<id>
    description=row.description,
    groups=list(row.groups),
    capabilities=["custom.agent", *row.capabilities],
    accepts_schema=_accepts_schema(params),
    hidden=not row.expose_to_llm,
))

@agent.handler(mode=MODE)
async def run(ctx: TaskContext, payload: dict) -> AgentOutput:
    sys_text  = _render(row.system_prompt, payload)   # §6
    user_text = _render(row.user_prompt, payload)
    resp = await ctx.llm.generate(
        prompt=[Message(role="system", content=sys_text),
                Message(role="user",   content=user_text)],
        preset=row.preset_name,
    )
    return _to_output(ctx, resp, output_as_file=row.output_as_file)  # §8
```

Because there is exactly one mode, the SDK's `_tool_specs`
(`bp_sdk/tools.py`) emits the bare-name tool `call_<agent_id>` =
`call_custom_<id>` — the mode label `"main"` never leaks into the
external name. `accepts_schema` is operator-pinned at construction the
same way MCP per-tool schemas are, so `_republish_schemas` will not
wipe it. The router admit-validates the caller's `payload` against this
schema before the handler runs, so `_render` always sees a dict with
the declared keys.

`ctx.llm` is initialised by the SDK dispatcher on every task regardless
of which process hosts the agent (`bp_sdk/context.py`), so the bridge
process gets a working `LlmServiceClient` even though today's MCP
agents never touch it. *Verify this end-to-end early* (§10 / open
questions) — it is the one assumption v1 leans on that the bridge has
never exercised.

## 6. Prompt templating & param→schema

Parameters are **strings only** (v1 non-goal forbids other types),
which makes both directions trivial and safe:

- **Forward (schema):** each param → one `"string"` property (§5).
- **Render:** substitute validated param values into the prompt
  templates using `string.Template(tpl).safe_substitute(payload)` —
  `$name` placeholders, **not** `str.format`. `str.format` is unsafe
  here: a param *value* containing `{}` would raise, and a *template*
  author could reach `{x.__globals__}`-style attributes. `$`-templating
  substitutes only declared keys and treats values as inert data.

Validation at write time (admin API):

- Every `$name` placeholder in `system_prompt`/`user_prompt` must be a
  declared param name → otherwise `safe_substitute` would silently
  leave a literal `$undeclared` in the prompt. Reject at create/edit.
- Param names unique and matching `^[a-z][a-z0-9_]*$`.

Note the obvious: param values are caller-supplied and land in the
prompt as data — this is ordinary prompt content, not a new injection
surface, but the system prompt should be authored to treat the
user-prompt section as untrusted. No code mitigation beyond that in v1.

## 7. ACL / capabilities / groups

The agent row carries operator-set `groups` + `capabilities`, plus a
fixed `custom.agent` capability marker (the coarse "this is a custom
agent" handle, paralleling `mcp.bridge`). These flow through the
existing agents-table ACL surface unchanged:

- **Who may call the custom agent**: gated by ACL rules over the
  agent's groups / capabilities / `@custom_<id>`. A starter rule
  (e.g. `allow * → custom/*` for a `custom` group, or
  `@custom_*`) makes the new agents reachable, mirroring how MCP
  agents become callable. Capability/group grammars per
  `bp_router/acl.py` (`^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$` for caps).
- **What the custom agent may call**: in v1, *nothing* — it is given no
  peer tools. This becomes load-bearing in v2 (§9): the custom agent's
  own `groups`/`capabilities` gate which peers its loop can reach,
  which is the containment story for "this is a configurable autonomous
  agent."

## 8. Output as file

When `output_as_file` is set, the handler stores the completion text in
the router-managed file store instead of returning it inline:

```python
def _to_output(ctx, resp, *, output_as_file):
    if not output_as_file:
        return AgentOutput(content=resp.text)
    ref = ctx.files.store(resp.text.encode(), name="output.md",
                          content_type="text/markdown")  # bp_sdk/files.py
    return AgentOutput(
        content=f"Output written to file: {ref.name}",
        files=[ref],
    )
```

The caller (an LLM parent) receives a short note plus a `file_ref` it
can read on demand via `Message.tool_response_from_result`, which
auto-threads child-produced files (`bp_sdk/llm.py`). This is the lever
for keeping a large generated artifact out of the parent's context
window — the whole point of the toggle. Exact `FileStash.store`
signature is an implementation detail to confirm against
`bp_sdk/files.py`.

## 9. v2 — the agent loop (designed, deferred)

Additive. New nullable/default-off columns (additive migration):

```sql
ALTER TABLE custom_agents
  ADD COLUMN agent_loop_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN max_rounds         integer NOT NULL DEFAULT 4
             CHECK (max_rounds BETWEEN 1 AND 16),
  ADD COLUMN file_access        text NOT NULL DEFAULT 'none'
             CHECK (file_access IN ('none','read_only','full')),
  ADD COLUMN peer_tools_enabled boolean NOT NULL DEFAULT false;
```

When `agent_loop_enabled`, the handler runs a **minimal in-bridge
loop** built on SDK primitives only (no `bp_agents` import):

```
messages = [system, user]
tools  = (file_access != 'none' ? sdk_file_tools.file_tools(file_access) : [])
       + (peer_tools_enabled    ? build_tools(ctx.peers.visible(), provider) : [])
for _ in range(max_rounds):
    resp = await ctx.llm.generate(prompt=messages, preset=…, tools=tools)
    messages.append(Message.assistant_from_response(resp))
    if not resp.tool_calls: break
    for tc in resp.tool_calls:
        if is_file_tool(tc):  result = sdk_file_tools.dispatch_file_tool(ctx.files, tc)
        else:                 result = await ctx.peers.spawn_from_tool_call(tc)
        messages.append(<tool response>)
return _to_output(ctx, resp, output_as_file=…)
```

This duplicates a trimmed slice of `run_llm_loop`'s logic
(`bp_agents/common/loop.py:392-537`) deliberately — reuse would breach
§1's dependency boundary. The pieces it leans on
(`assistant_from_response`, `spawn_from_tool_call`,
`sdk_file_tools.{file_tools,dispatch_file_tool}`, `build_tools`) are
all already in `bp_sdk`. If a third consumer ever wants this, promote
the minimal loop into `bp_sdk` rather than importing the suite version.

Containment: file `full` access + `peer_tools_enabled` makes the row a
configurable autonomous agent. Operator-only authoring + the agent's
own ACL groups/caps (which gate what its loop can reach, §7) are the
guardrails; `max_rounds` bounds cost. No new mechanism needed.

## 10. Implementation sequence (v1)

1. **DB**: migration for `custom_agents` (§3); `CustomAgentRow` /
   `CustomAgentView`; `insert/get/list/update/delete_custom_agent` +
   `_CUSTOM_AGENT_SELECT_COLS`.
2. **Router admin API**: `POST/GET/PATCH/DELETE /v1/admin/custom-agents`
   with validators (grammar, preset FK, placeholder ⊆ params), and
   `_mint_custom_agent_pending_invitation` reusing the MCP invitation
   helper. Audit events.
3. **Bridge**: `admin_client.list_custom_agents()`; `CustomAgentBridge`
   (sibling of `ServerBridge`); supervisor poll/diff/reconcile for the
   second source; `build_custom_agent` + the single-mode handler (§5),
   `_render` (§6), `_to_output` (§8). No MCP code touched.
4. **Verify `ctx.llm` in the bridge process** end-to-end before
   building the admin UI (§5 assumption).
5. **Admin UI**: `bp_admin/pages/custom_agents.py` + templates
   (`list.html`, `form.html`, `detail.html`) mirroring
   `mcp_servers` / `llm_presets`. Preset picker = dropdown of existing
   presets; dynamic param rows; system/user prompt textareas;
   output-as-file checkbox.
6. **ACL**: ship a starter rule making `custom_*` reachable (§7), or
   document the group convention operators add.
7. **Tests**: param→schema, `$`-template render (incl. missing/extra
   placeholder rejection), preset FK error, provisioning round-trip
   (create → poll → onboard → callable), output-as-file ref threading.
8. **Docs**: note the second bridge-agent kind near the MCP bridge docs;
   admin-ui doc for the new resource.

## 11. What not to do

- **Don't import `run_llm_loop` / anything `bp_agents` into the
  bridge.** It's the whole reason the boundary is called out in §1;
  reuse here trades a clean ~60-line v2 loop for the entire agent suite
  in the bridge image.
- **Don't use `str.format` for prompt rendering.** `$`-templating with
  `safe_substitute` only (§6) — format-string injection and `{}`-in-
  value crashes are real.
- **Don't add non-string params "while we're here".** The string-only
  constraint is what keeps schema + templating trivial; revisit only
  with a concrete need.
- **Don't ship the v2 loop columns in the v1 migration.** Keep v1's
  surface to single-completion; the loop migration (§9) is additive and
  lands with the feature that uses it.
- **Don't give the v1 agent any tools.** No file/peer tools until v2 —
  v1 is a pure completion, which is also what makes its blast radius
  small enough to ship first.

## 12. Open questions

- **`ctx.llm` in the bridge process.** MCP bridge agents have never
  called the LLM service; confirm the dispatcher wires a working
  `LlmServiceClient` for a bridge-hosted agent (it should, per
  `bp_sdk/context.py`, but it's the one untested assumption). Pin test
  in step 4.
- **Preset tier gate vs caller level.** A preset's `min_user_level`
  (`bp_router/llm/presets.py`) is checked against the *calling* user's
  level at `generate` time, not the operator's. A custom agent on an
  admin-gated preset silently fails for lower-tier callers. Surface
  the preset's `min_user_level` in the admin form as a warning; full
  per-caller modelling is out of scope.
- **Naming.** `bp_mcp_bridge` hosting non-MCP agents is a slight
  misnomer. Rename to `bp_agent_bridge` is tempting but a large,
  orthogonal churn (package path, container, env vars, deploy). Defer;
  note it.
- **Quota / cost attribution.** Custom-agent LLM spend rides the
  calling user's quota (it's their task). Confirm that's the desired
  attribution vs. charging the operator who authored the agent.
