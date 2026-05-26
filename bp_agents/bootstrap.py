"""bp_agents.bootstrap — one-shot production bootstrap for a running router.

    python -m bp_agents.bootstrap

Logs in as the bootstrap admin (env: ROUTER_BOOTSTRAP_ADMIN_EMAIL /
_PASSWORD, or BOOTSTRAP_ADMIN_*), then:

  1. **Registers** each agent's pre-supplied invitation token — read from
     `<AGENT>_INVITATION` env vars — via `POST /v1/admin/invitations` (which
     accepts a caller-supplied `token`). The chatbot's is flagged
     `provisions_service_user=true`. Idempotent: a token already registered
     (201 idempotent / 409) is treated as success.
  2. **Applies** the suite ACL (`bp_agents.acl`) via `PUT /v1/admin/acl/rules`.

Pure-Python (httpx) so it runs in the slim suite image with no curl. Wired
as the compose `bootstrap` one-shot (depends on the router being healthy;
the agents depend on it completing), so `docker compose up -d` registers +
ACLs before any agent onboards — no manual steps. An unset `<AGENT>_INVITATION`
is skipped with a warning so the same entrypoint also works ACL-only.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from bp_agents.acl import acl_replace_payload
from bp_agents.load_acl import _env

# name : env var : provisions_service_user. Only the chatbot provisions its
# usr_service_* principal (registration submit + per-user minting).
_ROSTER: list[tuple[str, str, bool]] = [
    ("chatbot", "CHATBOT_INVITATION", True),
    ("orchestrator", "ORCHESTRATOR_INVITATION", False),
    ("history_summarizer", "HISTORY_SUMMARIZER_INVITATION", False),
    ("memory", "MEMORY_INVITATION", False),
    ("knowledge_base", "KNOWLEDGE_BASE_INVITATION", False),
    ("md_converter", "MD_CONVERTER_INVITATION", False),
    ("config", "CONFIG_INVITATION", False),
    ("deep_reasoning", "DEEP_REASONING_INVITATION", False),
    ("research", "RESEARCH_INVITATION", False),
    ("computer_use", "COMPUTER_USE_INVITATION", False),
    ("sandbox", "SANDBOX_INVITATION", False),
]


async def _main() -> int:
    router = os.environ.get("ROUTER_URL", "http://127.0.0.1:8000")
    email = _env("ROUTER_BOOTSTRAP_ADMIN_EMAIL") or _env("BOOTSTRAP_ADMIN_EMAIL")
    password = _env("ROUTER_BOOTSTRAP_ADMIN_PASSWORD") or _env("BOOTSTRAP_ADMIN_PASSWORD")
    if not email or not password:
        print("admin creds not found (ROUTER_BOOTSTRAP_ADMIN_* / BOOTSTRAP_ADMIN_*)",
              file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        login = await client.post(
            f"{router}/v1/auth/login", json={"email": email, "password": password}
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Register pre-supplied invitation tokens.
        registered = 0
        for name, var, prov in _ROSTER:
            tok = os.environ.get(var)
            if not tok:
                print(f"skip {name}: {var} unset", file=sys.stderr)
                continue
            resp = await client.post(
                f"{router}/v1/admin/invitations",
                headers={**headers, "Idempotency-Key": f"register-{name}"},
                json={"level": "tier1", "token": tok, "provisions_service_user": prov},
            )
            if resp.status_code in (201, 409):  # created / already registered
                registered += 1
            else:
                resp.raise_for_status()
        print(f"registered {registered} invitation(s)")

        # 2. Apply the suite ACL.
        acl = await client.put(
            f"{router}/v1/admin/acl/rules", headers=headers, json=acl_replace_payload()
        )
        acl.raise_for_status()
        print(f"applied {len(acl.json())} ACL rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
