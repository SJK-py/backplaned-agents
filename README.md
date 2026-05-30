# backplaned-agents

[![CI](https://github.com/SJK-py/backplaned-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/SJK-py/backplaned-agents/actions/workflows/ci.yml)

**A multi-user personal-assistant suite — a single orchestrator that reasons, researches, runs code, and remembers — built on the [Backplaned](./docs/overview.md) agent platform.**

Message it on Telegram and it answers: it pulls from your private knowledge base, recalls what it learned about you in past conversations, searches the web, runs code in a sandbox, and schedules reminders — delegating to specialist agents as needed, all under *your* identity. Every user gets their own isolated files, memory, sessions, and knowledge.

The hard parts — transport, task lifecycle, delegation, the file store, the auth/ACL firewall, and a provider-agnostic LLM service — are the platform's job. This repo is the thin, focused layer on top: the agents, the conversation model, and the channels.

---

## Why it's built on Backplaned

Most agent frameworks make you hand-roll the plumbing: provider SDKs in every agent, ad-hoc inter-agent calls, bespoke file passing, DIY auth. Backplaned provides that plumbing as infrastructure, so each agent here is a few dozen lines of *behavior* — and gets multi-tenancy, security, and observability for free.

| Backplaned gives you… | …so this suite can just |
| --- | --- |
| **Typed task protocol + lifecycle** — a WebSocket frame protocol with admit, dispatch, retry, and an *exactly-one-terminal-result* guarantee. | Write each agent as a small `run_llm_loop` handler. No queues, no result plumbing. |
| **First-class delegation** — handing off *reassigns the live task* (it keeps its id) and the new agent terminates it. | Let the orchestrator pass a whole conversation to `deep_reasoning` / `research` / `computer_use` and back, with no orchestration glue. |
| **Provider-agnostic LLM service** — named presets over Gemini / Anthropic / OpenAI / local servers, with fallback chains, retries, token+cost accounting, embeddings, and SSRF-guarded endpoints. | Hold **no provider keys** in any agent. Agents call `ctx.llm` and name a tier (`pro`/`balanced`/`lite`/`embedding`); a user swaps Gemini↔Claude↔GPT from chat — no redeploy. |
| **Router-managed file store** — per-user/per-session scope, content-addressed dedup, name binding, and multimodal `file_ref` resolution into LLM calls (S3/rustfs backend). | Share files between memory, the KB, and the sandbox **by name**, feed images/PDFs straight into the model, and never touch object-store credentials. |
| **Deny-by-default ACL firewall** — declarative `caller → callee` rules with tier gating. | Keep the untrusted sandbox reachable *only* via `computer_use`, recall reachable by assistants, etc. — policy, not code. |
| **Multi-tenant identity** — agent JWTs, service principals, per-user tokens, and an invitation/onboarding flow. The end user's `user_id` is derived from the task, **never asserted by an agent**. | Be multi-user from the first message. Files, memory, sessions, and knowledge are siloed per user with *zero* per-agent enforcement code. |
| **Live progress + observability** — `ProgressFrame` fan-out, tracing, metrics, structured logs, Redis-backed revocation/quota. | Stream step-by-step activity to the user in verbose mode (`Thinking… / [Tool] / [Result]`) for free. |

---

## What the suite adds

A roster of cooperating agents and the conversation machinery around them:

- **Orchestrator** — the assistant the user talks to. Runs the tool-calling loop and **delegates** to specialists when a task warrants it.
- **Specialists** — `deep_reasoning` (planning / multi-step thought), `research` (web search + fetch → Markdown, RAG over your KB), `computer_use` (drives a **sandboxed** bash environment).
- **Long-term memory** — a per-user **fact graph** (LanceDB) with hybrid vector + BM25 retrieval, recency decay, 1-hop graph expansion, and background GC. The assistant remembers your preferences and context across conversations.
- **Knowledge base** — per-user documents with hybrid retrieval, semantic chunking, any-file ingest (via `md_converter`), and LLM-generated metadata.
- **Conversational sessions** — full history per `(session, agent)` thread with **rolling summarization** so context stays bounded without losing the thread.
- **Scheduled tasks** — DST-aware **cron** reminders and jobs that run on your behalf and ping you when they matter.
- **Channels** — **Telegram** (slash commands: `/new`, `/stop`, `/config`, `/cron`, `/password`, `/v` for verbose) and a **web app** (browser channel: login, session management, live-progress chat, settings/cron, file stash).
- **Helpers** — `config` (change settings in natural language), `history_summarizer`, `md_converter`.

Every task runs as the end user, so all of the above is isolated per person.

## How it fits together

```
Telegram ──▶ chatbot (gateway)        ┌──────────  Backplaned router  ───────────┐
              │  injects the turn as  │   task lifecycle · delegation · ACL ·    │
              │  a task for the user ─┼─▶ file store · LLM service · identity    │
              ▼                       └──────────────────────────────────────────┘
         orchestrator ──delegate──▶ deep_reasoning / research / computer_use
              │   │                                  │
              │   └─call──▶ memory · knowledge_base · md_converter
              ▼                                      ▼
        rolling summary                        sandbox (bash)
```

Agents reach the router over WebSocket and the suite's own Postgres + per-user LanceDB directly; the LLM and the file store are always the router's. See [`docs/agent-suite/overview.md`](./docs/agent-suite/overview.md) for the full picture.

---

## QuickStart

The system runs as a **router** (the Backplaned platform) plus a fleet of **suite agents**. The full reference is [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md); this is the happy path.

**Prerequisites:** Python 3.12+, Docker (compose v2), a Telegram bot token (from [@BotFather](https://t.me/BotFather)), and an LLM key (e.g. a `GEMINI_API_KEY`).

### Develop — router + agents from source, dependencies in Docker

```bash
# 0. Install. `llm-gemini` pulls the google-genai SDK the router needs to
#    call Gemini (suite agents call the LLM via the router, so they don't
#    need it); `admin` mounts the /admin web UI (else /admin/login 404s);
#    `webapp` adds the browser channel's FastAPI/Jinja2 stack.
uv venv && source .venv/bin/activate
uv pip install -e ".[router,suite,dev,llm-gemini,admin,webapp]"

# 1. Backing services — Postgres (creates BOTH bp_router + bp_suite). Redis,
#    SearXNG, and rustfs(S3) are opt-in profiles (single-worker dev needs none):
#    docker compose -f docker-compose.dev.yml up -d
#    # extras: … --profile redis --profile search --profile s3 up -d
docker compose -f docker-compose.dev.yml up -d

# 2. Router — generates ./.env (and prints a bootstrap admin password),
#    migrates bp_router. Set your LLM key, then boot it.
scripts/dev-up.sh
$EDITOR .env                          # set GEMINI_API_KEY=...
set -a && . ./.env && set +a
python -m bp_router                   # serves http://127.0.0.1:8000 — leave running

# 3. Suite — migrates bp_suite, mints each agent's invitation, launches all
#    12 agents (incl. the webapp browser channel), and applies the suite ACL.
#    Run in a second shell (venv active). run-suite.sh sets an insecure dev
#    WEBAPP_SESSION_SECRET for you; the web UI serves on http://127.0.0.1:8002.
set -a && . ./.env && set +a
SUITE_TELEGRAM_BOT_TOKEN=<your-token> scripts/run-suite.sh

# 4. Message your bot on Telegram and send /register, then approve it at
#    http://127.0.0.1:8000/admin/login  (admin creds were printed in step 2).
#    For the browser channel: send /password to the bot to set a web password,
#    then log in at http://127.0.0.1:8002 with your email + that password.
```

### Production — everything in Docker

```bash
# Production launcher. First asks whether to build deploy/.env.prod (a
# first deploy, or to change vars — prompts for the few things only you can
# provide: domain, admin email/password, LLM key, Telegram token, search
# backend; random-generates the rest). Then runs a compose action:
# start / restart / stop (start & restart can rebuild images first). The
# bundled-SearXNG `--profile search` flag is auto-added when configured.
# Compose brings up schema migrations → a one-shot `bootstrap` (registers
# invitations + applies the suite ACL once the router is healthy) → every
# agent, in dependency order.
scripts/prod.sh
```

Then message the bot on Telegram, send `/register`, and approve it as admin. The **browser channel** is served by the `webapp` service behind Caddy on its own host — `app.<your-domain>` by default (override with `WEBAPP_DOMAIN`); users log in with their email + a web password (`/password` to the bot). Invitations, networks, the SearXNG profile, and the sandbox-isolation caveat are detailed in [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md).

---

## Learn more

- [`.env.example`](./.env.example) — every configurable environment variable (router / agent SDK / suite), grouped with defaults.
- [`docs/agent-suite/overview.md`](./docs/agent-suite/overview.md) — the suite's architecture and design.
- [`docs/agent-suite/`](./docs/agent-suite/) — per-area design docs: [agents](./docs/agent-suite/agents.md), [delegation](./docs/agent-suite/delegation.md), [sessions](./docs/agent-suite/sessions.md), [memory](./docs/agent-suite/memory.md), [data model](./docs/agent-suite/data-model.md), [cron](./docs/agent-suite/cron.md), [channel](./docs/agent-suite/channel.md), [ACL](./docs/agent-suite/acl.md).
- [`docs/overview.md`](./docs/overview.md) — the Backplaned platform.
- [`docs/agent-suite/deferred-work.md`](./docs/agent-suite/deferred-work.md) — known caveats and intentional simplifications.

## License

See [LICENSE](./LICENSE).
