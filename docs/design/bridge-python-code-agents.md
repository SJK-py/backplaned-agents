# Agent bridge — operator-authored Python code agents

> **Status: implemented (v1).** A third bridge-provisioned agent kind,
> alongside MCP servers ([`mcp-bridge-per-server-mode-per-tool.md`](./mcp-bridge-per-server-mode-per-tool.md))
> and custom LLM agents ([`mcp-bridge-custom-llm-agents.md`](./mcp-bridge-custom-llm-agents.md)).
> An operator authors a Python function in the admin UI; the bridge stands
> up one backplane `Agent` whose handler runs that function in a
> uid-dropped subprocess. v1 is deliberately narrow — see §2. Migration
> `0013_code_agents`; runtime in `bp_mcp_bridge/code_runner.py` +
> `code_agent.py` + `code_agent_bridge.py`; UI at `/admin/code-agents`.
> Nine `[shipped]` deviations in §15.

## 1. Why

The custom LLM agent covers "prompt a model with these parameters". It
cannot do the other half of what operators want from a configurable
agent: **call something that isn't a model**. A weather API, an internal
REST service, a signed S3 request, a CSV transform, a rate calculation —
none of these are MCP servers anyone will write, and none of them are
prompts.

Today the only ways to add such a capability are to write a full suite
agent (a Python package, an invitation, a compose service, a deploy) or
to stand up an MCP server just to wrap one HTTP call. Both are far more
ceremony than the task deserves. A code agent is the missing rung: **a
single function, authored in the admin UI, that becomes a callable
backplane agent** — the same distance from idea to running agent that a
custom LLM agent already offers, for the non-LLM half of the problem.

### 1.1 Why the bridge, again

The same argument as §1 of the custom-LLM-agent doc, and it has since
been proven twice: the bridge's reusable value is the **provisioning and
hosting substrate**, not the MCP machinery.

- `Supervisor` reconciles DB rows against running `asyncio.Task`s,
  spawning / restarting / evicting one bridge per row.
- `AdminClient` holds the `service_mcp` refresh token and survives a
  state-dir wipe.
- Per-row invitation minting → the bridge consumes it on the next poll →
  one `credentials.json` per agent under `state_dir/<agent_id>/`.
- `config_signature()`-triggered restart on an admin edit.

A code agent is *just another backplane `Agent` provisioned from a DB
row*. What differs is the handler body — and, this time, **where the
handler body runs** (§3).

### 1.2 What the bridge already has that we need

Unlike the LLM kind, this one needs process hardening, and the bridge
already implements it for stdio MCP servers. All of it transfers:

| piece | where | what it does |
| --- | --- | --- |
| `_stdio_preexec` | `mcp_client.py:815` | rlimits, `PR_SET_NO_NEW_PRIVS`, `setgroups([])` → `setgid` → `setuid` in the forked child |
| `StdioSpawnConfig` | `mcp_client.py:795` | the child's *entire* env, cwd, uid, rlimit set |
| `StdioPolicy` | `config.py:99` | uid range, work root, launcher allowlist |
| `_stdio_uid` / `_build_stdio_spawn` | `server_bridge.py:227-286` | deterministic per-row uid, chowned work dir, scoped env, the userns EINVAL diagnostic |
| container capabilities | `docker-compose.prod.yml:552` | root + `cap_drop: ALL` + `SETUID,SETGID,CHOWN,KILL` + `no-new-privileges` |

The dependency boundary from §1 of the LLM doc holds unchanged: the
bridge imports **`bp_sdk` + `bp_protocol` only**. Nothing here needs
`bp_agents`.

## 2. Goals / non-goals

**Goals (v1)**

- One backplane `Agent` per `code_agents` row, `agent_id = code_<slug>`,
  hosted by the existing supervisor.
- Operator authors, per row: the function source, an entrypoint name,
  a **typed** parameter list, secret references, a timeout, and the same
  `groups` / `capabilities` / `expose_to_llm` / `output_as_file` surface
  the LLM kind has.
- Single mode. The parameter list **is** the mode's `accepts_schema`;
  the router admit-validates the caller's payload against it before the
  handler runs.
- The function runs in a **uid-dropped, rlimited, scoped-env subprocess**
  with a per-call working directory, killed on timeout.
- The function may reach the network (§3.4) and receives its declared
  secrets in its environment.

**Non-goals (v1)** — each has a reason, not just a schedule:

- **No backplane handles inside the code.** No `ctx.llm`, no
  `ctx.files`, no `ctx.peers`, no `ctx.history`. Giving the child those
  means inventing a child→parent RPC protocol; that is v2 (§12), and v1
  is genuinely useful without it. A code agent that needs a model calls
  *nothing* — the orchestrator that called it already has one.
