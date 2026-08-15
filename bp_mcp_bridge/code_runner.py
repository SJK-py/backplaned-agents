"""Run one operator-authored Python function in a hardened subprocess.

This module is the whole security story of the code-agent kind, so it is
kept free of backplane concerns: it takes source + params, and returns a
result or raises. `code_agent.py` wraps it in an `Agent`.

**Why not in-process.** The bridge holds `BP_MCP_BRIDGE_SERVICE_SECRET`
(which mints invitations for ANY bridged agent), every bridged agent's
`credentials.json`, and every resolved MCP secret. `exec()`-ing operator
code beside those means one stray `open()` is full impersonation of
another agent. See `docs/design/bridge-python-code-agents.md` §3.1.

**What the subprocess buys**, precisely (design §3.5): the uid drop keeps
it out of other agents' credentials; the CONSTRUCTED env keeps the
bridge's own secrets out of its reach; rlimits bound fork bombs, memory
and file size; the wall-clock timeout bounds it in time; `no_new_privs`
blocks privilege regain. It does NOT restrict network egress — that needs
CAP_NET_ADMIN, which the bridge deliberately does not have (§3.4).

The hardening primitives (`_stdio_preexec`, `StdioSpawnConfig`) are the
same ones the stdio MCP transport uses; this reuses them rather than
growing a second implementation of the uid drop.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bp_mcp_bridge.config import StdioPolicy
from bp_mcp_bridge.mcp_client import StdioSpawnConfig, _stdio_preexec

logger = logging.getLogger(__name__)

# Cap on the result a function may return inline. Past this the call fails
# with an error naming `output_as_file` rather than silently truncating —
# a truncated JSON body is worse than an error, because the caller cannot
# tell it happened.
MAX_RESULT_BYTES = 1_000_000

# Operator stderr kept per call. Enough to see a traceback; bounded so a
# chatty loop cannot fill the bridge's log pipeline.
MAX_STDERR_BYTES = 64_000

# Grace between SIGKILL of the process group and giving up on the wait. The
# kill is SIGKILL (not SIGTERM): a function that ignored its deadline has
# already had `timeout_s` to finish, and a handler-swallowed SIGTERM would
# just extend it.
_KILL_GRACE_S = 5.0

# How long to let the stderr drain finish after the kill. The pipes are already
# closed by then, so this is a guard against a pathological holder, not a wait
# anyone should observe.
_DRAIN_GRACE_S = 2.0


class CodeRunError(RuntimeError):
    """The function did not produce a result. `kind` separates a caller-
    visible failure (the code raised, timed out, returned something
    unserialisable) from a bridge fault (spawn failed, chown refused) so
    the handler can report the right thing — reporting an infrastructure
    problem as "your input was bad" is the mistake this exists to avoid
    (design §8)."""

    def __init__(self, message: str, *, kind: str = "code") -> None:
        super().__init__(message)
        self.kind = kind  # "code" | "internal"


@dataclass(frozen=True)
class CodeRunSpec:
    """Everything the runner needs for one agent's calls."""

    agent_id: str
    code: str
    entrypoint: str = "run"
    timeout_s: int = 30
    memory_mb: int = 512
    # Resolved literals, injected into the child's constructed env. The
    # caller resolves `env://` refs (`auth_resolver`); this module only ever
    # sees values, and never logs them.
    secrets: dict[str, str] = field(default_factory=dict)
    policy: StdioPolicy = field(default_factory=StdioPolicy)


