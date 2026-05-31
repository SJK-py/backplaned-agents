"""sandbox agent — per-user bash workspace (shared container / per-uid).

`bash` runs a shell command in the user's workspace dir, capturing
combined stdout/stderr; oversized output is saved to a file-store name
instead of inlined. `storage_to_workspace` / `workspace_to_storage`
bridge the named file store and the workspace filesystem.

uid isolation: each user is assigned a distinct OS uid (allocated +
persisted LOCALLY by `uid_store.UidStore` on the agent's state volume — the
sandbox is network-isolated from the suite DB, so it owns this mapping
itself). When the process runs as root (prod), the bash subprocess drops to
that uid and the workspace is chowned to it; rootless dev runs as the current
user. The map is sequential from `sandbox_uid_base`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import resource  # POSIX-only; the sandbox runs on Linux
from pathlib import Path

from pydantic import BaseModel

from bp_agents.agents.sandbox.uid_store import UidStore
from bp_agents.common import text_output
from bp_agents.settings import SuiteSettings, load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

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
            "The user's isolated sandbox workspace — run bash and move "
            "files between the stash and the workspace."
        ),
        groups=["infra"],
        capabilities=["computer.bash", "computer.network", "file.full"],
    ),
)

_settings: SuiteSettings = load_suite_settings()
# Per-user uid map, owned locally (NO suite DB — the sandbox is network-
# isolated from Postgres). Wired at startup from the agent's state dir.
_uid_store: UidStore | None = None


@agent.on_startup
async def _startup() -> None:
    global _uid_store  # noqa: PLW0603 — startup-wired handle
    _uid_store = UidStore(
        state_dir=Path(agent.config.state_dir),
        base=_settings.sandbox_uid_base,
        maximum=_settings.sandbox_uid_max,
    )


def _workspace(settings: SuiteSettings, user_id: str) -> Path:
    return Path(settings.sandbox_root) / _SAFE.sub("_", user_id)


def _user_uid(ctx: TaskContext) -> int | None:
    """The OS uid this user's bash drops to (allocated + persisted locally on
    first sight). None pre-startup or if the uid range is exhausted — the
    caller then runs without a drop rather than reuse a colliding uid."""
    if _uid_store is None:
        return None
    return _uid_store.uid_for(ctx.user_id)


def _apply_rlimits(settings: SuiteSettings) -> None:
    """Lower the calling (child) process's resource limits. A process may
    always reduce its own soft/hard limits without privilege, so this runs
    whether or not we drop uid. Best-effort: a limit we can't set is skipped
    rather than blocking the command. `resource` is imported at module top
    (NOT here) — a preexec_fn runs in the forked child and must not import
    (the import lock may be held at fork time → deadlock/failure)."""
    caps = (
        (resource.RLIMIT_NPROC, settings.sandbox_rlimit_nproc),
        (resource.RLIMIT_AS, settings.sandbox_rlimit_as_bytes),
        (resource.RLIMIT_FSIZE, settings.sandbox_rlimit_fsize_bytes),
        (resource.RLIMIT_CPU, settings.sandbox_rlimit_cpu_s),
    )
    for res, limit in caps:
        if not limit or limit <= 0:
            continue  # 0 disables this cap
        try:
            _soft, hard = resource.getrlimit(res)
            # Can't raise the hard limit unprivileged — clamp to it.
            new = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(res, (new, new))
        except (ValueError, OSError):
            continue


def _preexec(uid: int | None, settings: SuiteSettings):  # noqa: ANN202
    """Child pre-exec: bound resources (always) and drop privileges (when
    root + uid set). The rlimits stop one tenant's command — fork bomb,
    memory balloon, disk fill, CPU spin — from starving the SHARED container,
    which the wall-clock timeout and uid drop alone do not."""
    drop_uid = uid is not None and os.geteuid() == 0

    def _set() -> None:
        # Resource caps FIRST: they apply with or without the uid drop, and
        # capping NPROC before dropping to the sandbox uid bounds a fork bomb
        # against that uid's process count.
        _apply_rlimits(settings)
        if drop_uid:
            # Drop root's supplementary groups BEFORE setgid/setuid — otherwise
            # the subprocess keeps every group root belonged to, defeating the
            # per-user isolation this uid drop exists to provide. setgroups
            # must run while still privileged.
            os.setgroups([])
            os.setgid(uid)  # type: ignore[arg-type]
            os.setuid(uid)  # type: ignore[arg-type]

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
    # When we'll drop to a per-user uid, the workspace (created as root) must
    # be owned by that uid or the dropped command can't write to it. chown
    # only when actually dropping (root + uid set); harmless to repeat.
    if uid is not None and os.geteuid() == 0:
        try:
            await asyncio.to_thread(os.chown, str(workspace), uid, uid)
        except OSError as exc:
            # Don't swallow this: if the chown fails the workspace stays
            # root-owned and the about-to-be-dropped uid can't write it, so the
            # bash command fails anyway — with the opaque "Exception occurred in
            # preexec_fn" instead of a clear cause. The fix is the CHOWN
            # capability (docker-compose.prod.yml sandbox cap_add); surface that
            # explicitly rather than limping into a broken run.
            logger.error(
                "sandbox_workspace_chown_failed",
                extra={"event": "sandbox_workspace_chown_failed",
                       "uid": uid, "error": repr(exc)},
            )
            raise RuntimeError(
                f"sandbox could not chown its workspace to uid {uid} ({exc}); "
                "the sandbox container needs the CHOWN capability "
                "(cap_add: CHOWN) so the dropped user can write its workspace"
            ) from exc
    proc = await asyncio.create_subprocess_shell(
        payload.command,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_preexec(uid, settings),
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


@agent.handler(
    mode="bash",
    description="Run a bash command in the user's sandbox workspace and "
    "return its output.",
)
async def bash(ctx: TaskContext, payload: Bash) -> AgentOutput:
    return await run_bash(ctx, payload, settings=_settings, uid=_user_uid(ctx))


@agent.handler(
    mode="storage_to_workspace",
    description="Copy a stash file (by name) into the sandbox workspace so "
    "bash can operate on it.",
)
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


@agent.handler(
    mode="workspace_to_storage",
    description="Save a file from the sandbox workspace (by path) back to "
    "the stash, returning its stash name.",
)
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
