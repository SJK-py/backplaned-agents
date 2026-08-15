# Router-resolved preset slots

> **Status: implemented**, both halves. Router: `LlmRequestFrame.preset_slot`
> + `LlmResultFrame.resolved_preset` / `preset_downgraded`,
> `LlmService.resolve_slot`, `user_llm_preferences` (in the consolidated
> `0001_initial_schema`, shipped as `0011_user_llm_preferences`),
> `Settings.llm_default_presets`, the `/v1/llm/presets` +
> `/v1/llm/preferences` endpoints, `ctx.llm.generate(slot=…)`. Suite (step 7):
> `bp_agents/slots.py`, `run_llm_loop(slot=…)`, every agent call site,
> the webapp Models pane, and the suite's `user_config.preset_*` drop
> (shipped as `0004_drop_user_config_presets`).
> Covered by `tests/test_preset_slots.py` + `tests/test_webapp_phase5.py`.
> Carries three `[shipped]` deviations: §5.1 (where selection lives), §8.1
> (the vision sidecar's engagement decision), §8.2 (a delegation race the
> cutover exposed).
>
> Companion to [`router-managed-session-store.md`](./router-managed-session-store.md),
> which moved conversation into the router and left `user_config` — including
> the four `preset_*` columns — as the last per-user state in `bp_suite`. This
> covers the part of that move the router must *interpret* rather than merely
> store: which model a user's turn runs on.

