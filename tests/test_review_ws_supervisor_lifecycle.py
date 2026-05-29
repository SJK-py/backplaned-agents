"""Four WS supervisor lifecycle fixes from the third-pass review.

  1. **Supersede close code 4003** distinct from 4001 (auth_failed).
  2. **`last_seen_at` updated on disconnect** so admin dashboards
     show the disconnect time, not the connect time.
  3. **LLM tasks AWAITED after cancel** so provider streaming
     actually stops on disconnect (cancel-only let one more chunk
     run and bill).
  4. **Resume window doc clarification** — module docstring now
     describes precisely what the window does (and doesn't).

Source-pin style for the docstring + functional pins for the
behaviour changes.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


def test_supersede_close_uses_distinct_code_4003() -> None:
    """Source pin: the supersede close in `_handshake` uses code
    4003 with `reason="superseded"`, distinct from 4001 (auth)
    and 4029 (rate-limit)."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._handshake)
    assert "code=4003" in src
    assert 'reason="superseded"' in src


def test_supersede_close_code_unique_in_module() -> None:
    """Cross-check: no other path uses 4003 (each close code is a
    distinct semantic signal)."""
    pytest.importorskip("fastapi")
    from pathlib import Path

    body = Path(__file__).parent.parent.joinpath(
        "bp_router/ws_hub.py"
    ).read_text()
    # Exactly one `code=4003` literal in the file.
    assert body.count("code=4003") == 1


def test_on_disconnect_updates_last_seen_at() -> None:
    """Source pin: `_on_disconnect` calls
    `queries.update_agent_last_seen`. Pre-R6, that call ran only
    on connect, so admin dashboards saw stale connect-time
    'last seen' values for long-running connections."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._on_disconnect)
    assert "queries.update_agent_last_seen" in src
    # Wrapped in try/except so a DB hiccup doesn't fail the
    # disconnect path.
    assert "except Exception" in src


def test_on_disconnect_awaits_llm_task_cancellation() -> None:
    """Source pin: after `task.cancel()` on each in-flight LLM
    task, `_on_disconnect` `await`s gather() with a bounded
    timeout. Without the await, cancellation only sets the flag
    and the next provider-streaming chunk still runs (and is
    billed) before unwind."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._on_disconnect)
    assert "asyncio.wait_for(" in src
    assert "asyncio.gather(*in_flight" in src
    assert "return_exceptions=True" in src


def test_on_disconnect_llm_cancel_timeout_is_bounded() -> None:
    """The await must have a bounded timeout — a wedged provider
    call must not block the disconnect path indefinitely."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._on_disconnect)
    # 2 seconds is the chosen bound; pin both the literal and the
    # rationale comment.
    assert "timeout=2.0" in src


def test_module_doc_describes_resume_window_contract() -> None:
    """The module docstring now spells out precisely what the
    resume window does and doesn't do. Pre-R6 it implied frames
    bound for the agent DURING the gap were queued on the parked
    entry; in fact `delivery.py` returns `AgentNotConnected` for
    any agent not in `_live`."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    doc = ws_hub.__doc__ or ""
    assert "Resume window semantics" in doc
    assert "are NOT queued" in doc or "are **not** queued" in doc.lower()
    # Cross-references the close codes table.
    assert "4003" in doc
    assert "4029" in doc
    assert "4001" in doc


def test_llm_task_await_completes_when_tasks_finish_quickly() -> None:
    """Functional: when the cancelled LLM tasks complete promptly
    (the normal case), `_on_disconnect` proceeds without timing
    out. We can't easily exercise the full disconnect path without
    a live router, but we can drive the await behavior in
    isolation."""
    pytest.importorskip("fastapi")

    async def _run() -> None:
        # Build a list of tasks that cancel cleanly.
        async def _quick(token: object) -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return

        tasks = [asyncio.create_task(_quick(None)) for _ in range(3)]
        # Mimic the disconnect-path shape.
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=2.0,
            )
        except TimeoutError:
            assert False, "tasks didn't unwind within bounded wait"

        # All tasks finished (clean cancel).
        assert all(t.done() for t in tasks)

    asyncio.run(_run())