- **No per-agent dependency installation.** The function gets what is in
  the suite image. Per-agent `pip install` is a supply-chain hole and a
  cold-start problem; §12 sketches the pre-baked-venv shape if it is ever
  wanted.
- **No per-agent network egress control.** Not deferred out of
  laziness — it is *not achievable* with the bridge's capability set
  (§3.4). Saying so plainly beats shipping a `network: none` column that
  enforces nothing.
- **No persistent worker process.** One subprocess per call. A warm
  worker would leak state between users, which is disqualifying for
  something operators will use to hold API sessions.
- **No sharing the `custom_agents` table.** §4.1.

## 3. Where the code runs — the decision everything hangs on

### 3.1 Not in the bridge process

The bridge process holds:

- `BP_MCP_BRIDGE_SERVICE_SECRET` — the `service_mcp` refresh token, which
  mints invitations for **any** bridged agent;
- every bridged agent's `credentials.json` under `/mcp-state`;
- every resolved `env://` secret for every MCP server.

So `exec()`-ing operator code in-process is out. Not because operators
are hostile — because one `open('/mcp-state/mcp_github/credentials.json')`
in a debugging session is full impersonation of another agent, and
nothing in an in-process design prevents it. The blast radius of a typo
must not be "the whole bridge's credential set".

### 3.2 In a uid-dropped subprocess — the stdio pattern, per call

Reuse §1.2's machinery verbatim, with one change: the work dir is
**per call**, not per row, so two concurrent calls to the same agent
cannot see each other's scratch files.

```
parent (bridge)                         child (operator code)
──────────────────────────────────────  ─────────────────────────────
mkdtemp under work_root/<agent_id>/
chown → agent uid
write harness.py + agent_main.py
spawn  python -I -S harness.py ───────▶ import agent_main
  env  = scoped (§9)                    read one JSON line on stdin
  uid  = per-agent, deterministic        result = run(params, context)
  preexec = rlimits+no_new_privs+drop    write one JSON line on saved fd 1
  start_new_session=True                 exit
wait(timeout_s) ──── on timeout ──────▶ killpg(SIGKILL)
read result / stderr
rm -rf the temp dir  (finally)
```

Per-call spawn cost is ~30–60 ms for a bare interpreter (`-I -S` skips
site and user site-packages). That is the price of not keeping a warm
worker, and it is the right trade: a warm worker holding one tenant's
state and then serving another is exactly the bug class this feature
would otherwise introduce.

