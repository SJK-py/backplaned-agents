# Agent Suite — Agents

> Per-agent specification: group, capabilities, modes, and behavior. Read
> [`overview.md`](./overview.md) first for the foundations. Cross-cutting
> mechanics live in [`acl.md`](./acl.md), [`sessions.md`](./sessions.md),
> [`delegation.md`](./delegation.md), [`cron.md`](./cron.md),
> [`memory.md`](./memory.md).

## Conventions

- **Output:** every mode returns `AgentOutput(content, files)` (see [overview §6](./overview.md)). No bespoke `produces_schema`.
- **Payload kinds:** `message` modes take `{prompt: str}` (bare user input — the agent builds its own system prompt from config + history); `subagent`/`on_delegation` take `LLMData` (caller passes instruction + context + prompt); the rest take a structured payload.
- **Modes vs local tools:** only *inter-agent* entry points are **modes** (with `accepts_schema`). An agent's *internal* tools (deep_reasoning's `plan_mode`, the `file_tools`, research's web tools, config's editing toolset) live inside its own loop and are **not** modes.
- **`tool=false`** ⇒ in `non_tool_modes` (dispatchable, not advertised to tool-builders). **`hidden=true`** ⇒ the whole agent is absent from every tool catalog (still dispatchable by explicit `(agent, mode)`). ACL gates the **agent**, not the mode.
- **`current_time`:** every l0/l1 agent carries a local `current_time` tool — user turns are stored without a timestamp, so the wall clock is read on demand ([sessions.md](./sessions.md)).

---

## orchestrator — group l0, **hidden=true**

The personal assistant and session hub. Hidden because it's reached only by handcrafted paths (channel dispatch, l1 `end_delegation`/`execute_step`) — never as an LLM tool.

**Capabilities:** `agent.orchestration`, `agent.delegation`, `llm.generation.text`, `assistant.personal`, `assistant.general`, `file.full`, `session.history`. *(No `session.management` — that's the channel's.)*

