"""sandbox agent — per-user bash workspace (shared container / per-uid).

`bash` runs a shell command in the user's workspace dir, capturing
combined stdout/stderr; oversized output is saved to a file-store name
instead of inlined. `storage_to_workspace` / `workspace_to_storage`
bridge the named file store and the workspace filesystem.

uid isolation: when the user's `sandbox_uid` is configured AND the
process runs as root, the bash subprocess drops to that uid (per-user
isolation inside the shared container). Otherwise it runs as the current
user (dev / single-tenant).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from bp_agents.common import text_output
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

SANDBOX_AGENT_ID = "sandbox"
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class Bash(BaseModel):
    command: str


class StorageToWorkspace(BaseModel):
    name: str


class WorkspaceToStorage(BaseModel):
    path: str


agent = Agent(
    info=AgentInfo(
        agent_id=SANDBOX_AGENT_ID,
        description=(
            "Run bash commands and move files in the user's isolated "
            "sandbox workspace (the mode in the tool name selects the "
            "operation)."
        ),
        groups=["infra"],
        capabilities=["computer.bash", "computer.network", "file.full"],
    ),
)

_settings: SuiteSettings = load_suite_settings()
_pool: asyncpg.Pool | None = None


@agent.on_startup
async def _startup() -> None:
    global _pool  # noqa: PLW0603 — startup-wired handle
    _pool = await open_pool(_settings)


@agent.on_shutdown
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


def _workspace(settings: SuiteSettings, user_id: str) -> Path:
    return Path(settings.sandbox_root) / _SAFE.sub("_", user_id)


async def _user_uid(ctx: TaskContext) -> int | None:
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        cfg = await queries.get_user_config(conn, ctx.user_id)
    return cfg.sandbox_uid if cfg else None


def _preexec(uid: int | None):  # noqa: ANN202
    """Best-effort uid drop for the subprocess (only when root + uid set)."""
    if uid is None or os.geteuid() != 0:
        return None

    def _set() -> None:
        os.setgid(uid)
        os.setuid(uid)

    return _set


def _resolve_in_workspace(workspace: Path, path: str) -> Path:
    """Resolve a (possibly relative) path and confine it to the workspace
    — refuse traversal outside it."""
    candidate = (workspace / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    workspace = workspace.resolve()
    if workspace not in candidate.parents and candidate != workspace:
        raise ValueError("path escapes the workspace")
    return candidate


async def run_bash(
    ctx: TaskContext,
    payload: Bash,
    *,
    settings: SuiteSettings,
    uid: int | None = None,
) -> AgentOutput:
    workspace = _workspace(settings, ctx.user_id)
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_shell(
        payload.command,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_preexec(uid),
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=settings.sandbox_bash_timeout_s
        )
    except TimeoutError:
        proc.kill()
        return text_output(f"(command timed out after {settings.sandbox_bash_timeout_s}s)")
    text = out.decode("utf-8", errors="replace")
    if len(text) > settings.sandbox_max_inline_output:
        name = await ctx.files.write("bash_output.txt", text)
        head = text[: settings.sandbox_max_inline_output]
        return AgentOutput(
            content=f"{head}\n\n…(output truncated; full output saved as {name})",
            files=[name],
        )
    return text_output(text)


@agent.handler(mode="bash")
async def bash(ctx: TaskContext, payload: Bash) -> AgentOutput:
    return await run_bash(ctx, payload, settings=_settings, uid=await _user_uid(ctx))


@agent.handler(mode="storage_to_workspace")
async def storage_to_workspace(
    ctx: TaskContext, payload: StorageToWorkspace
) -> AgentOutput:
    workspace = _workspace(_settings, ctx.user_id)
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    src = await ctx.files.read(payload.name)
    dest = workspace / Path(payload.name).name
    data = await asyncio.to_thread(src.read_bytes)
    await asyncio.to_thread(dest.write_bytes, data)
    return text_output(f"Fetched into workspace: {dest.name}")


@agent.handler(mode="workspace_to_storage")
async def workspace_to_storage(
    ctx: TaskContext, payload: WorkspaceToStorage
) -> AgentOutput:
    workspace = _workspace(_settings, ctx.user_id)
    path = _resolve_in_workspace(workspace, payload.path)
    data = await asyncio.to_thread(path.read_bytes)
    name = await ctx.files.store(data, filename=path.name)
    return AgentOutput(content=f"Saved {path.name} to the stash as {name}", files=[name])


if __name__ == "__main__":
    agent.run()
