# ACL — Firewall-Style Rules

> The router's access-control model. One rule grammar covers both
> visibility (what an agent sees in its catalog) and permission (what
> an agent may invoke at task-admit time).
>
> Companion documents:
> - [`router/state.md §2.4`](./router/state.md#24-rbac--principal-levels) — user-level grammar (`admin`, `service`, `tierN`).
> - [`router/protocol.md`](./router/protocol.md) — wire frames and the `Welcome` catalog projection.
> - [`security.md`](./security.md) — JWT / session / agent auth.

## 1. Goals

The ACL exists to answer two questions on every NewTask admit:

1. **Visibility.** Should agent A see agent B in its catalog at all?
2. **Permission.** May the user behind A's current task invoke B?

Both are answered from the same rule list. There is no separate
catalog config, no per-agent ACL declarations, no scoped grants —
exactly one ordered list of rules, evaluated top to bottom.

## 2. Vocabulary

| Term       | Meaning                                                                     |
| ---------- | --------------------------------------------------------------------------- |
| Group      | A label an agent declares it belongs to (e.g. `rank0`, `team:coding`).      |
| Capability | A dotted namespace string an agent declares it provides (e.g. `llm.generate.text`). |
| Pattern    | A `<group>/<cap>` slot or `@<agent_id>` literal — see §4.                   |
| Rule       | A 4-tuple `(effect, user_level, caller_pattern, callee_pattern)` — see §3.  |
| User level | The session's principal level: `admin`, `service`, or `tierN`.              |

The user-level grammar is owned by [`router/state.md §2.4`](./router/state.md#24-rbac--principal-levels);
this document only consumes it.

## 3. Rule shape

A rule is a single line of four required fields:

```
<effect>  <user_level>  <caller_pattern>  ->  <callee_pattern>
```

| Field            | Domain                                              | Notes                                              |
| ---------------- | --------------------------------------------------- | -------------------------------------------------- |
| `effect`         | `allow` \| `deny`                                   |                                                    |
| `user_level`     | `*` \| `admin` \| `service` \| `tier0` \| `tier1` … | "or stricter" semantics — see §5.                  |
| `caller_pattern` | pattern slot                                        | Matches the agent originating the call.            |
| `callee_pattern` | pattern slot                                        | Matches the agent being invoked.                   |

Plus housekeeping persisted with the row: `rule_id`, `ord` (evaluation
order), `name`, `description`, `created_at`, `created_by`.

Examples:

```
allow  *      */*                  ->  */*                       # bootstrap default
deny   tier3  */*                  ->  rank0/*                   # tier3 cannot reach any rank0 agent
allow  *      rank0/*              ->  rank1/llm.generate.text   # rank0 may invoke text-LLM in rank1
deny   *      */*                  ->  @experimental_agent       # nobody calls this one
allow  admin  */*                  ->  @experimental_agent       # …except admin
```

## 4. Pattern slots

A pattern slot accepts exactly one of the two forms:

| Form                | Matches the agent when…                                              |
| ------------------- | -------------------------------------------------------------------- |
| `*/*`               | always.                                                              |
| `<group>/*`         | `<group>` is in the agent's `groups`.                                |
| `*/<cap>`           | `<cap>` is in the agent's `capabilities`.                            |
| `<group>/<cap>`     | both: the agent has the group and provides the capability.           |
| `*/<prefix>.*`      | the agent has any capability under the `<prefix>.` namespace.        |
| `<group>/<prefix>.*`| both: agent in group AND has any capability under `<prefix>.`.       |
| `@<agent_id>`       | the agent's `agent_id` equals `<agent_id>` exactly.                  |

Whole-token `*` wildcards are meaningful at the half-positions
(`*/X`, `X/*`, `*/*`). They are not character-class wildcards:
`rank*` is a literal group name, not a prefix match. `@*` and
`@<id>/<cap>` are rejected at rule-insert validation.

The capability half ALSO accepts a **trailing `.*` prefix glob**
of the shape `<prefix>.*`, where `<prefix>` is one or more dotted
lowercase segments. Semantics:

* `llm.*` matches any capability of the form `llm.<one-or-more-segments>`
  — e.g. `llm.generation.text`, `llm.embedding`, `llm.tool_call`.
* It does NOT match the bare `llm` (no trailing segment) or
  `llmpy.foo` (the dot is significant).
* The matcher is `any(c.startswith(prefix + ".") for c in capabilities)`.
* Multiple capabilities on the agent are OR-ed — at least one must
  match for the pattern to allow.

Prefix globs are **not supported on the group half** — groups are
flat tags by design, so the bundling intent is already expressible
via group membership. Leading-globs (`*.text`), middle-globs
(`llm.*.text`), and double-stars (`llm.**`) are deliberately
rejected at rule-insert validation. See `bp_router.acl.is_valid_pattern`.

`agent_id` strings are constrained to `[A-Za-z_][A-Za-z0-9_-]{0,63}`
so they cannot collide with `/`, `*`, `@`, or whitespace; a stricter
CHECK constraint lives on `agents.agent_id`. Group names and
capability names follow the same alphabet — see §10.

## 5. User-level matching

A rule's `user_level` works in two distinct modes:

  - When set to a **tierN**, it means "this tier or stricter."
    Stricter = more privileged = lower tier index. `tier1` admits
    `tier0` and `tier1`. It also admits `admin` and `service`
    (both internally mapped to `tier_index = -1`), since those are
    privilege-superset principals.
  - When set to **`admin`** or **`service`**, it means "exactly this
    level." `admin` rules do NOT admit `service` and vice versa —
    the two are peer principal types, not super/sub.
  - **`*`** admits any principal regardless of level.

The exact algorithm in `bp_router.acl._user_level_satisfies`:

```python
def _user_level_satisfies(actual: str, rule_level: str) -> bool:
    if rule_level == "*":
        return True
    if rule_level in ("admin", "service"):
        # admin / service are peer roles — exact-match only.
        return actual == rule_level
    # rule_level is tierN. Lower tier_index = stricter; admin/service
    # both map to -1 and so satisfy any tierN gate.
    idx = tier_index(rule_level)
    if idx is None:
        return False
    return level_satisfies_tier(actual, idx)
```

Concrete admission table for a fixed `rule_level`:

| Rule's `user_level` | Admits `admin`? | Admits `service`? | Admits `tier0`? | Admits `tier1`? | Admits `tier2`? |
| --- | --- | --- | --- | --- | --- |
| `*`       | ✅ | ✅ | ✅ | ✅ | ✅ |
| `admin`   | ✅ | ❌ | ❌ | ❌ | ❌ |
| `service` | ❌ | ✅ | ❌ | ❌ | ❌ |
| `tier0`   | ✅ | ✅ | ✅ | ❌ | ❌ |
| `tier1`   | ✅ | ✅ | ✅ | ✅ | ❌ |
| `tier2`   | ✅ | ✅ | ✅ | ✅ | ✅ |

So `admin` and `service` are interchangeable **only against `tierN`
rules** (both pass any tier gate). They are NOT interchangeable
against an `admin` or `service` rule — those require exact match.

To express **only this exact level**, write two rules — a deny
above an allow:

```
ord  effect  level  caller   callee
1    deny    tier0  */*  ->  @gemini_main      # blocks admin, service, tier0
2    allow   tier1  */*  ->  @gemini_main      # admits everything ≤ tier1
```

For each candidate level:

| Level     | Hits rule 1 (`deny tier0`)? | Hits rule 2 (`allow tier1`)? | Outcome      |
| --------- | --------------------------- | ---------------------------- | ------------ |
| `admin`   | yes                         | —                            | DENY         |
| `service` | yes                         | —                            | DENY         |
| `tier0`   | yes                         | —                            | DENY         |
| `tier1`   | no (idx 1 > 0)              | yes (idx 1 ≤ 1)              | ALLOW        |
| `tier2+`  | no                          | no (idx > 1)                 | terminal deny |

Order matters: the deny line must precede the allow. Admin UIs
should offer a one-click "exact level" macro that emits the pair.

## 6. Evaluation

Every visibility check (catalog construction, permission re-check at
admit) and every permission check (`NewTask` admit) runs the same
algorithm:

```python
def is_allowed(caller: AgentRow, callee: AgentRow, user_level: str) -> Decision:
    if caller.agent_id == callee.agent_id:
        return Decision.deny(reason="self_call")

    for rule in rules_in_order:                         # ord ascending
        if not _user_level_satisfies(user_level, rule.user_level):
            continue
        if not _matches(rule.caller_pattern, caller):
            continue
        if not _matches(rule.callee_pattern, callee):
            continue
        return Decision(allow=(rule.effect == "allow"), rule_name=rule.name)

    return Decision.deny(reason="default")              # implicit terminal deny
```

Properties:

- **First match wins.** Subsequent rules are not consulted.
- **Default deny.** An empty rule list — or one with no matching line —
  produces deny.
- **Self-call always denied.** No rule can override this.
- **Order is admin-managed.** The `ord` column is the source of truth;
  `POST /v1/admin/acl/rules` and the reorder endpoint are the only
  paths that mutate it.

There is no "scope" axis on a rule. A single rule applies to both
visibility and permission. See §7 for how the catalog projection
deals with the missing `user_level` at handshake time.

## 7. Catalog construction

A `WelcomeFrame.available_destinations` is built per agent, on every
WS handshake, via `bp_router.visibility.available_destinations`. The
agent has no associated session at handshake — so we cannot evaluate
rules with a concrete `user_level`. The catalog is built *generously*
(visibility = "is there any user level under which this would be
allowed?"), and each entry carries the set of admitting levels so
the SDK can filter per-task.

```python
def is_visible(caller: AgentRow, callee: AgentRow) -> bool:
    return any(
        is_allowed(caller, callee, lvl).allow
        for lvl in deployment_levels()      # admin, service, tier0..tier_max
    )


def callable_user_levels(caller: AgentRow, callee: AgentRow) -> list[str]:
    return [
        lvl for lvl in deployment_levels()
        if is_allowed(caller, callee, lvl).allow
    ]
```

`deployment_levels()` returns `["admin", "service", "tier0", "tier1",
..., f"tier{ROUTER_MAX_TIER}"]`. `ROUTER_MAX_TIER` is a deployment
setting (default 3); rules referencing higher tiers are valid, they
just don't surface in the catalog probe.

A catalog entry has the shape:

```jsonc
{
  "agent_id":              "gemini_main",
  "description":           "...",
  "groups":                ["rank1", "provider:gemini"],
  "capabilities":          ["llm.generate.text"],
  "accepts_schema":        { ... },
  "documentation_url":     "...",
  "hidden":                false,
  "callable_user_levels":  ["admin", "service", "tier0", "tier1"],
  "last_seen_at":          "2026-04-30T14:32:11+00:00"
}
```

The SDK uses `callable_user_levels` to filter outbound LLM tool
schemas: at handler time, `ctx.user_level ∈ entry.callable_user_levels`
is the gate. Filtering is an optimisation; the router admit check
re-evaluates rules live and is the security-critical enforcement.

`last_seen_at` is the ISO-8601 timestamp of the agent's most recent
successful WS handshake (or `null` if the agent has never connected).
It is a hint for admin UIs and LLM tool-selection heuristics — an
agent unseen for hours is probably offline and worth deprioritising —
and does **not** affect ACL eligibility. Catalog membership reflects
registration + rule allow, not current connectivity.

`hidden: true` is a separate SDK convenience flag that suppresses the
agent from auto-generated tool schemas. It does **not** affect ACL —
a hidden agent can still be invoked if the rules allow it, just
never appears in the LLM's tool list.

## 8. Permission at NewTask admit

The router's task-admit path (`bp_router.tasks.admit_task`) calls
`is_allowed(caller_row, callee_row, session.user_level)`. Failure
raises `AdmitError("acl_denied")` and the spawn is rejected with the
matched rule name in the audit log.

This re-evaluates against the live rule set, so admin edits take
effect on the next admit (no reconnect required).

**Modes are not an ACL axis.** An agent registers every handler in
one mode registry; `NewTaskFrame.input_mode` selects which mode's
schema the router validates against and which handler runs.
Control-plane modes are just modes flagged `tool=False` (listed in
`AgentInfo.non_tool_modes`, hidden from `build_tools`) — there is
no `is_control` flag and no separate `accepts_control_schema`. **ACL
admit calls the same `is_allowed(...)` against the same rule table
regardless of mode** — there is no per-mode policy. A permissive
fallback like `allow * */* -> */*` (the bootstrap default at ord 2)
therefore admits a call to ANY of the destination's modes,
control-plane ones included. Operators wanting to gate a
control-plane mode must add explicit deny rules at a lower `ord`
than the catch-all allow; a per-mode `scope` axis on the rule
grammar is not currently supported (mode is a routing key, not a
policy principal).

### 8.1 Catalog refresh — `CatalogUpdate` frame

To keep cached catalogs aligned with admin changes, the router pushes
a `CatalogUpdate` frame (see `docs/router/protocol.md`) whenever the
catalog could change for a connected agent:

| Trigger                                                | Catalog refresh? |
| ------------------------------------------------------ | ---------------- |
| Agent onboards (`POST /v1/onboard`)                    | yes — push to all live peers              |
| Admin suspend (`POST /v1/admin/agents/{id}/suspend`)   | yes              |
| Admin evict (`POST /v1/admin/agents/{id}/evict`)       | yes              |
| Any ACL rule mutation                                  | yes              |
| WS disconnect / heartbeat timeout                      | no               |
| WS reconnect / resume                                  | no — handshake delivers a fresh `Welcome` to that one agent |
| Resume window expiry                                   | no               |

Disconnects deliberately don't refresh: catalog represents
"registered + ACL-eligible," not "currently online." Calls to an
offline agent fail at admit with `agent_disconnected` — clean error,
no catalog churn under network flaps.

## 9. Agent lifecycle and `agents.status`

| Status      | In catalog? | WS handshake?       | Reachable? | How to enter        | How to leave                              |
| ----------- | ----------- | ------------------- | ---------- | ------------------- | ----------------------------------------- |
| `pending`   | no          | reject (not active) | no         | (reserved — unused) | manual                                    |
| `active`    | yes         | accept              | yes        | onboarding / unsuspend | suspend / evict                        |
| `suspended` | no          | reject              | no         | admin suspend       | admin unsuspend                           |
| `removed`   | no          | reject              | no         | admin evict         | terminal for that agent; the `agent_id` is freed for a NEW agent |

Admin endpoints driving the transitions:
`POST /v1/admin/agents/{id}/suspend` (active → suspended),
`POST /v1/admin/agents/{id}/unsuspend` (suspended → active),
`POST /v1/admin/agents/{id}/evict` (active|suspended → removed).
All three push a `CatalogUpdate` to remaining peers and emit
`agent.{suspended,unsuspended,evicted}` audit events.

`removed` is the eviction sentinel — terminal for *that agent instance*
(it never serves again). Eviction **renames the row's PK to a tombstone**
(`deleted_<id>_<epoch>`), and the co-located service principal
(`usr_service_<id>`) the same way, so the original `agent_id` is **freed for
a brand-new agent to onboard**. The row and all its `tasks`/`audit` history
are preserved under the tombstone id via FK `ON UPDATE CASCADE` (migration
`0002_fk_on_update_cascade`); an `agent.id_released` audit event records the
mapping. The freed id is reusable only via a fresh admin invitation
(onboarding still requires one), so it is never *silently* re-pointed.
Re-onboarding with an `agent_id` whose row exists with status `≠ 'pending'`
returns HTTP 409.

## 10. AgentInfo identity

With this model, `AgentInfo` carries *identity only* — the data the
rules match against:

```python
class AgentInfo(BaseModel):
    agent_id: str
    description: str
    groups: list[str]                # what this agent belongs to
    capabilities: list[str]          # what this agent provides
    accepts_schema: dict | None      # JSON schema for NewTask.payload
    produces_schema: dict | None
    documentation_url: str | None
    hidden: bool = False             # SDK-side: suppress from tool schemas
```

Dropped from the previous design:

- `tags` — replaced by `groups`.
- `requires_capabilities` — no longer used by any code path.
- `min_role`, `min_tier`, `min_user_level` — expressible via rules.
- `visible` patterns — admins own ACL; agents don't declare it.

`AgentInfo` is set at onboarding, frozen for the agent's WS lifespan,
and only mutable by an admin (see §11). The agent cannot edit its
own groups or capabilities mid-session.

## 11. Identifier grammar

| Identifier   | Regex                              |
| ------------ | ---------------------------------- |
| `agent_id`   | `[A-Za-z_][A-Za-z0-9_-]{0,63}`     |
| group name   | `[a-z][a-z0-9_:.-]{0,63}`          |
| capability   | `[a-z][a-z0-9_]*(\.[a-z0-9_]+)+`   |
| user_level   | `\*\|admin\|service\|tier[0-9]+`   |

Group names may contain `:` (so deployments can keep `team:coding`
style conventions) but never `/` or `*`. Capabilities are dotted
ASCII. All three are validated at the boundary — `AgentInfo`
Pydantic validators on insert, rule-pattern parser on
`POST /v1/admin/acl/rules`.

## 12. Admin API

All endpoints below take `Depends(require_admin)`.

```
GET    /v1/admin/acl/rules                          → ordered list
PUT    /v1/admin/acl/rules                          → atomic full replace
POST   /v1/admin/acl/rules                          → insert at ord
PATCH  /v1/admin/acl/rules/{rule_id}                → edit one field
DELETE /v1/admin/acl/rules/{rule_id}                → remove one
POST   /v1/admin/acl/rules/reorder                  → { rule_id: ord, ... }
POST   /v1/admin/acl/rules/simulate                 → { caller_id, callee_id, user_level }
                                                      → { allow, rule_name, evaluation_trace }
```

`simulate` returns the evaluation trace (which rules were skipped
and why, which one matched) — needed for admins to understand why a
given decision was reached without having to do it in their head.

Every write emits an audit event:

| Event                     | Payload                              |
| ------------------------- | ------------------------------------ |
| `acl.rule_added`          | full rule body                       |
| `acl.rule_updated`        | rule_id, changed fields              |
| `acl.rule_removed`        | rule_id, original body               |
| `acl.rules_reordered`     | { rule_id: (old_ord, new_ord), … }   |
| `acl.rules_replaced`      | rule_count                           |

### 12.1 Concurrency caveat

The router does **not** serialize concurrent admin rule edits. Each
mutation endpoint runs the same sequence — persist to `acl_rules`,
reload the in-memory `RuleSet` from the DB, push `CatalogUpdate` to
live agents — but two admins editing the rule list at the same time
can interleave at any point in that sequence.

What can happen in a contended window:

- Admin A's persist completes, then admin B's persist completes,
  then both reload the in-memory cache. The final state is
  consistent with the last DB write, but A's audit-event payload no
  longer matches the on-disk truth at the moment A logged it.
- Between A's persist and A's hot-reload, B's `simulate` reads a
  rule list that includes A's change in the DB but not yet in
  memory. Admit-time decisions during the same sub-second window
  use the stale in-memory snapshot.
- Catalog pushes from A and B may arrive at agents in either order.

For correctness this is fine — the DB is authoritative, the
in-memory state converges, and admit-time decisions always read the
live (post-write) cache, not the audit-event payload. For audit
*precision* it's not: the recorded payload of A's edit is what A
intended, not what the post-B world looks like.

If your deployment runs many concurrent admin edits (rare in
practice — admin actions are human-frequency), wrap the admin UI in
a deployment-level lock or queue. The router itself will not grow a
mutex; rule edits are not on a hot path that would justify it.

A fresh database — after `alembic upgrade head` — comes with three
seed rules:

```
ord  effect  user_level  caller       callee
0    allow   *           admin/*  ->  admin/*
1    deny    *           */*      ->  admin/*
2    allow   *           */*      ->  */*
```

The first two protect the reserved `admin` group. The router
ships with one built-in agent in that group — `admin_console`,
the synthetic caller for `POST /v1/admin/tasks/test` — and admins
can add more (e.g. operational tooling agents) by putting them in
`groups: ["admin"]`. The deny at ord 1 stops anyone outside the
admin group from invoking those agents; the allow at ord 0 lets
admin agents call each other (e.g. an admin orchestrator invoking
`admin_console` for diagnostics).

The third row at ord 2 is the deliberately permissive default —
"everything else is allowed." Admins replace it with real policy
as they go. The bootstrap is inserted by the same migration that
creates the table, so deployments that don't write any ACL config
still have an evaluable rule list.

There is no `acl.yaml` file. The DB is the only source of truth.
On router startup, `lifespan` loads rules from `acl_rules` into the
in-memory evaluator; admin writes both persist to the table and
hot-swap the evaluator.

### 13.1 The `admin` group convention

`admin` is a reserved group name in the bootstrap policy: members
are protected from external invocation by the deny rule at ord 1.
Putting an agent in `groups: ["admin"]` means "callable only by
other admin-group agents, plus the admin-driven endpoints that use
`admin_console` as their synthetic caller." Production deployments
that tighten the ord-2 rule should preserve the ord-0 / ord-1 pair
so the admin test endpoint keeps working.

## 14. Schema

```sql
CREATE TABLE acl_rules (
    rule_id         text PRIMARY KEY,
    ord             int  NOT NULL UNIQUE,
    name            text,
    description     text,
    effect          text NOT NULL CHECK (effect IN ('allow','deny')),
    user_level      text NOT NULL
                    CHECK (user_level ~ '^(\*|admin|service|tier[0-9]+)$'),
    caller_pattern  text NOT NULL
                    CHECK (caller_pattern ~ '<see migration 0001 for the full pattern regex>'),
    callee_pattern  text NOT NULL
                    CHECK (callee_pattern ~ '<see migration 0001 for the full pattern regex>'),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text REFERENCES users(user_id)
);
CREATE INDEX acl_rules_ord_idx ON acl_rules(ord);
```

`created_by` is nullable — the bootstrap row inserted by migration
0001 has no creator. CHECK constraints on `user_level` and the two
pattern columns enforce the grammars in §3 and §11; an invalid row
cannot exist on disk.

## 15. Observability

Every evaluated rule increments a counter:

```
router_acl_decisions_total{decision, effect, rule_name}
```

Where:
- `decision` is `visibility` or `permission`. Both are now emitted
  (R5 — earlier the visibility path bypassed the metric):
    * `permission` from admit-time ACL checks in
      `is_allowed_for` (`bp_router/api/admin.py:admit_task`).
    * `visibility` from catalog-construction probes in
      `compute_callable_user_levels` (used by Welcome-frame
      assembly). To keep series cardinality bounded the
      visibility path uses a synthetic `rule_name="<batch>"` —
      operators get the rate of show/hide/default-deny outcomes
      but not which specific rule produced each one.
- `effect` is `allow`, `deny`, or `default_deny` (terminal fall-through).
- `rule_name` is the matched rule's `name`, or `<self_call>` /
  `<default>` for synthetic outcomes; or `<batch>` for visibility
  probes regardless of which rule matched. The synthetic labels
  (`<self_call>`, `<default>`) match the `Decision.rule_name`
  field and the simulate trace step's `rule_name` byte-for-byte,
  so operators can correlate the three views without a renaming
  table.

A persistent deny rate from a specific caller against a specific
callee is the strongest signal that the rule list has a gap.
Recommended dashboards (see [`observability.md`](./observability.md)):

- Top 10 (caller_id, callee_id) pairs by deny count.
- Rate of `default_deny` outcomes — high values mean an admin should
  add an explicit rule (allow or deny) to make intent visible.
- Rule-firing distribution — rules with zero hits are dead and
  candidates for removal.

## 16. Pitfalls

- **Rule order drift.** Inserting a new rule near the top reorders
  evaluation. Use the simulate endpoint after every edit to
  re-validate intent.
- **`*` in `user_level` vs `*` in pattern halves.** Both mean "any"
  but they live in different namespaces. The rule
  `allow * */* -> */*` is universal-allow; `allow tier0 */* -> */*`
  is "any session at tier0 or stricter, any caller, any callee."
- **Generous catalog can mislead.** A catalog entry with
  `callable_user_levels: ["admin"]` shows up at handshake but is
  invocable only by admin sessions. The SDK filter handles this; an
  admin manually inspecting the catalog must read the levels.
- **Default deny is silent.** A blocked call without a matching deny
  rule reports `<default>` as the rule name, which can be hard to
  reason about. Prefer writing an explicit terminal `deny *` line
  for any class of agent the policy meant to deny — the audit log
  then attributes the denial to a named rule.
- **`agent_id` literals tie rules to specific agents.** Replacing an
  agent (different `agent_id`) breaks every `@<id>` rule pointing
  at the old one. Group/capability patterns are the durable choice;
  `@<id>` is for one-off exceptions.

## 17. Worked example: a small deployment

Three agents:

```
orchestrator     groups=[rank0]                    capabilities=[]
gemini_main      groups=[rank1, provider:gemini]   capabilities=[llm.generate.text, llm.generate.image]
test_writer      groups=[rank2, team:coding]       capabilities=[code.write_tests]
```

A reasonable rule list:

```
ord  effect  level     caller            callee
1    deny    *         */*           ->  @experimental_agent
2    allow   *         rank0/*       ->  rank1/llm.generate.text
3    deny    tier3     */*           ->  rank1/llm.generate.image
4    allow   *         rank0/*       ->  rank1/llm.generate.image
5    allow   *         team:coding/* ->  team:coding/*
6    deny    *         */*           ->  */*
```

Walking a few queries:

| Query                                                         | Result    | Reason          |
| ------------------------------------------------------------- | --------- | --------------- |
| `(orchestrator → gemini_main, llm.generate.text)`, tier1       | allow     | rule 2          |
| `(orchestrator → gemini_main, llm.generate.image)`, tier1      | allow     | rule 4          |
| `(orchestrator → gemini_main, llm.generate.image)`, tier3      | deny      | rule 3          |
| `(orchestrator → test_writer)`, tier1                          | deny      | rule 6 (terminal) |
| `(test_writer → gemini_main)`, tier0                           | deny      | rule 6 (terminal) |
| `(orchestrator → @experimental_agent)`, admin                  | deny      | rule 1          |

Note that rule 6 is the explicit terminal deny — it makes the
denial attributable in the audit log instead of falling through to
the implicit `<default>`. Recommended for any production
deployment.
