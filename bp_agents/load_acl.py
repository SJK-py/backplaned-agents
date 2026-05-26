"""bp_agents.load_acl — apply the suite ACL rule set to a running router.

    python -m bp_agents.load_acl

Logs in as the bootstrap admin (env / .env: ROUTER_BOOTSTRAP_ADMIN_EMAIL
+ ROUTER_BOOTSTRAP_ADMIN_PASSWORD) and bulk-replaces the router's ACL
rules with `bp_agents.acl.suite_acl_rules()` via `PUT /v1/admin/acl/rules`.
Replaces ALL rules — run against a router whose ACL the suite owns.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from bp_agents.acl import acl_replace_payload


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
        resp = await client.put(
            f"{router}/v1/admin/acl/rules",
            headers={"Authorization": f"Bearer {token}"},
            json=acl_replace_payload(),
        )
        resp.raise_for_status()
        print(f"applied {len(resp.json())} ACL rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