# The fixed, bridge-owned child entrypoint. NOT operator-editable.
#
# The stdout dance in the first lines is the detail that bites on day one:
# an operator's stray `print()` would otherwise interleave with the result
# JSON and corrupt it. fd 1 is duplicated to a private descriptor, then
# `sys.stdout` is pointed at stderr — so operator prints land in the log
# stream and only this harness writes the result.
_HARNESS = '''\
import json, os, sys, traceback

# `-I` implies `-P`, which deliberately does NOT put the script's directory on
# sys.path. That isolation is what we want from the interpreter's own env, but
# it also means `import agent_main` cannot find the sibling module — so put the
# workdir (and ONLY the workdir) on the path explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_result_fd = os.dup(1)
os.dup2(2, 1)
sys.stdout = sys.stderr

def _emit(obj):
    data = (json.dumps(obj) + "\\n").encode("utf-8")
    with os.fdopen(_result_fd, "wb") as out:
        out.write(data)
        out.flush()

try:
    request = json.loads(sys.stdin.readline() or "{}")
except Exception as exc:
    _emit({"ok": False, "error": {"type": "HarnessError",
                                  "message": "unreadable request: %s" % exc,
                                  "traceback": ""}})
    raise SystemExit(0)

try:
    import agent_main
except BaseException:
    _emit({"ok": False, "error": {"type": "ImportError",
                                  "message": "the agent module failed to import",
                                  "traceback": traceback.format_exc()}})
    raise SystemExit(0)

fn = getattr(agent_main, os.environ["BP_CODE_ENTRYPOINT"], None)
if not callable(fn):
    _emit({"ok": False, "error": {
        "type": "EntrypointError",
        "message": "module defines no callable %r" % os.environ["BP_CODE_ENTRYPOINT"],
        "traceback": ""}})
    raise SystemExit(0)

try:
    value = fn(request.get("params") or {}, request.get("context") or {})
except BaseException as exc:
    _emit({"ok": False, "error": {"type": type(exc).__name__,
                                  "message": str(exc),
                                  "traceback": traceback.format_exc()}})
    raise SystemExit(0)

try:
    _emit({"ok": True, "result": value})
except (TypeError, ValueError) as exc:
    _emit({"ok": False, "error": {
        "type": "UnserialisableResult",
        "message": "the function returned a value that is not JSON: %s" % exc,
        "traceback": ""}})
'''


def agent_uid(agent_id: str, policy: StdioPolicy) -> int | None:
    """The OS uid this agent's subprocesses drop to — deterministic from
    `agent_id` within the policy range, exactly as the stdio path derives
    one from `server_id`.

    Per AGENT, not per call: the per-call working directory already
    separates concurrent calls, and two calls to the same agent are the
    same trust domain by construction. None disables the drop (rootless
    dev, or no range configured)."""
    if policy.uid_base <= 0 or policy.uid_max <= policy.uid_base:
        return None
    span = policy.uid_max - policy.uid_base + 1
    digest = hashlib.sha256(agent_id.encode()).digest()
    return policy.uid_base + (int.from_bytes(digest[:4], "big") % span)


def _child_env(spec: CodeRunSpec, workdir: Path, context: dict[str, Any]) -> dict[str, str]:
    """The child's ENTIRE environment — built, never inherited.

    `os.environ.copy()` minus deletions is the version of this that looks
    right and leaks the next variable someone adds to the deployment. The
    invariant that matters: `BP_MCP_BRIDGE_SERVICE_SECRET` is absent by
    construction, not by removal."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Read by the harness to resolve the operator's function.
        "BP_CODE_ENTRYPOINT": spec.entrypoint,
        "BP_AGENT_ID": spec.agent_id,
    }
    for key in ("task_id", "user_id", "session_id"):
        value = context.get(key)
        if value:
            env[f"BP_{key.upper()}"] = str(value)
    # Operator secrets last: an operator naming one `PATH` is their problem,
    # not a bridge failure, and shadowing is the least surprising behaviour.
    env.update(spec.secrets)
    return env


def _prepare_workdir(spec: CodeRunSpec, uid: int | None) -> Path:
    """A fresh per-call directory holding the harness + the operator module,
    owned by the agent's uid so the dropped child can read and write it.

    Per CALL, not per agent: two concurrent calls to one agent must not see
    each other's scratch files."""
    root = spec.policy.work_root / spec.agent_id
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="call-", dir=str(root)))
    (workdir / "harness.py").write_text(_HARNESS, encoding="utf-8")
    (workdir / "agent_main.py").write_text(spec.code, encoding="utf-8")
    if uid is not None and os.geteuid() == 0:
        try:
            os.chown(workdir, uid, uid)
            for child in workdir.iterdir():
                os.chown(child, uid, uid)
        except OSError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            if exc.errno == errno.EINVAL:
                raise CodeRunError(
                    f"uid {uid} is not valid inside the container's user "
                    "namespace — under userns-remap/rootless the container "
                    "maps only a sub-range to uids 0..65535. Set "
                    "BP_MCP_BRIDGE_UID_BASE/_MAX to a range it maps",
                    kind="internal",
                ) from exc
            raise CodeRunError(
                f"could not chown the code workdir to uid {uid}: {exc}",
                kind="internal",
            ) from exc
    return workdir