| Mode | Payload | Tool? | Notes |
| --- | --- | --- | --- |
| `message` | `{prompt}` | no | Main loop. Builds context from the orchestrator thread (see [sessions.md](./sessions.md)); may delegate ([delegation.md](./delegation.md)). |
| `cron_message` | `{prompt}` | no | Scheduled run; **no session history**, full toolset, **never delegates**; returns `AgentOutput(content=message, metadata={report, reason})`. See [cron.md](./cron.md). |
| `subagent` | `LLMData` | no | Generic subagent execution (used by deep_reasoning's `execute_step`); no session history. |
| `end_delegation` | `{delegation_summary, exit_reason, user_prompt?}` | no | Hand-back target. Appends the recap to the main thread, optionally continues the loop. See [delegation.md](./delegation.md). |

`non_tool_modes`: all four.

---

## computer_use — group l1

Coding / computer tasks; drives the sandbox.

**Capabilities:** `llm.generation.text`, `assistant.coding`, `assistant.computer`, `file.full`, `computer.bash`, `computer.network`, `session.history`.

| Mode | Payload | Tool? | Notes |
| --- | --- | --- | --- |
| `subagent` | `LLMData` | **yes** | Tool face — the orchestrator's LLM calls `call_computer_use`. No history append, no streaming. |
| `on_delegation` | `LLMData` | no | First delegated turn; appends to its own thread; streams via `ctx.progress`. |
| `delegated_message` | `{prompt}` | no | Subsequent delegated turns; carries the `end_delegation` local tool. |

`non_tool_modes`: `[on_delegation, delegated_message]`. **Local tools:** sandbox calls (`bash`, etc.), `file_tools`, `read_file`.

---

## research — group l1

Web + RAG + document research; owns the knowledge base.

**Capabilities:** `llm.generation.text`, `assistant.rag`, `assistant.web`, `assistant.document`, `file.full`, `database.manage`, `database.retrieval`, `document.convert`, `session.history`, `web.fetch`.

> `database.retrieval` (not `search`) to match the knowledge_base callee; `document.convert` so its LLM can call `md_converter.convert`.

Same three modes as computer_use (`subagent` tool-visible; `on_delegation` / `delegated_message` `tool=false`).

**Local tools:**
- `web_search` — backend via `SUITE_WEB_SEARCH_BACKEND`: `searxng` (default, Brave-API-compatible link list), `brave` (LLM-Context API — params `country`/`search_language`/`count`/`freshness`/`local_city`), or `kagi` (FastGPT answer + cited sources). The tool's parameter schema reflects the active backend; an unset key falls back to SearXNG. See [deployment.md § Web search](./deployment.md#web-search).
- `html_fetch(urls[], raw=false, truncate=2000≤100k)` — fetches a list of URLs; `raw=true` returns raw HTML; `raw=false` routes each URL to `md_converter.webpage` (content), or — when backend=`kagi` — batches them through Kagi's Extract API. Caps: see [data-model.md](./data-model.md).
- `web_download(url)` — downloads to a file-store **name**; 50 MB / 150 s caps (env-configurable).
- knowledge_base calls, `file_tools`, `read_file`.

---

## deep_reasoning — group l1

Planning / multi-step reasoning.

**Capabilities:** `agent.orchestration`, `llm.generation.text`, `llm.multimodal.image`, `assistant.planning`, `assistant.reasoning`, `file.full`, `session.history`.

Same three modes as the other l1 agents.

**`plan_mode`** is a **terminal tool offered on delegated turns** (both `on_delegation` and `delegated_message`, wired via the `L1Config.extra_terminal` seam — *not* in `subagent` mode, which structurally bounds recursion since `execute_step` re-enters the suite only through `orchestrator(subagent)`). When the model calls `plan_mode(objective, steps?)`, the turn loop returns and `deep_reasoning/plan.py::run_plan` drives an explicit plan, one **fresh** decision loop per step (system prompt = objective + plan + result *summaries*, not an accumulating transcript). The result is the delegated turn's `AgentOutput`, so the session stays delegated to deep_reasoning. Plan state is in-memory; `plan_max_steps` / `plan_max_iters` guarantee termination.

| Plan tool | Args | Effect |
| --- | --- | --- |
| `add_step` | `add_after_num`, `contents` | insert a step (`0` = front) |
| `modify_step` | `target_step_num`, `contents` | rewrite a step |
| `remove_step` | `target_step_num` | delete a step |
| `execute_step` | `relevant_context`, `additional_instruction?` | **spawn `orchestrator(subagent)`** with (step-instruction + additional, relevant context + prior results, the current step); record the result, advance |
| `quit_and_report` | `result_content` | finalize + report (short-circuit) |
| `read_file` | `name` | inspect a result's file (`read_only` `file_tools`) |
| `send_file` | `name` | mark a produced file to deliver with the final report |

Each decision's prompt is `## Current step (n/N): {item} — decide: modify the plan / execute / finalize`. When the cursor passes the last step, a final loop (limited to `add_step` + `read_file`/`send_file`) writes the answer unless another step is needed.

> `execute_step` → `orchestrator(subagent)` is **l1→l0**; allowed by the ACL rule `l1/* -> l0/agent.orchestration`. The whole plan runs inside one delegated task, so watch its deadline for long plans ([delegation.md F4](./delegation.md)); each step also has a `plan_step_timeout_s`.

---

## config — group l2

Conversational user self-service: settings **and** scheduled jobs.

**Capabilities:** `user.config`, `user.cron`.

Both modes are **tool-visible** — config is reachable only by the orchestrator (`l0/* → l2/user.config`) and the channel (`channel/* → l2/user.config`), so exposing them as tools is safe. With two tool modes, `build_tools` names them `call_config_message` and `call_config_cron`.

| Mode | Payload | Tool? | Notes |
| --- | --- | --- | --- |
| `message` | `{prompt}` | **yes** (`call_config_message`) | the orchestrator's LLM applies "change my timezone" etc.; also `/config`. user-config read/set loop |
| `cron` | `{prompt}` | **yes** (`call_config_cron`) | cron-job management loop (list/add/remove/modify): the orchestrator sets reminders conversationally ("remind me at 8am"), and the channel's `/cron` spawns it directly. Hosted here — **not** the chatbot — because the router denies self-call (`<self_call>`). The scheduler stays in the chatbot; they share the `cron_jobs` table ([cron.md](./cron.md)). |

**Local tools:** the user-config editing toolset (read/set fields in [data-model.md](./data-model.md)) for `message`; the cron add/list/remove/modify toolset (`bp_agents/cron_manage.py`) for `cron`. Not delegatable.

---

## knowledge_base — group l3

Per-user document store + retrieval (LanceDB). All modes tool-visible (research's LLM calls them).

**Capabilities:** `database.manage`, `database.retrieval`, `file.full`, `document.convert`.

| Mode | Payload (structured) |
| --- | --- |
| `store` | `name`, `collection?`(=default), `title?`, `tags?`, `description?`, `overwrite?`(overwrite\|error\|append_count) |
| `modify` | `title`, `collection?`, `target_collection?`, `target_title?`, `tags?`, `description?` |
| `remove` | `title`, `collection?` |
| `list` | `query?`, `collection?`, `tag?` |
| `retrieve` | `query`, `collection?`, `title?`, `tags?`, `search_type?`(hybrid\|vector\|bm25), `count?`(=3) |
| `browse` | `query?`, `collection?`, `tag?`, `start?=0`, `end?=50` — **no** (webapp KB page; JSON, recency-sorted, ≤50/page) |
| `delete` | `title`, `collection?` — **no** (webapp KB page) |

`store` behavior: non-`.md` files are converted via `md_converter.convert` first; missing `title`/`tags`/`description` are LLM-generated (head 8k + tail 2k chars, env-configurable); content-addressed dedup enforced. Chunking + schema in [data-model.md](./data-model.md). `non_tool_modes`: `[browse, delete]`. `browse` returns JSON `{items:[{doc_id,title,collection,tags,description,created_at,updated_at}], total}`; it's the read side of the webapp Knowledge base page (`delete` is the write side — tool-facing `list`/`remove` stay for the orchestrator).

---

## memory — group l3

Per-user fact graph (LanceDB). See [memory.md](./memory.md) for the full pipeline.

**Capabilities:** `memory.add`, `memory.retrieval`.

| Mode | Payload | Tool? |
| --- | --- | --- |
| `retrieve` | `{query, count?=3, child_count?=2}` | **yes** — assistant agents recall mid-loop |
| `add` | `{user_prompt, assistant_response}` | no — channel fire-and-forget after a turn |
| `list` | `{query?, kind?, start?=0, end?=50}` | no — webapp Memory page (JSON; ≤50/page) |
| `delete` | `{uid}` | no — webapp Memory page |
| `manual_add` | `{fact, kind?}` | no — webapp Memory page (skips extraction; still reconciled) |

`non_tool_modes`: `[add, list, delete, manual_add]`. Uses an **embedding** preset (distinct from the chat presets) + the lite chat preset for extraction/decision calls. `list` returns JSON `{items:[{uid,fact,kind,created_at,last_used_at,score?}], total}` — newest-first by `last_used_at`, or ranked by the retrieval formula (relevance × decay) when a query is given (no graph expansion, no `touch`).

---

## history_summarizer — group l3, **hidden=true**

Rolling summarization; reached only by the channel.

**Capabilities:** `llm.generation.text`, `summarize.history`, `session.history`.

| Mode | Payload | Tool? |
| --- | --- | --- |
| `summarize_incumbent` | `{agent_id, up_to, previous_summary?}` | no |
| `summarize_all` | `{agent_id?, summarize_after?}` | no |
| `session_name` | `{user_prompt}` | no |

Read-only over `session_history`; returns `AgentOutput(content=<summary>)`. The **channel** applies the result (writes the summary, flips `incumbent`). `session_name` returns a short conversation title generated from the first user message (lite preset) — the channel writes it to `session_info.session_name` ([webapp.md](./webapp.md) §4). See [sessions.md](./sessions.md). `non_tool_modes`: all.

---

## md_converter — group l4

File / webpage → Markdown (MarkItDown). **Not hidden** — `convert` is a useful tool; only `webpage` is restricted.

**Capabilities:** `document.convert`, `web.convert`, `file.full`.

| Mode | Payload (structured) | Tool? |
| --- | --- | --- |
| `convert` | `name`, `output_type?`(file\|content\|auto), `ocr?`(=false) | **yes** |
| `webpage` | `url`, `output_type?`, `truncate?`(=2000≤100k) | **no** — restricts the URL-fetch surface to handcrafted callers (research's `html_fetch`) |

`auto` ⇒ content if ≤2k chars else file; `content` over 100k force-truncates. `webpage` fetch capped at 5 MB (env-configurable). `non_tool_modes`: `[webpage]`.

**LLM-vision OCR (optional, opt-in).** With `SUITE_MD_OCR_API_KEY` + `SUITE_MD_OCR_MODEL` set, the `markitdown-ocr` plugin can OCR images embedded in PDF/DOCX/PPTX/XLSX files — plus full-page OCR for scanned PDFs — inlining the extracted text. OCR is **per-request**: it runs only when a `convert` call passes `ocr=true` (the model should set it for scanned/image-based documents whose text isn't selectable), NOT on every conversion — the OCR converter is slower and spends a vision call per image. OCR uses its OWN OpenAI-Chat-Completions-compatible provider (key/model/`SUITE_MD_OCR_BASE_URL`/`SUITE_MD_OCR_PROMPT` + the `SUITE_MD_OCR_TIMEOUT_S`/`_MAX_RETRIES` bounds), separate from the router's presets, because MarkItDown needs a synchronous `llm_client` that can't ride the frame channel. `ocr=true` with no backend configured is a no-op (plain conversion).

---

## sandbox — group infra

Containerized Debian workspace, one per user (`/home/{user_id}/`, with `uv` pre-installed and a `user_id→uid` mapping in user-config). Per-user storage quota by level. All modes tool-visible (computer_use's LLM calls them).

**Capabilities:** `computer.bash`, `computer.network`, `file.full`.

| Mode | Payload (structured) |
| --- | --- |
| `bash` | `command` — execute; oversized stdout saved to a file-store name |
| `stash_to_workspace` | `name` — fetch a stash file into the workspace |
| `workspace_to_stash` | `path` — save a workspace file to the stash (returns its name) |

`non_tool_modes`: `[]`.

---

## chatbot — group channel, **hidden=true**

Telegram channel **+ session manager + cron scheduler** (v1). Acts as an authoritative user-management channel (registration, password reset) and a `service`-level principal that operates on behalf of users via `serviced_by` ([overview §2.1](./overview.md)). Because it **submits registrations as a service principal**, the router auto-grants it `serviced_by` on the new user at admin approval (and opens the user's initial session) — no manual wiring.

It is a **gateway**, not a normal handler-agent — its full runtime (the inbound poll loop, the three credential identities, the `outbound_admit`/`await_result` task-injection primitive, slash commands, and inbound/outbound file handling) lives in [`channel.md`](./channel.md). The **normal reply is the awaited result** of the task it injected; it's `hidden=true`, and the modes below are only *proactive* pushes / management — the common path never calls them.

**Capabilities:** `channel.telegram`, `user.auth`, `user.registration`, `user.cron`, `file.full`, `session.history`, **`session.management`**.

| Mode | Payload | Tool? | Notes |
| --- | --- | --- | --- |
| `message_to_user` | `{message}` | no | push text to `channel`+`chat_id` from session-info; append assistant row |
| `file_to_user` | `{name}` | no | fetch a stash file and send it |

`non_tool_modes`: both. Cron **management** now lives on the config agent's `cron` mode (the router denies self-call, so the channel can't host it); the chatbot still owns the cron **scheduler** (firing).

**Responsibilities beyond modes** (it's the session manager):
- Owns the **per-session queue** and all **session-info writes** ([sessions.md](./sessions.md), [delegation.md](./delegation.md)).
- Dispatches user messages to `orchestrator(message)`, or to `delegated_to` (`delegated_message`) during delegation.
- Fires `memory.add` (parallel) and summarization (queued) after each turn.
- Auto-saves user-attached files to the stash and appends a `(T,T)` "user-attached file saved as {name}" row before dispatching the message; `agent_id` = the current target (orchestrator or the delegate).
- Runs the **cron scheduler** ([cron.md](./cron.md)).

---

## webapp — group channel (**v2**)

Web channel + session manager.

**Capabilities:** `channel.web`, `user.auth`, `user.config`, `file.full`, `computer.bash`, `session.management`.

Same session-manager responsibilities as the chatbot. Triggers the v2 channel-agnostic cron routing ([cron.md §6](./cron.md)).
