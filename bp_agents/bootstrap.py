"""bp_agents.bootstrap — one-shot production bootstrap for a running router.

    python -m bp_agents.bootstrap

Logs in as the bootstrap admin (env: ROUTER_BOOTSTRAP_ADMIN_EMAIL /
_PASSWORD, or BOOTSTRAP_ADMIN_*), then:

  1. **Registers** invitations via `POST /v1/admin/invitations` (which
     accepts a caller-supplied `token`). Two shapes:

       * `SUITE_ROSTER_TOKEN` — ONE token bound to every agent that does not
         provision a service user, each name consumable once
         (`docs/design/deployment-agent-host.md` §3). This is the path the
         agent host uses.
       * `<AGENT>_INVITATION` — the original per-agent tokens. Still the only
         way to register the chatbot's `provisions_service_user=true`
         invitation, which is deliberately NOT bundled into the roster: it
         yields a minting-capable principal, and eleven ordinary agents
         should not inherit that.

     Idempotent either way: a token already registered (201 / 409) is
     treated as success.
  2. **Applies** the suite ACL (`bp_agents.acl`) via `PUT /v1/admin/acl/rules`,
     MERGING so admin-added rules (e.g. MCP grants) survive each boot.

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

from bp_agents.acl import merge_preserving_custom, suite_acl_rules
from bp_agents.load_acl import _env

# Invitation TTL (seconds). The suite mints a FRESH single-use token per agent
# on every launch (`scripts/prod.sh` `refresh_invitations`) and agents onboard
# within seconds of this bootstrap completing, so a short TTL is ample and
# limits the blast radius of a leaked-but-unused token. The router GC sweeps
# expired rows (bp_router.tasks.invitation_gc_loop). Overridable for slow /
# staggered rollouts via ROUTER_BOOTSTRAP_INVITATION_TTL_S.
_INVITATION_TTL_S = int(os.environ.get("ROUTER_BOOTSTRAP_INVITATION_TTL_S", "600"))

# name : env var : provisions_service_user. Only the chatbot provisions its
# usr_service_* principal (registration submit + per-user minting).
_ROSTER: list[tuple[str, str, bool]] = [
    ("chatbot", "CHATBOT_INVITATION", True),
    ("webapp", "WEBAPP_INVITATION", False),
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
        #
        # NO per-name Idempotency-Key. The token itself is the natural dedup
        # key — re-registering the SAME token collides on the token-hash PK
        # and returns 409 ("already registered", fine). A per-name key
        # (`register-<name>`) was actively WRONG with fresh-token-per-launch
        # (prod.sh `refresh_invitations`): the router's idempotency contract
        # returns the EXISTING row for a repeated key and IGNORES the new
        # token, so a relaunch's fresh token was never registered → the agent
        # then presents an unregistered token → 403. Without the key, each
        # launch's fresh token is registered for real.
        registered = 0

        # ROSTER PATH (`docs/design/deployment-agent-host.md` §3). One token
        # for every agent that does NOT provision a service user — which is
        # all of them but the chatbot. Twelve mandatory env vars become two,
        # and the token is bound to names: an invitation with no roster can
        # onboard as ANY name, because `POST /v1/onboard` takes the name from
        # the agent's own `agent_info`.
        #
        # The chatbot keeps its own token deliberately. Its invitation is
        # flagged `provisions_service_user` — a higher-privilege credential
        # that yields a minting-capable principal — and bundling that with
        # eleven ordinary agents would hand every one of them the same flag.
        roster_token = os.environ.get("SUITE_ROSTER_TOKEN")
        if roster_token:
            roster_names = [n for n, _var, prov in _ROSTER if not prov]
            resp = await client.post(
                f"{router}/v1/admin/invitations",
                headers=headers,
                json={
                    "level": "tier1",
                    "token": roster_token,
                    "agent_ids": roster_names,
                    "provisions_service_user": False,
                    "expires_in_s": _INVITATION_TTL_S,
                },
            )
            if resp.status_code in (201, 409):
                registered += 1
                print(f"registered roster for {len(roster_names)} agent(s)")
            else:
                print(
                    f"register roster FAILED: {resp.status_code} {resp.text}",
                    file=sys.stderr,
                )
                resp.raise_for_status()

        # PER-AGENT PATH. Still supported and still the only way to register
        # a `provisions_service_user` invitation. With a roster token set,
        # only the chatbot's var is normally present; without one, this is
        # the original twelve-token behaviour, unchanged.
        for name, var, prov in _ROSTER:
            tok = os.environ.get(var)
            if not tok:
                if not roster_token:
                    print(f"skip {name}: {var} unset", file=sys.stderr)
                continue
            resp = await client.post(
                f"{router}/v1/admin/invitations",
                headers=headers,
                json={
                    "level": "tier1",
                    "token": tok,
                    "provisions_service_user": prov,
                    "expires_in_s": _INVITATION_TTL_S,
                },
            )
            # 201 = freshly registered; 409 = this exact token already
            # registered (idempotent re-run). Anything else (403/422/5xx) is a
            # real failure we must surface — do NOT mask it.
            if resp.status_code in (201, 409):
                registered += 1
            else:
                print(
                    f"register {name} FAILED: {resp.status_code} {resp.text}",
                    file=sys.stderr,
                )
                resp.raise_for_status()
        print(f"registered {registered} invitation(s)")

        # 2. Apply the suite ACL — NON-DESTRUCTIVELY. Read the current rules
        # first and merge: refresh only the suite-owned rules, preserve every
        # other rule an admin added (e.g. MCP grants). This runs on every prod
        # boot (the `bootstrap` compose service), so a destructive replace
        # would wipe admin customisations each restart.
        current = await client.get(
            f"{router}/v1/admin/acl/rules", headers=headers
        )
        current.raise_for_status()
        acl = await client.put(
            f"{router}/v1/admin/acl/rules",
            headers=headers,
            json=merge_preserving_custom(current.json()),
        )
        acl.raise_for_status()
        applied = len(acl.json())
        preserved = applied - len(suite_acl_rules())
        print(f"applied {applied} ACL rules ({preserved} admin rule(s) preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