`start_new_session=True` puts the child in its own process group so the
timeout can `killpg` — a bare `kill(pid)` leaves anything the operator's
code forked (a subprocess, a thread pool's children) running.

### 3.3 The uid is per agent, not per call

Deterministic from `agent_id` within the policy range, exactly as
`_stdio_uid` derives one from `server_id`. Per-call uids would need an
allocator and a reaper for no isolation gain: the per-call temp dir
already separates concurrent calls, and two calls to the *same* agent are
the same trust domain by construction.

Two consequences worth stating: **code agents share the uid range with
stdio MCP servers**, so `BP_MCP_BRIDGE_UID_BASE/_MAX` must be wide
enough for both (a 10 000-wide default range is ample); and a uid
collision between a code agent and an MCP server is possible but
harmless — they get the same OS identity, not each other's files, since
the work dirs differ.

### 3.4 Network: what is enforced, and what is not

**The bridge already has unrestricted outbound internet.** No network in
`docker-compose.prod.yml` is `internal: true`, so every container on the
`agents` network reaches the default gateway. (The comment at
`docker-compose.prod.yml:610-613` implying stdio servers need an egress
path added is wrong and should be corrected — see §14.)

That is the feature: a code agent can call an outside API with no new
plumbing. It is also the containment story's weak point, and **v1 does
not fix it**. Isolating a child into its own network namespace requires
`CAP_NET_ADMIN`, which the bridge deliberately does not have and should
not get — adding it to a container that runs operator code trades a
larger capability for a smaller one.

So v1's honest position: **a code agent inherits the bridge's egress.**
The controls that actually exist are admin-only authoring, the audited
code body (§10), and the uid drop that stops it reaching other agents'
credentials. If per-agent egress policy is needed, the answer is a
separate `code_worker` container with its own compose network — §12.

### 3.5 What the subprocess buys, stated precisely

It is worth being exact, because "sandboxed" is a word that invites
over-reading:

| threat | mitigated? | by what |
| --- | --- | --- |
| read another agent's `credentials.json` | **yes** | uid drop; `/mcp-state` is root-owned 0700 |
| read the bridge's service secret | **yes** | scoped env (§9) — the child's environment is constructed, not inherited |
| fork bomb / memory balloon / disk fill | **yes** | `RLIMIT_NPROC` / `RLIMIT_AS` / `RLIMIT_FSIZE` in `preexec` |
| never return | **yes** | wall-clock timeout → `killpg` |
| regain privilege via a setuid binary | **yes** | `no_new_privs` + container `no-new-privileges` |
| read another *call's* scratch files | **yes** | per-call temp dir |
| exfiltrate its own params to the internet | **no** | §3.4 |
| exhaust the container's CPU | partly | `RLIMIT_CPU` per process; the container `mem_limit` bounds the rest |
| read the host filesystem | partly | container isolation only — no chroot/mount namespace |

## 4. Data model — the `code_agents` table

### 4.1 Why a separate table

`custom_agents.preset_name` is `text NOT NULL REFERENCES llm_presets(name)`.
Adding a `kind` discriminator means making it nullable, which drops a
real constraint on every existing LLM row to accommodate a kind that will
never use it. The two kinds share about six columns and differ in about
eight.

The reuse that matters is **code, not schema** — §11 extracts it.

```sql
CREATE TABLE code_agents (
    agent_id      text PRIMARY KEY
                  CHECK (agent_id ~ '^code_[a-z][a-z0-9_]*$'),
    description   text NOT NULL DEFAULT '',

    -- the operator's module source, and the function within it
    code          text NOT NULL DEFAULT '',
    entrypoint    text NOT NULL DEFAULT 'run'
                  CHECK (entrypoint ~ '^[a-z_][a-z0-9_]*$'),

    -- ordered list of {name, type, description, required}; type is one of
    -- string|integer|number|boolean|array|object  (§5)
    parameters    jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- optional JSON Schema for the return value → AgentInfo.produces_schema
    returns       jsonb,

    -- {ENV_NAME: "env://VAR"} — refs only, never literals (§9)
    secret_refs   jsonb NOT NULL DEFAULT '{}'::jsonb,

    timeout_s     integer NOT NULL DEFAULT 30  CHECK (timeout_s BETWEEN 1 AND 300),
    memory_mb     integer NOT NULL DEFAULT 512 CHECK (memory_mb BETWEEN 64 AND 4096),

    groups        jsonb NOT NULL DEFAULT '[]'::jsonb,
    capabilities  jsonb NOT NULL DEFAULT '[]'::jsonb,
    expose_to_llm boolean NOT NULL DEFAULT true,
    output_as_file boolean NOT NULL DEFAULT false,
    enabled       boolean NOT NULL DEFAULT true,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text REFERENCES users(user_id),

    -- provisioning, mirrors mcp_servers / custom_agents exactly
    pending_invitation_token      text,
    pending_invitation_expires_at timestamptz
);
```

Notes:

- `agent_id` stores the **full** `code_<slug>` string, matching how
  `custom_<slug>` and `mcp_<id>` are stored. The `code_` prefix is the
  namespace and the ACL handle.
- `timeout_s` caps at 300 s. The router's own task deadline is the outer
  bound; this is the inner one the bridge enforces itself.
- `memory_mb` maps to `RLIMIT_AS`. Unlike the stdio path — which disables
  `RLIMIT_AS` because `uvx` needs enormous virtual address space — a bare
  `python -I -S` has a predictable footprint, so the cap is usable here.
- No `network` column. §3.4 — a column that enforces nothing is worse
  than its absence, because it reads as a guarantee.

## 5. Typed parameters

The LLM kind is string-only, and the reason is specific: `$`-templating
into a prompt. Values are substituted as inert text, so types would buy
nothing and cost a conversion story.

**That rationale does not transfer.** A code agent receives a `dict`;
the router already jsonschema-validates the payload at admit
(`bp_router/tasks.py:673-721`); and forcing an operator to write
`int(params["limit"])` at the top of every function is friction with no
safety return. So a code-agent parameter carries a `type`:

```
string | integer | number | boolean | array | object
```

Each becomes one property of that JSON type in the mode's
`accepts_schema`, with `additionalProperties: false` so the router
rejects anything undeclared. `required` works as it does today.

There is no `file_ref` flag in v1: dereferencing a file name needs
`ctx.files`, which the child does not have (§2). An operator who wants
file content passes it as a `string` parameter, or waits for v2.

**`returns`** is optional and, when set, becomes the agent's
`produces_schema`. This is a real advantage over the LLM kind — the code
controls its output shape, so it can promise one, and a calling model
gets a typed contract instead of prose.

## 6. The agent

Single mode, exactly like the custom LLM agent, so the SDK's `_tool_specs`
emits the bare-name tool `call_code_<slug>` and the mode label never
leaks into the external name.

```python
MODE = "main"

info = AgentInfo(
    agent_id=row.agent_id,                       # code_<slug>
    description=row.description,
    groups=list(row.groups),
    capabilities=["code.agent", *row.capabilities],
    accepts_schema={MODE: _accepts_schema(row.parameters)},
    produces_schema=row.returns or None,
    produces_files=row.output_as_file,
    hidden=not row.expose_to_llm,
)
```

`code.agent` is the coarse marker, paralleling `mcp.bridge` and
`custom.agent`. `accepts_schema` is operator-pinned at construction, and
`_merged_hello_agent_info` (`bp_router/ws_hub.py:64`) republishes it on
every reconnect — so an admin edit that changes parameters takes effect
when the supervisor restarts the bridge, with no re-onboarding.

## 7. The subprocess contract

The crux of v1. Two files land in the per-call temp dir:

- **`agent_main.py`** — the operator's `code`, verbatim.
- **`harness.py`** — a fixed, bridge-owned stub. Never operator-editable.

The harness:

1. **Saves fd 1, then points `sys.stdout` at `sys.stderr`.** This is not
   optional and it is the detail that bites on day one: an operator's
   stray `print()` would otherwise interleave with the result JSON and
   corrupt it. Operator prints become stderr, which the parent drains
   into logs; the result goes out on the saved descriptor.
2. Reads one JSON line from stdin:
   `{"params": {...}, "context": {...}}`.
3. `import agent_main`, resolves `entrypoint`, and calls it:

   ```python
   def run(params: dict, context: dict) -> str | dict | list | int | float | bool | None
   ```

   Always two positional arguments — no signature introspection, no
   optional-second-arg magic. `context` carries `user_id`, `session_id`,
   `task_id`, `agent_id` and `deadline_s`. It is informational: nothing
   in v1 lets the code act on the backplane with it.
4. Writes exactly one JSON line to the saved fd:
   `{"ok": true, "result": ...}` or
   `{"ok": false, "error": {"type": ..., "message": ..., "traceback": ...}}`.
5. Exits.

Parent side:

- A `str` result becomes `AgentOutput.content`. Anything else is
  JSON-serialised into `content` (and validated against `returns` when
  declared).
- Result over `_MAX_INLINE_RESULT_BYTES` (1 MB) is **refused with a clear
  error naming `output_as_file`**, rather than silently truncated — a
  truncated JSON body is worse than an error, because the caller cannot
  tell.
- `output_as_file` routes the body through `ctx.files.write(...)` in the
  **parent**, which already handles name collisions via
  `dedup="append_count"`.
- A failed call returns the error `type` and `message` to the caller and
  logs the traceback. The traceback is *not* returned: the caller is
  usually a model that cannot act on it, and operator code may
  legitimately have secrets in local variables that a traceback renders.
- stderr is drained continuously (never after `wait`, or a child that
  fills the pipe buffer deadlocks), truncated, and logged.

## 8. Failure taxonomy

Getting this right is what makes the agent usable by a model, and it is
where the LLM kind got it wrong (§14, issue 2):

| condition | to the caller |
| --- | --- |
| payload fails `accepts_schema` | router refuses at admit — never reaches the handler |
| `run` raises | `AgentOutput` failure with `type: message` |
| exceeded `timeout_s` | failure, "timed out after N s" |
| killed by a rlimit (OOM, NPROC) | failure naming the limit hit |
| result unserialisable / oversize | failure naming the fix |
| harness/spawn failure, chown refused | **internal error**, not a caller error — this is the bridge's fault, and it must not be reported as "your input was bad" |

## 9. Secrets and the child's environment

Reuse `auth_resolver.resolve_auth_value` unchanged: `secret_refs` holds
`env://VAR` references, resolved in the **parent** and injected into the
child's constructed environment. The same validator posture as
`mcp_servers.auth_value_ref` applies — **the admin API refuses a literal
secret in the DB**.

The child's environment is *built*, never inherited — the same rule
`_build_stdio_spawn` follows:

```python
env = {
    "PATH": <inherited>, "HOME": <workdir>, "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "BP_TASK_ID": ..., "BP_USER_ID": ..., "BP_SESSION_ID": ...,
    **resolved_secret_refs,
}
```

`BP_MCP_BRIDGE_SERVICE_SECRET` is absent by construction, and there
should be a test that asserts exactly that — it is the single most
important invariant in this design.

## 10. Audit and the code body

The audit payload records `sha256(code)` and its length, **never the
body** — the same rule the session store's audit follows for message
content (`router-managed-session-store.md` §12). An append-only hash
chain containing operator code is an erasure problem, and the code is
already in the `code_agents` row where it can be read, edited and
deleted normally.

`config_signature()` includes the code hash, so an edit restarts the
bridge exactly as a prompt edit does for the LLM kind.

## 11. What gets shared with `custom_agents`

Not the table (§4.1) — the code. Extracted before the new kind is built,
so there is one implementation rather than two that drift:

| today | becomes |
| --- | --- |
| `CustomAgentParam` + name grammar | a shared param model, `type` added |
| `_accepts_schema` (`custom_agent.py:121`) | shared builder over typed params |
| `_check_groups_grammar` / `_check_caps_grammar` | already shared — reuse as-is |
| `_mint_custom_agent_pending_invitation` | one helper parameterised by table |
| `_active_custom` + `_reconcile_custom_once` + `_start/_stop_custom` | one generic reconcile keyed by `(kind, id)` |
| admin UI param editor | one template partial, plus a type dropdown |

The supervisor generalisation is the one with real payoff: it currently
has two near-identical 60-line blocks, and this feature would make it
three.

## 12. What v2 would add

Sketched so v1's shape does not foreclose it, and explicitly not built:

- **Host callbacks.** A JSON-RPC channel child→parent over a dedicated
  fd — the inverse of `StdioMcpClient`, which already implements
  newline-delimited JSON-RPC over pipes. The parent serves a fixed,
  allow-listed surface (`llm.generate`, `files.read`, `files.write`,
  `peers.call`) gated by the same per-row toggles the LLM loop uses
  (`file_access`, `peer_tools_enabled`). The child-side stub becomes
  `context.llm(...)` etc. Nothing in v1's contract blocks this: §7's
  `context` dict is the natural place for the handles to appear.
- **A `code_worker` container** with its own compose network, for
  per-agent egress policy (§3.4) and per-agent dependency venvs. This is
  the honest home for both, and it is a deployment change, not a
  protocol one.
- **`file_ref` parameters**, which need `ctx.files` and therefore the
  callback channel.

## 13. Implementation sequence

**Preflight — fix what the LLM kind already got wrong**, before a third
kind makes it worse:

0. Custom-agent metrics (§14 issue 1) and the PATCH uniqueness validator
   (§14 issue 4). Both are small; the metrics one is the difference
   between "the bridge is broken" being noticed and not.

Then:

1. **Extract the shared surface** (§11): param model, schema builder,
   invitation helper, generic supervisor reconcile. No behaviour change;
   the existing 40 custom-agent tests are the regression net.
2. **DB**: migration `code_agents`; `CodeAgentRow` / `CodeAgentView`;
   `insert/get/list/update/delete_code_agent` + `_CODE_AGENT_SELECT_COLS`.
3. **Router admin API**: `POST/GET/PATCH/DELETE /v1/admin/code-agents`
   plus `/reconnect` and `/connected`, with validators (id grammar,
   entrypoint grammar, param names + **uniqueness on both POST and
   PATCH**, `secret_refs` values must be refs, `returns` must be a valid
   JSON Schema). Audit with the code hash.
4. **Bridge — the runner first, in isolation**: `code_agent.py`'s
   subprocess runner with a hand-built spec, no backplane. Verify in a
   real container, as root, that the uid drops, the rlimits bind, the
   timeout kills the group, and **the child cannot see
   `BP_MCP_BRIDGE_SERVICE_SECRET`**. This is this design's equivalent of
   the LLM kind's "does `ctx.llm` work in the bridge?" — the one
   assumption everything else rests on, pinned before the UI exists.
5. **Bridge — the agent**: `build_code_agent` + handler + `CodeAgentBridge`,
   registered with the generic supervisor from step 1.
6. **Admin UI**: `bp_admin/pages/code_agents.py` + templates mirroring
   `custom_agents` — code textarea (monospace), typed-param editor,
   secret-ref rows, timeout/memory, and a plain warning that the code
   runs with the bridge's network access.
7. **ACL**: document the `code_*` group convention, mirroring `custom_*`.
8. **Tests**: structural (migration text, select-cols coverage),
   validators, schema mapping, and — the ones that matter — a real
   interpreter round trip, a timeout that kills a forked grandchild, the
   scoped-env assertion, oversize-result refusal, and stdout-pollution
   (an operator `print()` must not corrupt the result).
9. **Docs**: this doc's status line; the bridge docs' "kinds" list;
   `docs/backplaned/changelog.md` if any platform file moves.

## 14. Issues in the current custom-agent implementation

Found while inspecting; recorded here because §13 step 0 depends on them
and because two of them are shapes this design must not copy.

1. **No metrics for custom agents at all.** Every metric in
   `bp_mcp_bridge/metrics.py` is incremented only on the MCP path, and
   `supervisor.py:207` sets `active_bridges` from `len(self._active)` —
   MCP bridges only. A deployment with 10 custom agents and 0 MCP servers
   reports `active_bridges 0`. Fix before adding a third kind, and make
   the gauge count all kinds.
2. **`_read_text_ref` reports server faults as caller errors**
   (`custom_agent.py:173`): a bare `except Exception` becomes
   `InputValidationError` → 400, so a file-store outage reaches the
   calling model as "cannot read your file". §8 is this design's answer.
3. **The loop bounds rounds, not context** (`custom_agent.py:295`).
   `max_rounds` caps LLM calls; nothing caps accumulated tool-result
   size, and `bp_sdk/file_tools.py:43` allows 500k chars *per read*. The
   failure is a provider rejection after 15 rounds of spend.
4. **PATCH accepts duplicate parameter names that POST rejects.**
   `_check_param_names_unique` is called only from
   `CustomAgentCreate._cross_field` (`admin.py:4348`); `update_custom_agent`
   re-validates placeholders against the merged record but never
   uniqueness. `_accepts_schema` then silently collapses the duplicate.
5. **Deleting an agent leaves its backplane identity registered.**
   `delete_custom_agent` drops the row but never revokes the `agents`
   row, its credentials, groups or ACL reachability; re-creating the same
   `agent_id` inherits the old identity. `delete_mcp_server` is the same,
   so this is a shared platform gap — but `code_agents` will inherit it
   too, and it should be fixed once for all three kinds.
6. **`enabled: false` is not an authorization control.** The supervisor
   drops disabled rows from `desired`, so the bridge disconnects, but the
   router keeps the agent registered and ACL-reachable; a call fails as
   `destination not active`. Fine as behaviour — just never describe
   `enabled` as a security control.
7. **The compose egress comment is wrong** (`docker-compose.prod.yml:610-613`).
   No network is `internal: true`; the bridge has outbound internet
   today. §3.4 depends on this being stated accurately.

## 15. `[shipped]` What the build changed

Nine places the implementation departed from the text above. The first
seven are small. The last two are not: both are cases where the design's
one-line summary of a mechanism ("wait, then read") hid a real bug that only
running the thing surfaced — which is the argument for §13's step 4 in a
nutshell.

  * **`python -I` implies `-P`, so the harness must put its own directory on
    `sys.path`.** §7 said "spawn `python -I -S harness.py`" and stopped
    there. `-I` is what we want against the interpreter's own environment
    (no `PYTHON*` vars, no user site-packages) but it also refuses to
    prepend the script's directory — so `import agent_main` failed outright
    on the first run. The harness now inserts the workdir, and only the
    workdir, explicitly.
  * **`RLIMIT_FSIZE` was added to the shared `StdioSpawnConfig`.** §4 listed
    `memory_mb` but nothing bounded disk. A runaway `open(...).write` fills
    the volume every other bridged agent shares. The stdio path keeps it
    disabled (an MCP server may legitimately cache large artifacts); the
    code path sets 64 MB.
  * **The drains are standalone tasks, not gathered with the wait.** The
    first shape — `wait_for(gather(feed, drain, drain, wait))` — deadlocks
    its own cleanup: on timeout the gather is already cancelled, so
    re-awaiting it to collect partial output raises `CancelledError` out of
    the timeout handler. The drains now run for the call's whole life and
    are collected after the kill, which is also how a killed function's
    partial stderr still reaches the log.
  * **The supervisor generalisation landed as `_Kind`.** §11 asked for it;
    it is a dataclass of (name, active map, lister, row factory, bridge
    factory) plus one `_reconcile_kind`. `_reconcile_custom_once` /
    `_start_custom` / `_stop_custom` survive as thin named wrappers, because
    they are the names the codebase refers to.
  * **`returns: {}` is the PATCH clear sentinel.** Not in the design, and
    needed: PATCH's "None means leave alone" rule otherwise leaves no way to
    remove a declared output schema once set.
  * **An unresolvable secret is skipped and logged, not fatal.** §9 did not
    say. Failing the bridge means one typo in one of five refs takes the
    agent offline entirely; skipping means the function sees the variable
    missing and can say so. The ref NAME is logged; the value never is.
  * **Code agents write `output.txt`, not `output.md`.** The LLM kind's
    output is prose and markdown is right for it. A function returns JSON as
    often as text, and naming it `.md` mislabels the majority case.
  * **The runner waits on the RESULT LINE, not on process exit.** §3.2's
    diagram says `wait(timeout_s)`, and that is wrong in a way only a test
    finds: `asyncio`'s `Process.wait()` does not return until the pipes close
    too, so a function that returns fine after a bare
    `subprocess.Popen(...)` — a background job it never reaps — was reported
    as a **timeout with its result discarded**. The harness writes exactly one
    line, so the runner reads exactly one line and takes the result the
    moment it exists. The process group is then killed unconditionally, on
    success as well as timeout, so the straggler does not outlive the call
    either.
  * **Cleanup runs on every exit path, and lets its tasks finish.** Two
    bugs in one: an oversize result raised out of the read and skipped
    teardown entirely, and cancelling the drains mid-read left `stdin`
    unclosed and the pipes open. Both leak the subprocess transport to the
    garbage collector, which surfaces much later — in an unrelated call — as
    `Event loop is closed` from `BaseSubprocessTransport.__del__`. The fix
    is a single `_cleanup` on every path that kills the group, then lets the
    feed and drain tasks *complete* rather than cancelling them.

### 15.1 `[shipped]` Retry policy, after the fact

Two operator reports landed after v1: the bridge **retried endlessly without
giving up**, and **HTTPS was flaky with some providers**. Both are bridge-wide
rather than code-agent specific, and both are now fixed here because this is
where the retry story is written down.

**Endless retry** was three loops, none of which escalated or stopped:

  * the supervisor respawned a dead bridge every poll interval, forever —
    a connect attempt against someone else's server every 30 s, with a stack
    trace to match, for the life of the process;
  * the SSE stream task reconnected forever, *inside* a bridge that still
    reported itself healthy;
  * the reconcile-refresh loop re-armed its own event on a fixed 5 s.

`bp_mcp_bridge/health.py` is the piece that stops the first. Per bridged
agent — all three kinds — it counts consecutive failures, defers the next
start by a doubling backoff (30 s → 15 min), and after eight failures stops
entirely, logging once and setting `bp_mcp_bridge_bridge_given_up`. Two
things reopen it, both already in the admin UI and both meaning "an operator
wants another go": **editing the row** (the config signature changes) and
**clicking Reconnect** (a fresh invitation is minted). It is deliberately not
a DB `failed` flag — that would make recovery from a two-hour upstream outage
need a human, and upstreams recover on their own.

One subtlety worth stating: a *clean but immediate* exit counts as a failure.
That is how a bridge with neither credentials nor an invitation returns, and
counting only exceptions would leave it spinning at the poll interval — the
same bug in a quieter costume.

The other two stop **where they run**, because the health gate cannot see
either. The SSE stream task and the refresh loop both live *inside* a bridge
that never exits, so no task ever completes for the supervisor to account
for — a gate that only watches task exits would have left both spinning
under a green light. So `SseMcpClient` caps consecutive reconnects, and
`_refresh_loop` now escalates 5 s → 5 min and, after eight consecutive
failures, stops re-arming itself and parks on `wait()`.

Parking is deliberately cheaper than giving up on a whole bridge: the
consequence is a **stale mode set**, not a dead agent — the bridge keeps
serving the tools it already has. And recovery needs no gate of its own,
because the loop is still sitting on its event: the next genuine signal, an
admin "Refresh tools" click or an SSE `tools/list_changed`, starts it over
from zero. That is why the give-up branch is a `continue` and not a `return`
— a `return` would make the bridge permanently deaf to refresh for the rest
of its life, which is a worse bug than the one being fixed.

**Flaky HTTPS** was two things, and the intuitive fix is the weaker one:

  * The real gap was that the CONNECT handshake (`initialize` +
    `tools/list`) had **no retry**, while `tools/call` had had one all
    along. A hosted provider dropping a single connection therefore killed
    the whole bridge and cost a full restart cycle.
    `ServerBridge._connect_with_retry` closes that with the same
    bounded-retry-on-transient policy, and the classifier moved to
    `mcp_client.is_transient_error` so there is one definition of
    "worth another go".
  * **Pool tuning does not fix stale sockets**, though it is the first thing
    one reaches for. httpx already expires idle connections after 5 s —
    shorter than any common LB idle timeout — so the failures that get
    through are connections closed for other reasons inside whatever window
    is chosen. The explicit `Limits` pins the value so it cannot drift
    looser; the retry is what recovers.
  * What the client genuinely fixes is **timeouts**: a scalar `timeout=60`
    gave the *connect* phase 60 s, and the SSE client's `timeout=None`
    bounded nothing at all, so a hung TLS handshake could stall its stream
    task forever.

## 16. What not to do

- **Don't run operator code in the bridge process.** §3.1. Not "for
  simple agents", not "behind a flag". The credential blast radius is the
  entire point of the subprocess.
- **Don't let the child inherit the parent's environment.** Build it
  (§9). `os.environ.copy()` plus deletions is the version of this that
  looks right and leaks the next variable someone adds.
- **Don't return tracebacks to the caller.** Log them. Operator locals
  contain secrets.
- **Don't add a `network` column that doesn't enforce anything** (§3.4).
  A control that reads as a guarantee and isn't is worse than none.
- **Don't reuse the `custom_agents` table** — it costs the
  `preset_name` FK for every existing row (§4.1).
- **Don't keep a warm worker process.** Cross-tenant state leak (§2).
- **Don't let the harness be operator-editable**, and don't let operator
  `print()` reach fd 1 (§7).
- **Don't add per-agent `pip install`** without the separate container
  (§12) — a package fetched at call time is both a supply-chain hole and
  an unbounded cold start.
- **Don't give the bridge `CAP_NET_ADMIN`** to get netns isolation. It is
  a larger capability than the one it would contain.
- **Don't put the code body in the audit chain** (§10).

## 17. Open questions

- **Timeout vs. the router's task deadline.** `timeout_s` is the bridge's
  inner bound; `ctx.deadline` is the outer one, and neither the LLM
  loop nor this design consults it. Should the runner clamp `timeout_s`
  to the remaining deadline? Probably yes, and it is a small change —
  but it is the first place in the bridge that would read `ctx.deadline`
  at all, so it deserves a deliberate decision rather than a drive-by.
- **Concurrency per agent.** Nothing bounds how many subprocesses one
  code agent may have in flight; a popular agent under a burst could
  spawn dozens against a 1 GB container. `RLIMIT_NPROC` is per-uid and
  therefore *does* bound it — but bluntly, by failing the fork. An
  explicit per-agent semaphore in the bridge would fail more gracefully.
  Deferred until a real burst exists.
- **Cost/quota attribution.** Inherited from the LLM kind's open
  question, and now sharper: a code agent's *compute* is the operator's
  container, but its *task* is the calling user's. Nothing meters
  subprocess CPU per user. Probably fine; worth naming.
- **Editing code from the admin UI with no test path.** An operator has
  no way to try a function before it goes live to every caller. A "dry
  run with sample params" endpoint that executes the same runner and
  returns the raw result would be a large usability win and a small
  amount of code. Deliberately out of v1 scope; likely the first
  follow-up anyone asks for.
- **Naming.** `bp_mcp_bridge` hosting a third non-MCP kind makes the
  misnomer worse. Still not worth the churn (package path, container,
  env vars, deploy). Name the new modules neutrally (`code_agent.py`),
  call the process "the agent bridge" in docs, and leave the rename as a
  cosmetic change for a quieter week.

## 18. Sizing — estimate vs. shipped

| piece | estimated | shipped |
| --- | --- | --- |
| preflight: custom-agent metrics + PATCH validator | ~80 | 84 |
| shared surface (§11): `agent_common.py` + `agent_bridge.py` | ~150 | 276 |
| migration + models + queries | ~200 | ~280 |
| router admin API + validators | ~350 | ~450 |
| `code_runner.py` (subprocess + harness) | ~400 | 446 |
| `code_agent.py` (agent + handler) | — | 185 |
| `code_agent_bridge.py` | ~150 | 185 |
| admin UI (page + 2 templates) | ~450 | 913 |
| tests (`tests/test_code_agents.py`) | ~600 | 963 |

~2 700 lines plus 963 of tests — about 45% over, mostly in the two
places a spec can afford to be vaguer than code: the admin UI (a code
textarea, a typed-param editor and a secret-ref editor are three Alpine
components, not one) and the shared extraction, which turned out to be
two modules rather than one because the schema helper and the bridge
lifecycle have nothing to do with each other.

No `bp_agents` dependency, no router protocol change, no new container,
and one additive field on a shared struct (`rlimit_fsize_bytes`, §15).

The largest risk was step 4 — and it paid for itself immediately: the
first run failed on `-I` implying `-P`, which no amount of reading the
design would have surfaced. Pinning the runner before the UI existed
meant that was a 5-minute fix rather than a mystery three layers down.
