"""sandbox agent — per-user bash workspace (shared container / per-uid).

`bash` runs a shell command in the user's workspace dir, capturing
combined stdout/stderr; oversized output is saved to a file-store name
instead of inlined. `stash_to_workspace` / `workspace_to_stash`
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
import errno
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


class StashToWorkspace(BaseModel):
    name: str


class WorkspaceToStash(BaseModel):
    path: str


agent = Agent(
    info=AgentInfo(
        agent_id=SANDBOX_AGENT_ID,
        description=(
            "The user's isolated, PERSISTENT sandbox workspace — a single "
            "directory that bash runs in and that keeps its files across "
            "calls. Run shell commands (bash), pull a stash file in "
            "(stash_to_workspace) where bash can use it by bare filename, "
            "and push a produced file back out (workspace_to_stash). Fetched "
            "and created files all live together in bash's working directory; "
            "run `ls` to see them."
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


def _ensure_workspace(workspace: Path, uid: int | None) -> None:
    """Create the workspace dir and (when dropping to a per-user uid) make it
    owned by that uid. The whole sandbox treats the workspace as belonging to
    the user uid: bash runs AS that uid, and the stash<->workspace copies run
    as it too (see `_copy_as_uid`). Owning the dir up-front is what lets the
    dropped processes read+write it — root itself has cap_drop: ALL minus
    SETUID/SETGID/CHOWN, so NO CAP_DAC_OVERRIDE, and cannot touch a uid-owned
    dir directly. Raises a clear RuntimeError if the chown is refused."""
    workspace.mkdir(parents=True, exist_ok=True)
    if uid is None or os.geteuid() != 0:
        return  # rootless dev: no drop, no chown needed.
    try:
        os.chown(str(workspace), uid, uid)
    except OSError as exc:
        logger.error(
            "sandbox_workspace_chown_failed",
            extra={"event": "sandbox_workspace_chown_failed",
                   "uid": uid, "errno": exc.errno, "error": repr(exc)},
        )
        if exc.errno == errno.EINVAL:
            hint = (
                f"uid {uid} is not valid inside the container's user namespace "
                "— under Docker userns-remap/rootless the container maps only a "
                "sub-range to uids 0..65535. Set SUITE_SANDBOX_UID_BASE/_MAX to "
                "a range your container maps (default 2000..60000 fits a "
                "standard 65536-wide map)"
            )
        else:
            hint = (
                "the sandbox container needs the CHOWN capability "
                "(cap_add: CHOWN) so the dropped user can own its workspace"
            )
        raise RuntimeError(
            f"sandbox could not chown its workspace to uid {uid} ({exc}); {hint}"
        ) from exc


def _write_bytes_as_uid(dest: Path, data: bytes, uid: int | None) -> None:
    """Write `data` to `dest` AS the user uid (when dropping). Root has no
    CAP_DAC_OVERRIDE, so it can't create a file inside the uid-owned workspace
    bash leaves behind — fork a child, drop to the uid, and write there. Fork
    (not a thread) because setuid is per-process and must not touch the agent's
    own root identity; the child does only the write and exits.

    The child does NO Python imports or async work (pathlib/io are already
    imported, the event loop is never touched), so the multi-threaded-fork
    deadlock the DeprecationWarning warns about can't bite — same property the
    existing bash `preexec_fn` fork relies on. `data` is already in hand."""
    if uid is None or os.geteuid() != 0:
        dest.write_bytes(data)
        return
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            dest.write_bytes(data)
            os._exit(0)
        except BaseException:  # noqa: BLE001 — child must never escape the fork
            os._exit(17)
    _, status = os.waitpid(pid, 0)
    if os.WEXITSTATUS(status) != 0:
        raise PermissionError(
            f"sandbox could not write {dest.name} as uid {uid} "
            f"(child exit {os.WEXITSTATUS(status)})"
        )


def _read_bytes_as_uid(path: Path, uid: int | None) -> bytes:
    """Read `path` AS the user uid (when dropping) — the workspace file is
    uid-owned and root (no CAP_DAC_OVERRIDE) may not be able to read it. The
    dropped child streams the bytes back through a pipe."""
    if uid is None or os.geteuid() != 0:
        return path.read_bytes()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(r)
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            with open(path, "rb") as fh:
                while chunk := fh.read(65536):
                    os.write(w, chunk)
            os.close(w)
            os._exit(0)
        except BaseException:  # noqa: BLE001
            os._exit(17)
    os.close(w)
    chunks: list[bytes] = []
    while data := os.read(r, 65536):
        chunks.append(data)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    if os.WEXITSTATUS(status) != 0:
        raise PermissionError(
            f"sandbox could not read {path.name} as uid {uid} "
            f"(child exit {os.WEXITSTATUS(status)})"
        )
    return b"".join(chunks)


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
    # Create + (when dropping) hand the workspace to the user uid, so the
    # dropped bash can read+write it. Shared with the stash<->workspace copies.
    await asyncio.to_thread(_ensure_workspace, workspace, uid)
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
    # An empty result is normal (e.g. a successful `cp`/`python script.py` that
    # prints nothing) — say so explicitly, otherwise the model misreads the
    # blank reply as a failure or a missing file and retries needlessly.
    if not text.strip():
        return text_output(
            "(command finished with no output — this usually means success; "
            "run `ls` to see the workspace if you expected a file)"
        )
    return text_output(text)


@agent.handler(
    mode="bash",
    description="Run a bash command in the user's persistent sandbox "
    "workspace. Every call starts in the SAME working directory — the "
    "workspace root — and files persist across calls, so a file you fetched "
    "with stash_to_workspace, or wrote in an earlier command, is right there "
    "in `.` (run `ls` to see them). Relative paths resolve against the "
    "workspace; you don't know or need its absolute path. Returns combined "
    "stdout+stderr (a successful command may print nothing).",
)
async def bash(ctx: TaskContext, payload: Bash) -> AgentOutput:
    return await run_bash(ctx, payload, settings=_settings, uid=_user_uid(ctx))


@agent.handler(
    mode="stash_to_workspace",
    description="Copy a stash file (by name) into the sandbox workspace so "
    "bash can operate on it. The file lands at the TOP of the workspace, which "
    "is exactly bash's working directory — so afterwards `bash` can reference "
    "it by its bare filename (e.g. `python example.py` or `cat ./example.py`), "
    "no path needed.",
)
async def stash_to_workspace(
    ctx: TaskContext, payload: StashToWorkspace
) -> AgentOutput:
    workspace = _workspace(_settings, ctx.user_id)
    uid = _user_uid(ctx)
    # Same ownership model as bash: the workspace belongs to the user uid.
    await asyncio.to_thread(_ensure_workspace, workspace, uid)
    src = await ctx.files.read(payload.name)
    dest = workspace / Path(payload.name).name
    data = await asyncio.to_thread(src.read_bytes)
    # Write AS the uid — root (no DAC_OVERRIDE) can't create a file inside the
    # uid-owned workspace bash leaves behind.
    await asyncio.to_thread(_write_bytes_as_uid, dest, data, uid)
    # Tell the model exactly how to reach it: bash's cwd IS this workspace, so
    # the file is just `./<name>` (or the bare name) — no absolute path needed.
    return text_output(
        f"Copied '{payload.name}' into the workspace as ./{dest.name} "
        f"(bash starts in this directory, so refer to it as '{dest.name}')."
    )


@agent.handler(
    mode="workspace_to_stash",
    description="Save a file from the sandbox workspace back to the stash so "
    "it can be delivered or reused, returning its stash name. Pass the path as "
    "bash would see it — a workspace-relative path like 'out.csv' or "
    "'results/report.pdf' (the same cwd bash runs in).",
)
async def workspace_to_stash(
    ctx: TaskContext, payload: WorkspaceToStash
) -> AgentOutput:
    workspace = _workspace(_settings, ctx.user_id)
    path = _resolve_in_workspace(workspace, payload.path)
    # Read AS the uid — the workspace file bash produced is uid-owned, and root
    # (no DAC_OVERRIDE) may not be able to read it.
    data = await asyncio.to_thread(_read_bytes_as_uid, path, _user_uid(ctx))
    name = await ctx.files.store(data, filename=path.name)
    return AgentOutput(content=f"Saved {path.name} to the stash as {name}", files=[name])


if __name__ == "__main__":
    agent.run()
