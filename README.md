# backplaned-agents

backplaned-agents is a first-party, multi-user personal-assistant suite built on the Backplaned router and SDK. A single orchestrator runs the conversation and delegates to specialist agents — computer use, research/RAG, and deep reasoning — backed by per-user long-term memory (a fact graph) and a document knowledge base. On top it adds conversational sessions with rolling summarization, scheduled cron tasks, and user-facing channels (Telegram today, web next.)

Everything is per end-user: each task runs under the user’s own identity, and files, memory, sessions, and knowledge are isolated per user. Backplaned provides the transport, task lifecycle, delegation, file store, ACL, and LLM service; this repo layers the agents, conversation model, and channels on top.

## QuickStart

The system runs as a **router** (the Backplaned platform) plus a fleet of **suite agents**. The full reference is [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md); this is the happy path.

**Prerequisites:** Python 3.12+, Docker (compose v2), a Telegram bot token (from [@BotFather](https://t.me/BotFather)), and an LLM key (e.g. a `GEMINI_API_KEY`).

### Develop — router + agents from source, dependencies in Docker

```bash
# 0. Install. The llm-gemini extra pulls the google-genai SDK the router
#    needs to call Gemini (suite agents call the LLM via the router, so
#    they don't need it themselves).
uv venv && source .venv/bin/activate
uv pip install -e ".[router,suite,dev,llm-gemini]"

# 1. Backing services — Postgres (creates BOTH bp_router + bp_suite) + Redis.
docker compose -f docker-compose.dev.yml up -d
#    Optional research web search:
#    docker compose -f docker-compose.dev.yml --profile search up -d

# 2. Router — generates ./.env (and prints a bootstrap admin password),
#    migrates bp_router. Set your LLM key, then boot it.
scripts/dev-up.sh
$EDITOR .env                          # set GEMINI_API_KEY=...
set -a && . ./.env && set +a
python -m bp_router                   # serves http://127.0.0.1:8000 — leave running

# 3. Suite — migrates bp_suite, mints each agent's invitation, launches all
#    11 agents. Run in a second shell with the venv active.
set -a && . ./.env && set +a
SUITE_TELEGRAM_BOT_TOKEN=<your-token> scripts/run-suite.sh

# 4. Initial configuration (first run only)
python -m bp_agents.load_acl          # apply the suite ACL (PUT /v1/admin/acl/rules)
#    Then message your bot on Telegram and send /register, and approve it at
#    http://127.0.0.1:8000/admin/login  (admin creds were printed in step 2).
```

### Production — everything in Docker

```bash
# 1. Generate deploy/.env.prod — prompts for the few things only you can
#    provide (domain, admin email/password, GEMINI key, Telegram token) and
#    random-generates the rest (DB password, JWT/session/metrics secrets,
#    object-store keys, one invitation token per agent).
scripts/init-prod-env.sh

# 2. Up. Compose runs the schema migrations, then a one-shot `bootstrap`
#    (registers the invitations + applies the suite ACL once the router is
#    healthy), then every agent — in dependency order, one command.
docker compose -f docker-compose.prod.yml --env-file deploy/.env.prod up -d
```

Then message the bot on Telegram, send `/register`, and approve it as admin. Invitations, networks, the SearXNG profile, and the sandbox-isolation caveat are detailed in [`docs/agent-suite/deployment.md`](./docs/agent-suite/deployment.md).

## License

See [LICENSE](./LICENSE).
