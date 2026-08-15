# backplaned-agents

[![CI](https://github.com/SJK-py/backplaned-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/SJK-py/backplaned-agents/actions/workflows/ci.yml)

<p align="center">
  <img src="docs/assets/overview.png" alt="Backplaned-Agents overview — the Backplaned platform (typed task protocol, first-class delegation, provider-agnostic LLM service, router-managed file store, deny-by-default ACL firewall, multi-tenant identity, live observability, MCP servers, admin web UI) and the multi-user assistant suite (orchestrator + deep_reasoning / research / computer_use specialists, knowledge base, conversational sessions, scheduled tasks, MCP servers) across Telegram / KakaoTalk / web app, plus the Docker production-deployment flow." width="960">
</p>

**A multi-user personal-assistant suite — a single orchestrator that reasons, researches, runs code, and remembers — built on the [Backplaned](./docs/backplaned/overview.md) agent platform.**

Message it on Telegram and it answers: it pulls from your private knowledge base, recalls what it learned about you in past conversations, searches the web, runs code in a sandbox, and schedules reminders — delegating to specialist agents as needed, all under *your* identity. Every user gets their own isolated files, memory, sessions, and knowledge.

The hard parts — transport, task lifecycle, delegation, the file store, the auth/ACL firewall, and a provider-agnostic LLM service — are the platform's job. This repo is the thin, focused layer on top: the agents, the conversation model, and the channels.

---

## Why it's built on Backplaned

Most agent frameworks make you hand-roll the plumbing: provider SDKs in every agent, ad-hoc inter-agent calls, bespoke file passing, DIY auth. Backplaned provides that plumbing as infrastructure, so each agent here is a few dozen lines of *behavior* — and gets multi-tenancy, security, and observability for free.

| Backplaned gives you… | …so this suite can just |
| --- | --- |
| **Typed task protocol + lifecycle** — a WebSocket frame protocol with admit, dispatch, retry, and an *exactly-one-terminal-result* guarantee. | Write each agent as a small `run_llm_loop` handler. No queues, no result plumbing. |
| **First-class delegation** — handing off *reassigns the live task* (it keeps its id) and the new agent terminates it. | Let the orchestrator pass a whole conversation to `deep_reasoning` / `research` / `computer_use` and back, with no orchestration glue. |
| **Provider-agnostic LLM service** — named presets over Gemini / Anthropic / OpenAI / local servers, with fallback chains, retries, token+cost accounting, embeddings, and SSRF-guarded endpoints. | Hold **no provider keys** in any agent. Agents call `ctx.llm` and name a **slot** (`pro`/`balanced`/`lite`); the router resolves it from the user's own choice ∩ their tier. A user swaps Gemini↔Claude↔GPT from Settings — no redeploy, and a model their plan forbids is refused there rather than on their next message. |
| **Router-managed file store** — per-user/per-session scope, content-addressed dedup, name binding, and multimodal `file_ref` resolution into LLM calls (S3-compatible backend — SeaweedFS in the bundled deploy). | Share files between memory, the KB, and the sandbox **by name**, feed images/PDFs straight into the model, and never touch object-store credentials. |
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
- **Operator-defined agents, three kinds, no redeploy** — a supervisor stands up one backplane agent per row in the admin UI, provisions its credentials, and reconciles live as you edit. All three are ordinary **ACL-gated** agents the orchestrator and specialists can call:
  - **MCP servers** — connect external **[Model Context Protocol](https://modelcontextprotocol.io)** servers (GitHub, databases, SaaS APIs, …) over HTTP/SSE or a local `stdio` subprocess; each becomes one agent with one mode per tool, re-listed as the server's tool set changes. ([design](./docs/design/mcp-bridge-per-server-mode-per-tool.md))
  - **Custom LLM agents** — a system prompt, a user-prompt template, typed parameters and a model preset become a callable agent; optionally a bounded tool-use loop with file access and peer tools. ([design](./docs/design/mcp-bridge-custom-llm-agents.md))
  - **Python code agents** — for the half of the problem that isn't a model: a weather API, an internal REST call, a CSV transform. You author a function in the admin UI and it runs in a **uid-dropped, rlimited, scoped-env subprocess** with a per-call working directory, killed on timeout — not in the bridge process, so a typo can't reach another agent's credentials. ([design](./docs/design/bridge-python-code-agents.md))
- **Channels** — **Telegram** (slash commands: `/new`, `/stop`, `/config`, `/cron`, `/password`, `/v` for verbose), **KakaoTalk** (the same commands, via an egress-only pull consumer behind a tiny Cloudflare Worker relay — see [`docs/design/kakao-channel.md`](./docs/design/kakao-channel.md)), and a **web app** (browser channel: login, session management, live-progress chat, settings/cron, file stash).
- **Helpers** — `config` (change settings in natural language), `history_summarizer`, `md_converter`.

Every task runs as the end user, so all of the above is isolated per person.

---

## QuickStart (production)

A single launcher — [`scripts/prod.sh`](./scripts/prod.sh) — drives the whole production lifecycle on top of [`docker-compose.prod.yml`](./docker-compose.prod.yml): the router, the suite agents (grouped into two host processes plus the isolated sandbox — see [`docs/design/deployment-agent-host.md`](./docs/design/deployment-agent-host.md)), and the bundled dependencies (Postgres, Valkey, SeaweedFS, and optionally SearXNG), behind a Caddy edge proxy. For **local development** (router + agents from source), see [`DEVELOPMENT.md`](./DEVELOPMENT.md).

**Prerequisites:** Docker (compose v2) on the host, a hostname for the edge proxy (a real domain, a LAN name, or a bare IP — `localhost` also works), an LLM provider API key (Anthropic / Gemini / OpenAI), and a Telegram bot token (from [@BotFather](https://t.me/BotFather)).

```bash
scripts/prod.sh
```

It runs interactively in two stages.

**1 — Build `deploy/.env.prod`** (first deploy, or to change vars). Prompts for only the values you must provide and **random-generates the rest** (Postgres password, JWT / admin-session / metrics secrets, object-store keys, and one invitation token per agent):

- **Edge host** — `localhost` (local self-signed TLS), a real domain (auto-TLS via Let's Encrypt; must resolve here with 80/443 open), a LAN name (Caddy internal CA), or a bare IP (the webapp moves to a separate HTTPS port). Also asks whether TLS is terminated **upstream** (Cloudflare Tunnel / external LB) so Caddy serves plain HTTP at the origin.
- **LLM provider + key** — Anthropic, Gemini, OpenAI, or **Custom** (wire your own lite/balanced/pro presets via the admin UI or a `deploy/presets.custom.jsonc` overlay).
- **Telegram bot token** and **web-search backend** (bundled SearXNG, or hosted Brave / Kagi by key).

Re-run later and answer "no" to reuse the existing file — volume-baked secrets (Postgres password, S3 keys) are carried forward so a rebuild never breaks auth against a surviving volume.

**2 — Run a compose action** against that env file:

| Action | Effect |
|---|---|
| **start** | `up -d` (optionally `--build` to rebuild images from source first) |
| **restart** | `up -d --force-recreate` (optionally `--build`) |
| **stop** | `down` — keeps data volumes |
| **reset** | `down -v` — **deletes** the DB + all data volumes (Postgres, Valkey, SeaweedFS, LanceDB, agent creds) for a clean slate |

> **⚠️ Upgrading from a build before 2026-08-18?** Both Alembic chains were
> consolidated to a single baseline migration, so there is **no upgrade path**
> from an older database — `init` fails with `Can't locate revision identified
> by '00NN_…'` and nothing starts against a stale schema. Use the **reset**
> action above and start fresh; back anything you care about up first, because
> `reset` deletes every data volume. A **fresh** install is unaffected. Details
> and the safe-failure guarantee: [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md#databases).

Compose brings everything up in dependency order: **router** → a one-shot **`init`** (both schemas, then the agent invitations + suite ACL once the router is healthy) → the agent groups. Onboarding uses **one roster token** (`SUITE_ROSTER_TOKEN`) plus the chatbot's own higher-privilege invitation, rather than one token per agent. The `search` profile (bundled SearXNG) is auto-added when the env file points at it — no flag to remember. The **agent bridge** runs under an optional `mcp` profile that `prod.sh` auto-adds when `MCP_BRIDGE_SECRET` is set — which it generates by default, so the bridge runs out of the box; you then add MCP servers, custom LLM agents, or Python code agents in the admin UI (unset the secret to leave it off).

Then message the bot on Telegram, send `/register`, and approve it as admin. The **browser channel** is served by the `channels` service (which also runs the chatbot) behind Caddy on its own host — `app.<your-domain>` by default (override with `WEBAPP_DOMAIN`); users log in with their email + a web password (`/password` to the bot). Invitations, networks, the SearXNG profile, and the sandbox-isolation caveat are detailed in [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md).

**Optional — SSO (OIDC):** the webapp can also authenticate against an external identity provider (Authelia, Keycloak, Google, Microsoft). It's **off by default** and wired manually by the operator: set the `OIDC_*` variables in `deploy/.env.prod` (the compose files already pass them through) — see the commented block in [`deploy/.env.prod.example`](./deploy/.env.prod.example) and the design in [`docs/design/oidc-webapp.md`](./docs/design/oidc-webapp.md).

---

## Learn more

- [`DEVELOPMENT.md`](./DEVELOPMENT.md) — local development: run the router + full agent suite from source, smoke tests, the agent test-drive, and dev-mode footguns.
- [`.env.example`](./.env.example) — every configurable environment variable (router / agent SDK / suite), grouped with defaults.
- [`docs/agent-suite/overview.md`](./docs/agent-suite/overview.md) — the suite's architecture and design.
- [`docs/agent-suite/`](./docs/agent-suite/) — per-area design docs: [agents](./docs/agent-suite/agents.md), [delegation](./docs/agent-suite/delegation.md), [sessions](./docs/agent-suite/sessions.md), [memory](./docs/agent-suite/memory.md), [data model](./docs/agent-suite/data-model.md), [cron](./docs/agent-suite/cron.md), [channel](./docs/agent-suite/channel.md), [ACL](./docs/agent-suite/acl.md).
- [`docs/backplaned/overview.md`](./docs/backplaned/overview.md) — the Backplaned platform.
- [`docs/agent-suite/deferred-work.md`](./docs/agent-suite/deferred-work.md) — known caveats and intentional simplifications.

## License

See [LICENSE](./LICENSE).
