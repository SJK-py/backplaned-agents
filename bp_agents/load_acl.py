"""bp_agents.load_acl — apply the suite ACL rule set to a running router.

    python -m bp_agents.load_acl

Logs in as the bootstrap admin (env / .env: ROUTER_BOOTSTRAP_ADMIN_EMAIL
+ ROUTER_BOOTSTRAP_ADMIN_PASSWORD) and refreshes the router's suite-owned
ACL rules via `PUT /v1/admin/acl/rules`.

NON-DESTRUCTIVE: it reads the current rules first and MERGES — only the
suite's own rules (by name) are refreshed; every other rule an admin added
(e.g. MCP grants) is preserved. This runs on every `run-suite.sh` boot, so a
destructive replace would wipe admin customisations each restart.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from bp_agents.acl import merge_preserving_custom, suite_acl_rules


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(".env") as fh:
            for line in fh:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


async def _main() -> int:
    router = os.environ.get("ROUTER_URL", "http://127.0.0.1:8000")
    email = _env("ROUTER_BOOTSTRAP_ADMIN_EMAIL")
    password = _env("ROUTER_BOOTSTRAP_ADMIN_PASSWORD")
    if not email or not password:
        print("admin creds not found (ROUTER_BOOTSTRAP_ADMIN_*)", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        login = await client.post(
            f"{router}/v1/auth/login",
            json={"email": email, "password": password},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        # Read current rules so the apply can preserve admin-added (non-suite)
        # rules instead of clobbering them.
        current = await client.get(f"{router}/v1/admin/acl/rules", headers=auth)
        current.raise_for_status()
        existing = current.json()
        body = merge_preserving_custom(existing)
        resp = await client.put(
            f"{router}/v1/admin/acl/rules",
            headers=auth,
            json=body,
        )
        resp.raise_for_status()
        applied = len(resp.json())
        preserved = applied - len(suite_acl_rules())
        print(f"applied {applied} ACL rules ({preserved} admin rule(s) preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