def _spawn_config(spec: CodeRunSpec, workdir: Path, env: dict[str, str]) -> StdioSpawnConfig:
    """Reuse the stdio transport's spawn shape so there is ONE uid-drop
    implementation in the bridge.

    Unlike the stdio path, `RLIMIT_AS` is set: that path disables it because
    `uvx` reserves enormous virtual address space, but a bare `python -I -S`
    has a predictable footprint, so the cap is usable and is the operator's
    `memory_mb`."""
    return StdioSpawnConfig(
        env=env,
        cwd=str(workdir),
        uid=agent_uid(spec.agent_id, spec.policy),
        no_new_privs=True,
        rlimit_nproc=64,
        rlimit_as_bytes=spec.memory_mb * 1024 * 1024,
        # Slightly over the wall clock: the wall-clock timeout is the primary
        # bound, and a CPU cap below it would turn a legitimately busy
        # function into a confusing SIGXCPU before the timeout explains it.
        rlimit_cpu_s=spec.timeout_s + 5,
        rlimit_fsize_bytes=64 * 1024 * 1024,
    )


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Read up to `limit` bytes, then keep draining and discarding.

    Draining CONCURRENTLY with the wait is not optional: a child that fills
    the stderr pipe buffer blocks forever on write, and a `wait()` that has
    not drained would deadlock against it."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return b"".join(chunks)
        if total < limit:
            chunks.append(chunk[: limit - total])
            total += len(chunk)


