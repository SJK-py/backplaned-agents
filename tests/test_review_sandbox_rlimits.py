"""Sandbox bash subprocesses must be resource-bounded (rlimits).

Second-pass security (medium): the sandbox gave each bash command a
wall-clock timeout and a best-effort uid drop, but NO resource limits — so
one tenant's command (fork bomb, memory balloon, disk fill, CPU spin) could
starve every other tenant in the SHARED container. uid drop bounds *who*, not
*how much*. Fix: `_apply_rlimits` lowers RLIMIT_NPROC/AS/FSIZE/CPU in the
child pre-exec, applied whether or not we drop uid (a process can always lower
its own limits unprivileged).
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import tempfile

from bp_agents.agents.sandbox.agent import _apply_rlimits, _preexec
from bp_agents.settings import SuiteSettings


class _Limits:
    """Duck-typed stand-in carrying just the four rlimit knobs."""

    def __init__(self, *, nproc=0, as_bytes=0, fsize=0, cpu=0) -> None:  # type: ignore[no-untyped-def]
        self.sandbox_rlimit_nproc = nproc
        self.sandbox_rlimit_as_bytes = as_bytes
        self.sandbox_rlimit_fsize_bytes = fsize
        self.sandbox_rlimit_cpu_s = cpu


def test_preexec_returns_callable_even_without_root_or_uid() -> None:
    """Regression: previously `_preexec` returned None when uid was None /
    not root, so rlimits never applied. Now it always returns a preexec_fn
    (the rlimit caps are independent of the uid drop)."""
    fn = _preexec(None, _Limits(fsize=4096))
    assert callable(fn)


def _child_getrlimit(which: str, limits: _Limits) -> int:
    """Run a child that applies `limits` in preexec and prints the soft
    limit of `which` (a resource.RLIMIT_* name)."""
    code = (
        "import resource;"
        f"print(resource.getrlimit(resource.{which})[0])"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        preexec_fn=lambda: _apply_rlimits(limits),  # type: ignore[arg-type]
        capture_output=True, text=True, check=True,
    )
    return int(r.stdout.strip())


def test_apply_rlimits_caps_fsize_in_child() -> None:
    assert _child_getrlimit("RLIMIT_FSIZE", _Limits(fsize=4096)) == 4096


def test_apply_rlimits_caps_nproc_in_child() -> None:
    # NPROC may be clamped down to the parent's hard limit; assert it's no
    # higher than requested and not unlimited.
    val = _child_getrlimit("RLIMIT_NPROC", _Limits(nproc=64))
    assert val <= 64 and val != resource.RLIM_INFINITY


def test_zero_disables_a_cap() -> None:
    """A 0 setting leaves that limit untouched (equals the parent's)."""
    parent_soft = resource.getrlimit(resource.RLIMIT_FSIZE)[0]
    assert _child_getrlimit("RLIMIT_FSIZE", _Limits(fsize=0)) == parent_soft


def test_fsize_cap_is_enforced_on_write() -> None:
    """Behavioral: under a tiny FSIZE cap, a child writing past it is killed
    / errors (returncode != 0) instead of filling the disk."""
    path = tempfile.mktemp(prefix="bp_rlimit_")
    try:
        # Loop the writes: the first fills up to the 4096 cap (partial write,
        # no error); a later write starting AT/BEYOND the limit raises EFBIG
        # (CPython ignores SIGXFSZ, so the write errors instead of killing) →
        # uncaught → nonzero exit. A single big write would only do a partial.
        code = (
            "import os;"
            f"fd=os.open({path!r}, os.O_WRONLY|os.O_CREAT);"
            "[os.write(fd, b'a'*100_000) for _ in range(50)]"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            preexec_fn=lambda: _apply_rlimits(_Limits(fsize=4096)),
            capture_output=True, text=True, check=False,
        )
        assert r.returncode != 0, "write past RLIMIT_FSIZE should fail"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_settings_defaults_present_and_bounded() -> None:
    mf = SuiteSettings.model_fields
    assert mf["sandbox_rlimit_nproc"].default == 256
    assert mf["sandbox_rlimit_as_bytes"].default == 2 * 1024**3
    assert mf["sandbox_rlimit_fsize_bytes"].default == 1024**3
    assert mf["sandbox_rlimit_cpu_s"].default == 120
