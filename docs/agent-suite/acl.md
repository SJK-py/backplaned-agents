# Agent Suite — Capabilities & ACL

> The suite's access-control rules, expressed in Backplaned's ACL grammar
> (`docs/acl.md`). Read with [`agents.md`](./agents.md) (which lists each
> agent's capabilities) and [`overview.md`](./overview.md).

## 1. Two kinds of capability

A capability string is either **routed** or a **marker**. Only routed caps need ACL rules.

- **Routed** (matched by some rule's caller/callee pattern):
  `agent.orchestration`, `assistant.*`, `memory.add`, `memory.retrieval`,
  `summarize.history`, `user.config`, `database.*`, `document.convert`,
  `web.fetch`, `web.convert`, `computer.*`, `channel.*`.
- **Markers** (authorization enforced elsewhere — **no ACL rule**):
  - `file.full` → the router's file store (gated by task-derived identity + active-executor, not ACL).
  - `session.history`, `session.management` → the suite's Postgres (direct, shared-DB access).
  - `user.auth`, `user.registration`, `user.cron` → HTTP admin endpoints.
  - `llm.generation.text`, `llm.multimodal.image`, `agent.delegation` → descriptive.

> **Key invariant:** the router ACL gates **agent reachability**, not the mode. Reaching an agent permits *any* of its modes (including `tool=false` ones). Design the trust boundary at the agent level; use `hidden=true` to keep an agent out of tool catalogs.

## 2. Rule grammar (recap)

`<effect> <user_level> <caller_pattern> -> <callee_pattern>`, pattern slots
`*/*`, `<group>/*`, `*/<cap>`, `<group>/<cap>`, `*/<prefix>.*`,
`<group>/<prefix>.*`, `@<agent_id>`. Deny-by-default. An allow-only list is
order-independent; any added `deny` is order-sensitive (place above allows).

## 3. The rule set (deny-by-default — replaces the bootstrap allow-all)

```
# Orchestration spine
allow * l0/*                    -> l1/*                    # orchestrator → l1 (subagent + delegation hand-off)
allow * l1/agent.orchestration  -> l1/*                    # deep_reasoning → l1 specialists (plan steps)
allow * l1/*                    -> l0/agent.orchestration  # l1 → orchestrator (execute_step + end_delegation)

# Channel ↔ agents
allow * channel/*               -> l0/*                    # user message → orchestrator
allow * channel/*               -> l1/*                    # user message → delegated agent (delegated_message)
allow * l0/*                    -> channel/*               # orchestrator → channel (message_to_user / file_to_user)
allow * l1/*                    -> channel/*               # delegated agent → channel (interim push)

# Memory (recall vs write)
allow * */assistant.*           -> l3/memory.retrieval     # assistant agents recall mid-loop
allow * channel/*               -> l3/memory.add           # channel add after a turn + webapp Memory page

# Knowledge base (webapp page)
allow * channel/*               -> l3/database.*           # webapp Knowledge base page browse/delete

# Summarization
allow * channel/*               -> l3/summarize.history    # channel (session manager) fires the summarizer

# User config
allow * l0/*                    -> l2/user.config          # orchestrator drives conversational config changes
allow * channel/*               -> l2/user.config          # webapp config UI (v2)

# Infra + converters
allow * */computer.*            -> infra/computer.*        # computer_use → sandbox
allow * */database.*            -> l3/database.*           # research → knowledge_base
allow * */document.*            -> */document.*            # knowledge_base / research → md_converter (file→md)
allow * */web.fetch             -> */web.convert           # research webpage → md_converter
```

## 4. Why each rule (and the flows it enables)

| Rule | Enables |
| --- | --- |
| `l0/* -> l1/*` | orchestrator spawns l1 subagents; delegates the session to an l1 (hand-off) |
| `l1/agent.orchestration -> l1/*` | deep_reasoning calls l1 specialists directly |
| `l1/* -> l0/agent.orchestration` | deep_reasoning `execute_step` → `orchestrator(subagent)`; every delegate's `end_delegation` → `orchestrator(end_delegation)` |
| `channel/* -> l0/*` / `-> l1/*` | user-message dispatch (orchestrator normally; the delegate during delegation) |
| `l0/* -> channel/*` / `l1/* -> channel/*` | pushing to the user (`message_to_user`, `file_to_user`) — though most interim output rides `ctx.progress` |
| `*/assistant.* -> l3/memory.retrieval` | orchestrator / computer_use / research / deep_reasoning recall facts |
| `channel/* -> l3/memory.add` | the channel fires `memory.add` post-turn (it has no `assistant.*`, so this dedicated rule is required) |
| `channel/* -> l3/summarize.history` | the channel (session manager) triggers rolling summarization |
| `l0/* -> l2/user.config` | the orchestrator applies conversational config changes |
| `*/computer.* -> infra/computer.*` | computer_use → sandbox |
| `*/database.* -> l3/database.*` | research → knowledge_base |
| `*/document.* -> */document.*` | knowledge_base (store conversion) and research (`document.convert`) → md_converter |
| `*/web.fetch -> */web.convert` | research's `html_fetch` → `md_converter.webpage` |

## 5. Tier gating (optional, later)

All rules use `user_level=*`, evaluated against the **end-user's** session level (per [overview §2.1](./overview.md) — *not* the channel's `service` level). To restrict a premium agent to higher tiers, add **deny** rules **above** the allows, e.g.:

```
deny  tier0  */*  ->  @deep_reasoning      # free tier can't reach deep_reasoning
```

## 6. Capability deltas from the original draft

- `research`: `database.search` → `database.retrieval`; **+** `document.convert`.
- `knowledge_base`: group `infra` → **`l3`** (so the callee pattern is `l3/database.*`).
- `chatbot` / `webapp`: **+** `session.management` (they are the session managers).
- `orchestrator`: **−** `session.management` (keeps `session.history`).