async def _read_result_line(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Read the harness's single result line.

    Deliberately NOT a drain-to-EOF. The harness writes exactly one line, and
    waiting for EOF means waiting for every holder of the pipe to let go — so
    a function that returns normally after leaving a background process
    (`subprocess.Popen(...)` without a wait) would be reported as a timeout
    and have its perfectly good result thrown away. Reading one line takes the
    result the moment it exists and lets the cleanup deal with the stragglers.
    """
    if stream is None:
        return b""
    try:
        return await stream.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        # EOF before a newline: the interpreter died before the harness could
        # finish writing. Whatever partial bytes exist are the caller's best
        # diagnostic.
        return bytes(exc.partial)
    except asyncio.LimitOverrunError as exc:
        # The reader's buffer is sized to `MAX_RESULT_BYTES` at spawn, so this
        # means the line genuinely exceeded the cap — not that the buffer was
        # too small for a legitimate result. Report it as oversize here rather
        # than reading a truncated prefix that would then fail to parse and be
        # misreported as a harness fault.
        raise CodeRunError(
            f"the result exceeds {MAX_RESULT_BYTES} bytes; set "
            "output_as_file on this agent, or return less"
        ) from exc


async def run_code(
    spec: CodeRunSpec,
    params: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    """Run the operator's function once and return its value.

    Raises `CodeRunError` for every failure, tagged `code` (the caller
    should see it) or `internal` (the bridge is at fault)."""
    uid = agent_uid(spec.agent_id, spec.policy)
    workdir = await asyncio.to_thread(_prepare_workdir, spec, uid)
    env = _child_env(spec, workdir, context)
    spawn = _spawn_config(spec, workdir, env)
    request = json.dumps({"params": params, "context": context}) + "\n"

    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "harness.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(spawn.env),
                cwd=spawn.cwd,
                # Size the reader's buffer to the result cap, so `readuntil`
                # can hold a full-size line and a LimitOverrunError means
                # "genuinely oversize" rather than "buffer too small".
                limit=MAX_RESULT_BYTES + 8192,
                preexec_fn=_stdio_preexec(spawn),  # noqa: PLW1509 — the drop IS the point
                close_fds=True,
                # Own process group, so the timeout can kill anything the
                # function forked. A bare kill(pid) leaves grandchildren.
                start_new_session=True,
            )
        except OSError as exc:
            raise CodeRunError(
                f"could not start the code subprocess: {exc}", kind="internal"
            ) from exc

        stdout, stderr = await _communicate(proc, request, spec)
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)

    if stderr:
        logger.info(
            "code_agent_stderr",
            extra={
                "event": "code_agent_stderr",
                "bp.code_agent_id": spec.agent_id,
                "stderr": stderr.decode("utf-8", "replace"),
            },
        )
    return _parse_result(spec, proc.returncode, stdout, stderr)


async def _communicate(
    proc: asyncio.subprocess.Process, request: str, spec: CodeRunSpec
) -> tuple[bytes, bytes]:
    """Feed the request, take the result line, and enforce the wall clock.

    Waiting on the RESULT LINE rather than on process exit is what makes a
    leaked background process a non-event: `proc.wait()` does not return
    until the pipes close too, so a function that returned fine after a bare
    `Popen(...)` would be reported as a timeout with its result discarded.

    stderr is drained concurrently for the whole call, never after: a child
    that fills the stderr pipe buffer blocks on write, and a read that only
    starts later would deadlock against it.

    The process group is ALWAYS killed once the result is in hand — the exit
    path below — so a code agent cannot leave background work running on the
    bridge host after its call returns.
    """

    async def _feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(request.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child died early; the exit path reports it
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    feed_task = asyncio.create_task(_feed())
    out_task = asyncio.create_task(
        _read_result_line(proc.stdout, MAX_RESULT_BYTES + 4096)
    )
    err_task = asyncio.create_task(_drain(proc.stderr, MAX_STDERR_BYTES))
    pending = (feed_task, out_task, err_task)

    try:
        stdout = await asyncio.wait_for(asyncio.shield(out_task), spec.timeout_s)
        timed_out = False
    except TimeoutError:
        stdout, timed_out = b"", True
    except BaseException:
        # Anything else — an outer cancel, or the read itself refusing an
        # oversize line. Both must still clean up: a skipped teardown leaves
        # the subprocess transport to the garbage collector, which surfaces
        # much later, in an unrelated call, as "Event loop is closed" from
        # `BaseSubprocessTransport.__del__`.
        await _cleanup(proc, pending)
        raise

    # Before returning, not after: `stderr` is part of the result.
    stderr = await _cleanup(proc, pending)
    if timed_out:
        raise CodeRunError(
            f"the function did not finish within {spec.timeout_s}s"
        )
    return stdout, stderr


async def _cleanup(
    proc: asyncio.subprocess.Process,
    pending: tuple[asyncio.Task, ...],
) -> bytes:
    """Reap the process group and collect the drains. Returns stderr.

    The kill is unconditional: on timeout it is what enforces the bound, and
    on success it clears anything the function forked and walked away from,
    so a code agent leaves no background work on the bridge host.

    The tasks are then allowed to FINISH rather than cancelled — the pipes
    are closed once the group is dead, so they return promptly, and finishing
    is what releases the transport (`_feed`'s `finally` closes stdin; a drain
    cancelled mid-read leaves its pipe open). On the timeout path the stderr
    they collect is the operator's only diagnostic.
    """
    await _kill_group(proc)
    done: list[Any] = [None, b"", b""]
    with contextlib.suppress(TimeoutError):
        done = await asyncio.wait_for(
            asyncio.shield(asyncio.gather(*pending, return_exceptions=True)),
            _DRAIN_GRACE_S,
        )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return done[2] if isinstance(done[2], bytes) else b""


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group, then reap it.

    The `wait()` is UNCONDITIONAL, even when the child has already exited on
    its own: `wait()` is what drives asyncio's subprocess transport to finish
    and release its pipes. Skipping it for an already-dead child leaves the
    transport for the garbage collector, which surfaces much later as
    "Event loop is closed" from `BaseSubprocessTransport.__del__`."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), _KILL_GRACE_S)


def _parse_result(
    spec: CodeRunSpec, returncode: int | None, stdout: bytes, stderr: bytes
) -> Any:
    """Turn the harness's one JSON line into a value or a `CodeRunError`."""
    line = stdout.strip()
    if not line:
        # No result line at all: the interpreter died before the harness
        # could emit — an rlimit kill (SIGKILL shows as -9), or a crash.
        detail = stderr.decode("utf-8", "replace").strip()[-400:]
        if returncode is not None and returncode < 0:
            raise CodeRunError(
                f"the function was killed by signal {-returncode} — most "
                f"likely a resource limit (memory_mb={spec.memory_mb})"
                + (f": {detail}" if detail else "")
            )
        raise CodeRunError(
            f"the function produced no result (exit {returncode})"
            + (f": {detail}" if detail else "")
        )
    if len(line) > MAX_RESULT_BYTES:
        raise CodeRunError(
            f"the result exceeds {MAX_RESULT_BYTES} bytes; set "
            "output_as_file on this agent, or return less"
        )
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise CodeRunError(
            f"the harness produced unreadable output ({exc})", kind="internal"
        ) from exc
    if payload.get("ok"):
        return payload.get("result")
    err = payload.get("error") or {}
    # The TRACEBACK is logged, never returned: the caller is usually a model
    # that cannot act on it, and operator locals may hold secrets.
    trace = err.get("traceback") or ""
    if trace:
        logger.warning(
            "code_agent_traceback",
            extra={
                "event": "code_agent_traceback",
                "bp.code_agent_id": spec.agent_id,
                "traceback": trace,
            },
        )
    etype = err.get("type") or "Error"
    message = err.get("message") or "the function failed"
    raise CodeRunError(f"{etype}: {message}")