Today the router owns the **ceiling** (a preset's `min_user_level`) and the
suite owns the **choice** (`user_config.preset_balanced`). Neither knows the
other exists. The consequence is a selection menu that can offer a user a
model they are not entitled to, a refusal that lands mid-conversation instead
of at selection, and two lookups per turn to make one decision.

This makes the two halves one decision, resolved where both facts already
live, without teaching the router what "balanced" means.

## 1. How it works today

### 1.1 The router's gate — a per-user ceiling

Each preset carries `min_user_level` (default `*`). The comparison is
`principals.user_level_satisfies` (`bp_router/principals.py:54`), shared with
the ACL evaluator so there is one grammar: `*` admits anyone, `admin` and
`service` are exact matches, `tierN` means "this tier or stricter" (lower
number is more privileged).

`LlmService._resolve` (`llm/service.py:695-718`) checks the gate on the
*requested* preset and raises `PresetNotAllowedError` when it fails. Gated
presets reachable through a `fallback_preset` chain are treated differently
on purpose: an inaccessible fallback is skipped silently, so an operator can
mix permissive and restricted presets in one chain, and a user is never
*upgraded* onto a preset they did not ask for (`service.py:251-257`).

The caller's level is TTL-cached for 60 s, LRU-capped at 5000 entries, and
invalidated on admin level change and suspension. Two properties of the hot
path are already right and this design must preserve them:

  * **Identity is derived, not asserted.** The level comes from the task row
    via `_derive_task_scope`, never `frame.user_id` — otherwise a low-trust
    agent could name a privileged user to satisfy a gate and bill that tenant
    (`dispatch.py:669-685`).
  * **Ungated calls cost no lookup.** `chain_needs_tier` walks the fallback
    chain and skips the level fetch entirely when nothing in it is gated.

### 1.2 The suite's selection — a per-user default

> *State before this design. Both halves are gone now: the presets moved to
> `user_llm_preferences` (§8), and `config_edit.py` was folded into
> `bp_agents/user_prefs.py` when the remaining settings moved into the
> router's user scope
> ([`router-managed-session-store.md` §13.5](./router-managed-session-store.md)).*

`user_config.preset_{pro,balanced,lite,embedding}` holds preset *names*.
Whether a user may change one is governed by
`SuiteSettings.selectable_presets_{pro,balanced,lite}` — a **static,
operator-wide allow-list**: empty means the slot is system-managed, non-empty
lists the names a user may pick. `config_edit.py` enforces it for the chat
path and `webapp/pages/config.py` renders the same list as a form.

Every agent reads `user_config` at turn start and passes an explicit preset
name on each `LlmRequest` (`orchestrator/agent.py:156` and 27 sibling call
sites).

## 2. What is actually wrong

**2.1 The menu is not the entitlement.** One global list versus a per-user
gate. The suite's own docstring concedes the coupling is manual: *"Only list
presets the router has actually seeded and that suit your users' level — the
router still enforces each preset's `min_user_level` at call time as a
backstop."* In a deployment with both tier1 and tier3 users, no single list is
correct for both. And the suite cannot compute the right list, because preset
enumeration is admin-only (`GET /v1/admin/llm/presets`, `require_admin`) — so
the selection surface is a hand-maintained duplicate of policy that lives in
the router.

**2.2 Refusal lands at the worst possible moment.** Selecting an
unentitled preset succeeds. The failure arrives on the *next turn* as
`LLM_PRESET_NOT_ALLOWED` (`dispatch.py:1041`) — after the user sent a message,
after the input row was written — and it is not retriable: every subsequent
turn fails identically until someone changes the config. A refusal belongs at
selection, where the user is already making a decision and can be told why.

**2.3 Two lookups, one decision.** Each turn the agent reads `user_config` to
turn "balanced" into a name; the router then resolves the user's level to
check that name. Worse, the *name* is what crosses the wire, so the router
cannot distinguish "the user chose this" from "the agent hardcoded this" —
exactly the distinction any graceful-degradation policy needs.

**2.4 Nothing survives a demotion.** Drop a user from tier1 to tier3 and their
stored `preset_pro` silently becomes a landmine: correct in the database,
refused on every call. There is no path that re-resolves it.

## 3. The model: slots

A **slot** is an opaque per-user key naming a preference — `"balanced"`,
`"pro"`, `"lite"`, or anything a future suite invents. The router resolves a
slot to a preset; it never interprets the slot's name.

**Embedding is not a slot** (§12) — it is the one model choice that is not
safe to change, so it stays operator configuration on the existing explicit
`preset=` path.

This is the same discipline the session store applies to `role` and
`item_kind`: the platform stores and dispatches on the key, and the meaning
lives with the caller. "Balanced" gets its meaning from operator
configuration, not from router code.

```
LlmRequest(preset_slot="balanced")
        │
        ├─ user preference for "balanced"?  ──yes──▶ passes the gate? ──yes──▶ USE IT
        │                                                   │
        │                                                   └──no──▶ slot default (flagged)
        └─ no preference ──▶ slot default ──▶ passes the gate? ──no──▶ preset_not_allowed
```

### 3.1 Resolution order

1. **User preference** for the slot, if set and it satisfies the gate → use it.
2. **Preference set but gate-failing** → fall back to the slot default and set
   `preset_downgraded` in the result metadata. This is a deliberate departure
   from the explicit-`preset=` path, which hard-refuses. The request "give me
   the balanced model" is satisfiable by degrading; "give me `claude-opus`" is
   not. Without this, §2.4's demotion wedges every turn the user takes.
3. **No preference** → the operator's slot default.
4. **Slot default itself gate-failing** → `preset_not_allowed`. An operator
   has configured a default the user cannot reach; failing closed is right,
   and it is a configuration error worth surfacing.
5. **Unknown slot** → `preset_slot_unknown`.

Explicit `preset=` keeps working unchanged, including its hard refusal —
agents that must pin a specific model (the multimodal sidecar, an eval
harness) still can. `preset_slot` and `preset` are mutually exclusive.

### 3.2 Where slot defaults live

A new router setting, `llm_default_presets: dict[str, str]`, mapping slot →
preset name. It is operator configuration, validated at startup against the
loaded preset map (unknown target → startup error, same posture as the
fallback-cycle check). The suite's `SuiteSettings.default_preset_*` fields
collapse into it.

## 4. Storage: preferences need their own trust boundary

The shipped session store already offers a per-user opaque KV — user-scoped
`session_state` with `owner_agent_id IS NULL`, verified working cross-agent
with CAS and surviving session purge. Most of `user_config` should go there:
`custom_note`, `verbose`, `language`, `sandbox_uid`,
`max_context_token_limit`, `default_session_id`. The router stores those and
interprets none of them.

**Preset preferences must not.** That namespace is writable by any agent
acting in the user's session, and the router would be *acting* on the value —
choosing which model, at which cost, under which tier gate. A value the router
enforces policy on cannot be one any agent can overwrite. So:

```sql
user_llm_preferences (
    user_id     text  NOT NULL REFERENCES users ON DELETE CASCADE,
    slot        text  NOT NULL,
    preset_name text  NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, slot)
)
```

One table, written **only** through the session-JWT endpoints of §5 — the
user's own authority over their own preference — and read by the router. No
agent-facing write path exists, which is the same "remove it rather than gate
it" move the session store made with its message-POST endpoint.

The distinction to hold onto: `session_state` is for values the router
*stores*; this table is for the one value the router *obeys*.

## 5. Endpoints

Two new session-JWT endpoints replace the static allow-list:

| endpoint | purpose |
| --- | --- |
| `GET /v1/llm/presets` | presets **this caller's level satisfies**, each with name, description, and the slots it is the default for |
| `GET \| PUT /v1/llm/preferences` | the caller's slot → preset map; `PUT` validates against the gate |

`GET /v1/llm/presets` is the piece that makes the menu correct by
construction: it filters with the same `user_level_satisfies` the call path
uses, so a user is never offered what they cannot run.
`SuiteSettings.selectable_presets_*` disappears — an operator no longer
hand-maintains a list that duplicates `min_user_level`.

`PUT /v1/llm/preferences` re-checks the gate and returns
`403 preset_not_allowed` with the required level, moving §2.2's refusal from
mid-turn to selection time. It deliberately does **not** consult the operator
allow-list, because there no longer is one: entitlement is the allow-list.

The admin view (`GET /v1/admin/llm/presets`, with key refs and provider
detail) stays exactly as it is. The new endpoint is a projection for end
users: name, description, slot defaults — never credentials.

### 5.1 `[shipped]` Selection lives in the webapp, not in chat

§8 below says "the config agent now validates through the router". It
cannot, and §4/§9 are why: both endpoints require a **session JWT**, and an
agent does not hold one. Deliberately — the whole argument for a separate
table is that a value the router *acts* on must not be writable by anything
acting merely on the user's behalf. Minting a user token for the config
agent would hand it exactly the authority the design removes.

So model selection lives on the **webapp Settings page**, the only suite
process holding the user's own access token, as a Models pane: one `<select>`
per slot from `GET /v1/llm/presets`, one `PUT /v1/llm/preferences` per save,
with the router's 403 rendered as a message beside the field. The config
agent keeps every other `user_config` field and, asked about models, says
where the switch is rather than guessing at a value it cannot read.

This does narrow the README's *"a user swaps Gemini↔Claude from chat — no
redeploy"* to *"from Settings"*. What it buys is worth more than the
sentence: the menu is filtered by entitlement instead of by a hand-kept
list, and the refusal lands at selection time (§2.2) instead of on the next
message. A chat path could be restored later through the **channel** rather
than an agent — a chatbot gateway already mints per-user tokens under its
`serviced_by` rights, so a `/model` command would carry the user's own
authority. Not built: it would need the same pane's logic in two gateways,
and Settings covers it.

## 6. Protocol

`LlmRequestFrame` gains one field:

```python
    # Resolve the preset from the caller's per-user preference for this
    # SLOT (an opaque key — the router never interprets its name), falling
    # back to the operator's slot default. Mutually exclusive with
    # `preset`; when both are set the request is rejected rather than
    # silently preferring one.
    preset_slot: str | None = None
```

`LlmResultFrame` gains two, both additive and defaulted:

```python
    resolved_preset: str | None = None   # what actually ran
    preset_downgraded: bool = False      # §3.1 case 2 — preference was un-entitled
```

`resolved_preset` closes a real observability gap that exists today: with slot
resolution the agent no longer knows which model answered unless the router
says so. `preset_downgraded` is what lets a suite tell the user *once* —
"your saved model is no longer available on your plan, using X" — rather than
silently changing behaviour or failing every turn.

SDK: `ctx.llm.generate(..., slot="balanced")`, alongside the existing
`preset=` and legacy `model=`.

## 7. Efficiency

The resolution is free on the hot path because it rides a cache that already
exists. `_UserLevelCacheEntry` currently holds `(level, expires_at)`; it grows
to hold the slot map, so one fetch serves both the gate and the preference,
under the same 60 s TTL and LRU cap. Invalidation gains one trigger
(preference write) beside the two it has (level change, suspension).

Per turn, per agent:

| | today | after |
| --- | --- | --- |
| suite DB read of `user_config` for model choice | 1 | 0 |
| router user-level fetch | 1 per gated call, 60 s TTL | same entry, now carrying preferences — **no extra fetch** |
| what crosses the wire | a preset name | a slot key |
| who can tell "user chose" from "agent hardcoded" | nobody | the router |

The `chain_needs_tier` fast path needs one adjustment: a slot request always
needs the cache entry (to read the preference), but a *cache hit* is not a DB
hit, so the ungated-preset optimisation survives for explicit `preset=` calls
and costs a slot call nothing beyond the lookup it already needs.

## 8. What this deletes

  * `SuiteSettings.selectable_presets_{pro,balanced,lite}` and the
    `preset_choices` machinery in `bp_agents/config_edit.py` (since folded
    into `bp_agents/user_prefs.py`) and `webapp/pages/config.py` — replaced
    by `GET /v1/llm/presets`.
  * `user_config.preset_{pro,balanced,lite}` — replaced by
    `user_llm_preferences`. `preset_embedding` is deleted outright rather
    than migrated: it was never user-selectable, and it must not become so
    (§12). The embedding model stays operator config, passed as an explicit
    `preset=` by the agents that embed.
  * `SuiteSettings.default_preset_*` — replaced by `llm_default_presets`.
  * The per-turn `get_user_config` read whose main job was resolving a preset
    name (28 call sites; the remaining fields move to user-scoped
    `session_state`).

And it improves the feature the README advertises — *"a user swaps
Gemini↔Claude from chat — no redeploy"*. The selection surface now validates
through the router, so an unentitled request is refused **at selection**,
with the required level in hand, instead of being accepted and then failing
on the next turn. (Which surface, exactly: see §5.1.)

### 8.1 `[shipped]` The vision sidecar decides per response

The sidecar engages when the turn's preset is declared text-only
([`multimodal-vision-sidecar.md`] §3.1) — but with a slot, the agent cannot
know the resolved preset before the call. `multimodal_preset_for` (which
computed it agent-side) is deleted; `run_llm_loop` now takes the configured
vision preset plus the operator's `text_only_presets` and decides per
response from `resolved_preset`, before dispatching that round's tool calls.

One honest cost: the tool SPEC — whether `read_file` advertises the optional
`purpose` arg — is still fixed before round one, so a slot caller advertises
it whenever a vision preset is configured, including on turns that resolve to
a multimodal preset. An unused optional argument is a far smaller cost than
either alternative (feeding an image to a text-only model, or a pre-flight
round trip just to learn the preset).

### 8.2 `[shipped]` The cutover exposed a delegation race

The first hosted run of a slot-based hand-off failed sporadically with
`preset_not_allowed: caller could not be verified for slot resolution`. The
cause was not slots. `_admit_delegation` flipped `tasks.active_agent_id` to
the delegate **after** awaiting its ack — and the SDK acks, then starts the
handler. For the width of the router's own commit, the delegate is executing
while the router still records the caller as active, so everything derived
from that column via `attachments.derive_task_file_scope` refuses it: preset
slots, tier-gated presets, named-file operations, and `SessionOp` — on the
delegate's opening turn, which is precisely when a hand-off does its work.

Slot resolution only made it *visible*: before, the common path used an
ungated preset, which skips the identity derivation entirely.

The fix flips inside the same short-lived transaction that validates, then
delivers, then unwinds with a guarded flip-back if the destination
disconnects, times out, or refuses. That is the shape `admit_task` has always
used for a fresh task (insert with the destination active, force-fail on
rejection); delegation was the inconsistent one. It also improves the case
the old ordering called out as an accepted loss: a cancel arriving during the
ack window now finds the delegate and cancels it, instead of leaving an
orphaned execution for the deadline sweep. Pinned by
`tests/test_review_delegation_pool_release.py`.

## 9. Security

  * **Identity stays derived.** Slot resolution uses the same task-derived
    `user_id` the gate uses (`dispatch.py:669-685`). A preference is looked up
    for the *trusted* user, never an asserted one — otherwise an agent could
    borrow another tenant's model entitlement by naming them.
  * **No agent-facing write path.** Preferences are written only under a
    session JWT. An agent can *read* the effective resolution (implicitly, by
    making a call); it cannot change what a user is entitled to or prefers.
  * **The gate remains the authority.** A preference is a filter *inside* the
    ceiling, never a way past it. Every path — call, `PUT`, and listing —
    checks `user_level_satisfies`, and the call path checks it again even for
    a preference that passed at write time, because levels change.
  * **Downgrade is visible, not silent.** §3.1 case 2 reports
    `preset_downgraded` rather than quietly running a different model; a suite
    that ignores the flag degrades safely, one that reads it can explain.
  * **No credential exposure.** The user-facing projection carries name,
    description, and slot defaults — never `api_key`, `api_key_ref`, or
    `base_url`.

## 10. Implementation sequence

1. `user_llm_preferences` table + migration; `llm_default_presets` setting
   with startup validation against the preset map.
2. Extend `_UserLevelCacheEntry` to carry the slot map; one fetch, one
   invalidation path.
3. `LlmService.resolve_slot(user_id, slot) -> (preset_name, downgraded)`
   implementing §3.1, and its use in the `generate` / `embed` /
   `count_tokens` entry points.
4. `LlmRequestFrame.preset_slot` + `LlmResultFrame.resolved_preset` /
   `preset_downgraded`; mutual-exclusion validation; dispatch wiring.
5. `GET /v1/llm/presets`, `GET|PUT /v1/llm/preferences`.
6. SDK `slot=` parameter.
7. Suite cutover (§8) — part of the suite rework, not this PR.

Steps 1–6 are additive: no existing frame field, endpoint, or preset row
changes shape, and an agent that never sends `preset_slot` sees identical
behaviour.

## 11. What not to do

  * **Don't let the router interpret slot names.** No `Literal["pro",
    "balanced", "lite"]`, no per-slot code paths. The moment "balanced" means
    something in router code, the platform owns one suite's model taxonomy.
  * **Don't put preferences in `session_state`.** It is agent-writable, and
    this is the one per-user value the router enforces policy on (§4).
  * **Don't hard-refuse a gate-failing preference.** That is §2.4 with extra
    steps — a demoted user's every turn fails. Degrade and flag.
  * **Don't degrade an explicit `preset=`.** "Never upgrade a user onto a
    preset they didn't ask for" is the existing rule and it still holds; a
    named preset is a specific request, and quietly running a different model
    would break the callers that pin one deliberately.
  * **Don't keep `selectable_presets_*` "as a safety net".** Two sources of
    truth for entitlement is the bug being fixed.

## 12. Open questions

  * **Per-slot tier floors.** An operator may want "the `pro` slot requires
    tier1" independent of any individual preset's gate. Expressible as a
    `min_user_level` on the slot default map. Left out of v1 — no caller wants
    it yet, and preset-level gating covers the known cases.
  * ~~**Should `embedding` be a slot?**~~ **Decided: no.** Changing an
    embedding model invalidates every vector already written — the per-user
    LanceDB stores would silently return garbage similarity against vectors
    from a different model, with no error and no migration path. It stays
    operator configuration (`llm_default_presets` may name it, but it is not
    resolvable per user and never appears in `GET /v1/llm/presets`). A slot
    is for a choice that is safe to change between turns; this one is not
    safe to change at all without a re-embed.
  * **Notifying a downgrade exactly once.** `preset_downgraded` fires on every
    call until the user re-picks. Suppressing repeats is suite policy (a flag
    in user-scoped `session_state`), but it is worth confirming that is where
    it belongs rather than a router-side "notified_at".
  * **Cost visibility.** Once slots exist, "what will this cost me" becomes
    answerable per slot. Out of scope here, but the listing endpoint is the
    natural place for a future price hint.
