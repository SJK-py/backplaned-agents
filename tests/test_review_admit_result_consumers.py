"""R10 CRITICAL regression: every `admit_task` consumer must unwrap
`AdmitResult.task_id`.

R9 changed `admit_task` from returning a bare `str` to
`AdmitResult(task_id, replay_result)`. PR #200's commit claimed
"blast radius: 2 callers" but there were THREE — the third,
`bp_sdk.testing.TestServer.spawn_and_wait`, was missed. It bound
the `AdmitResult` straight into an asyncpg query param and a
`ResultFrame(task_id=...)` Pydantic str field, breaking the
documented agent-developer e2e harness (`test_smoke_e2e.py`).
That stayed green offline because the e2e test skips without
`TEST_DB_URL` — it would have surfaced exactly when a real agent
developer first ran the harness.

This guard enumerates EVERY `await admit_task(...)` call site in
the codebase and asserts each unwraps `.task_id` — so the next
missed consumer fails a fast offline unit test instead of a live
integration run.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _python_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pkg in ("bp_router", "bp_sdk"):
        out.extend((_ROOT / pkg).rglob("*.py"))
    return out


def test_every_admit_task_call_unwraps_task_id() -> None:
    """AST-scan: for every `await admit_task(...)` (or
    `admit_task(...)` awaited), the value must be consumed as
    `.task_id` — either `(await admit_task(...)).task_id`, or
    bound to a name `r` that is later `.task_id`-accessed. A call
    whose result is bound and used WITHOUT `.task_id` is the R9
    regression and fails here."""
    offenders: list[str] = []

    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        if "admit_task(" not in text:
            continue
        tree = ast.parse(text, filename=str(path))

        for node in ast.walk(tree):
            # Match a Call to a function named `admit_task`
            # (bare name or attribute, e.g. `tasks_mod.admit_task`).
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute)
                else None
            )
            if name != "admit_task":
                continue
            # Skip the definition itself / non-call mentions handled
            # by the Call filter already.

            # Case 1: directly attribute-accessed —
            # `(await admit_task(...)).task_id`. The Call's parent
            # chain is Await -> Attribute(attr="task_id").
            # We find this by checking if SOME Attribute node in the
            # tree wraps an Await wrapping THIS call with
            # attr == "task_id".
            wrapped_ok = any(
                isinstance(a, ast.Attribute)
                and a.attr == "task_id"
                and isinstance(a.value, ast.Await)
                and a.value.value is node
                for a in ast.walk(tree)
            )
            if wrapped_ok:
                continue

            # Case 2: bound to a name via `<name> = await
            # admit_task(...)` (or `= admit_task(...)`), and that
            # name is `.task_id`-accessed somewhere in the same
            # module.
            bound_name: str | None = None
            for assign in ast.walk(tree):
                if not isinstance(assign, ast.Assign):
                    continue
                val = assign.value
                if isinstance(val, ast.Await):
                    val = val.value
                if val is node and len(assign.targets) == 1 and isinstance(
                    assign.targets[0], ast.Name
                ):
                    bound_name = assign.targets[0].id
                    break
            if bound_name is not None:
                used_task_id = any(
                    isinstance(a, ast.Attribute)
                    and a.attr == "task_id"
                    and isinstance(a.value, ast.Name)
                    and a.value.id == bound_name
                    for a in ast.walk(tree)
                )
                # A bound result that is NEVER `.task_id`-accessed
                # AND IS used elsewhere (e.g. as a query param) is
                # the regression. If it's bound but the name is the
                # AdmitResult used structurally (`.replay_result`
                # too), that's still fine — `.task_id` access is the
                # required signal.
                if used_task_id:
                    continue
                offenders.append(
                    f"{path.relative_to(_ROOT)}: `{bound_name} = "
                    f"await admit_task(...)` is never .task_id-"
                    f"unwrapped (R9 AdmitResult regression)"
                )
                continue

            # Case 3: awaited-but-discarded or passed inline as an
            # argument without .task_id — also a regression unless
            # it's the pytest.raises error-path style (no value use).
            # Conservatively flag; the known-good sites are all
            # Case 1 or Case 2.
            offenders.append(
                f"{path.relative_to(_ROOT)}: `admit_task(...)` result "
                f"used without .task_id unwrap (R9 AdmitResult "
                f"regression)"
            )

    # Error-path test code legitimately does
    # `with pytest.raises(...): asyncio.run(admit_task(...))` — but
    # that's in tests/, which we don't scan (only bp_router/bp_sdk).
    assert not offenders, "AdmitResult not unwrapped:\n" + "\n".join(
        offenders
    )


def test_test_router_call_unwraps_task_id() -> None:
    """Targeted pin on the consumer PR #200 missed:
    `TestRouter.call` (the e2e harness `test_smoke_e2e.py` drives —
    skipped offline without TEST_DB_URL, which is why the suite
    stayed green while it was broken)."""
    pytest.importorskip("fastapi")
    from bp_sdk.testing import TestRouter

    src = inspect.getsource(TestRouter.call)
    # The admit_task result is `.task_id`-unwrapped before the
    # poll loop binds it as a query param / ResultFrame field.
    assert ").task_id" in src
    assert "task_id = await admit_task(" not in src


def test_known_router_consumers_unwrap_task_id() -> None:
    """Pin the other two known consumers so a future refactor that
    re-introduces a bare-str assumption is caught here too."""
    pytest.importorskip("fastapi")
    from bp_router import dispatch
    from bp_router.api import admin

    dsrc = inspect.getsource(dispatch._handle_new_task)
    assert "result = await admit_task(" in dsrc
    assert "result.task_id" in dsrc

    asrc = inspect.getsource(admin)
    assert ").task_id" in asrc  # admin Test-Task path unwraps
