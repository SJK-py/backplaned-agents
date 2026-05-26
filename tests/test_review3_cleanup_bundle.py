"""Tests for the third-pass review cleanup bundle (M-4, M-5, M-6, M-7).

  - M-4: pool-acquire-per-iteration removed from `cancel_task`,
    `fail_inflight_for_agent`, `_sweep_once`, and `_gc_files_once`.
    Each holds ONE connection across the loop body; storage I/O
    and frame delivery happen AFTER the conn is released.
    `fail_task` accepts an optional `conn` so its callers can
    pass the held connection through.
  - M-5: per-field item-count caps on Pydantic frame fields that
    take `list[dict[str, Any]]` or similar fan-out types. Bytes
    cap at the WS layer doesn't bound item count — a 1 MiB
    payload of `[{}, {}, ...]` produces vastly more Python-object
    overhead than the wire bytes suggest.
  - M-6: admin login failure log no longer carries the email.
    Router-side audit row already captures it; duplicating in
    the BFF log puts PII into shipped log streams.
  - M-7: `Settings.log_prompts` and its prod-blocking validator
    are deleted — the field had zero readers, so the validator
    gave a false sense of "this is gated."
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# M-4: hoisted pool acquires
# ===========================================================================


def _count_pool_acquires(src: str) -> int:
    """Count actual `async with pool.acquire()` call sites — match
    only the with-block prefix so comments mentioning
    "pool.acquire()" don't inflate the count."""
    return src.count("async with pool.acquire()")


def test_m4_cancel_task_uses_single_pool_acquire_in_loop() -> None:
    """`cancel_task`'s per-target loop must run inside ONE
    `async with pool.acquire() as conn:` block — not acquire
    again per iteration. Pin via source inspection."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.cancel_task)
    n_acquires = _count_pool_acquires(src)
    assert n_acquires == 1, (
        f"review3-M4 regression: cancel_task acquires the pool "
        f"{n_acquires} times (expected 1) — per-iteration acquires "
        "back in the loop"
    )
    # The fan-out plan pattern — collect first, deliver after
    # release — must be present.
    assert "fanout_plan" in src


def test_m4_fail_inflight_holds_one_conn_across_loop() -> None:
    """`fail_inflight_for_agent` must pass the held conn through to
    each `fail_task` so the pool isn't acquired per row."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.fail_inflight_for_agent)
    assert _count_pool_acquires(src) == 1
    assert "conn=conn" in src, (
        "review3-M4: fail_task must be called with the held conn"
    )


def test_m4_sweep_once_holds_one_conn_across_loop() -> None:
    """`_sweep_once` must hold one connection across the
    deadline-exceeded fail loop, passing it through to
    `fail_task` (now conn-acceptable)."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module._sweep_once)
    assert _count_pool_acquires(src) == 1
    assert "conn=conn" in src


def test_m4_gc_files_once_holds_one_conn_across_loop() -> None:
    """`_gc_files_once` must batch the EXPIRY-SCAN DB work under one
    connection, then release BEFORE storage deletes (network I/O
    that would block the pool if held).

    R8 MEDIUM addendum: a SECOND short-lived `pool.acquire()` was
    added in the storage-delete phase to re-check `count_other_file_refs`
    immediately before each delete — closing the
    commit→storage-delete window where a concurrent cross-user
    dedup `insert_file` could re-reference the sha256 and get its
    bytes deleted out from under it. That re-check connection is
    held ONLY for the COUNT and released BEFORE the slow storage
    delete, so the M4 invariant (no per-file acquire during the
    big scan; no connection held across storage I/O) still holds —
    the literal acquire count is now 2, not 1."""
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module._gc_files_once)
    # One acquire for the expiry scan, one short re-check acquire in
    # the storage-delete phase. NOT a per-scan-row acquire (the M4
    # regression this guards against).
    assert _count_pool_acquires(src) == 2
    # The expiry scan still collects intents first, deletes storage
    # after release — plain-tuple loop var, not a row object.
    assert "storage_to_delete" in src
    assert "for file_id, sha256 in storage_to_delete" in src
    # The re-check must be present and must skip the delete when a
    # new owner re-referenced the content.
    assert "count_other_file_refs" in src
    assert "file_gc_storage_delete_skipped_reref" in src
    # The expiry scan loop must NOT acquire per row (the original
    # M4 hazard). The scan loop iterates `rows` from
    # find_expired_files; ensure no acquire sits inside it by
    # checking the acquire that wraps the scan precedes the
    # `for row in rows:` and the second acquire is AFTER the
    # storage_to_delete collection.
    scan_acquire = src.index("async with pool.acquire()")
    delete_loop = src.index("for file_id, sha256 in storage_to_delete")
    recheck_acquire = src.index(
        "async with pool.acquire()", scan_acquire + 1
    )
    assert scan_acquire < delete_loop < recheck_acquire, (
        "the re-check acquire must come AFTER the expiry scan and "
        "INSIDE the post-release storage-delete loop, not inside "
        "the expiry-scan loop"
    )


def test_m4_fail_task_accepts_optional_conn() -> None:
    """`fail_task` gained an optional `conn` parameter so callers
    that already hold a connection can pass it through. Without
    `conn`, behaviour is unchanged (acquires its own)."""
    from bp_router import tasks as tasks_module

    sig = inspect.signature(tasks_module.fail_task)
    assert "conn" in sig.parameters
    conn_param = sig.parameters["conn"]
    assert conn_param.default is None, (
        "conn must default to None so existing callers don't break"
    )


# ===========================================================================
# M-5: Pydantic frame field caps
# ===========================================================================


def test_m5_llm_request_messages_capped() -> None:
    """`LlmRequestFrame.messages` must reject lists exceeding the
    `_LLM_MAX_MESSAGES` cap. 1024 turns is generous for any real
    conversation; an oversize list is almost certainly an attack
    or a runaway agent."""
    from bp_protocol.frames import _LLM_MAX_MESSAGES, LlmRequestFrame

    too_big = [{"role": "user", "content": "x"}] * (_LLM_MAX_MESSAGES + 1)
    with pytest.raises(Exception) as excinfo:
        LlmRequestFrame(
            agent_id="a",
            trace_id="0" * 32,
            span_id="0" * 16,
            messages=too_big,
        )
    assert "messages" in str(excinfo.value).lower() or "length" in str(
        excinfo.value
    ).lower()


def test_m5_llm_request_tools_capped() -> None:
    from bp_protocol.frames import _LLM_MAX_TOOLS, LlmRequestFrame

    too_big = [{"name": f"t{i}", "schema": {}} for i in range(_LLM_MAX_TOOLS + 1)]
    with pytest.raises(Exception):
        LlmRequestFrame(
            agent_id="a",
            trace_id="0" * 32,
            span_id="0" * 16,
            tools=too_big,
        )


def test_m5_llm_request_text_capped_for_embed() -> None:
    from bp_protocol.frames import _LLM_MAX_EMBED_INPUTS, LlmRequestFrame

    too_big = ["x"] * (_LLM_MAX_EMBED_INPUTS + 1)
    with pytest.raises(Exception):
        LlmRequestFrame(
            agent_id="a",
            trace_id="0" * 32,
            span_id="0" * 16,
            kind="embed",
            text=too_big,
        )


def test_m5_llm_result_tool_calls_capped() -> None:
    from bp_protocol.frames import _LLM_MAX_TOOL_CALLS, LlmResultFrame

    too_big = [{"id": f"c{i}", "name": "t", "args": {}} for i in range(
        _LLM_MAX_TOOL_CALLS + 1
    )]
    with pytest.raises(Exception):
        LlmResultFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="cid",
            tool_calls=too_big,
        )


def test_m5_llm_result_vectors_capped() -> None:
    from bp_protocol.frames import _LLM_MAX_VECTORS, LlmResultFrame

    too_big = [[0.0]] * (_LLM_MAX_VECTORS + 1)
    with pytest.raises(Exception):
        LlmResultFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="cid",
            vectors=too_big,
        )


def test_m5_llm_result_reasoning_blocks_capped() -> None:
    from bp_protocol.frames import _LLM_MAX_REASONING_BLOCKS, LlmResultFrame

    too_big = [{"type": "thinking", "text": "x"}] * (_LLM_MAX_REASONING_BLOCKS + 1)
    with pytest.raises(Exception):
        LlmResultFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
            ref_correlation_id="cid",
            reasoning_blocks=too_big,
        )


def test_m5_caps_are_named_constants_not_inline_magic() -> None:
    """The caps live as `_LLM_MAX_*` module-level constants so
    operators can grep for them (and a future PR can promote them
    to settings if needed). Pin the names so a regression that
    inlines a magic number is caught."""
    from bp_protocol import frames as frames_module

    src = inspect.getsource(frames_module)
    expected = [
        "_LLM_MAX_MESSAGES",
        "_LLM_MAX_TOOLS",
        "_LLM_MAX_EMBED_INPUTS",
        "_LLM_MAX_TOOL_CALLS",
        "_LLM_MAX_REASONING_BLOCKS",
        "_LLM_MAX_VECTORS",
    ]
    for name in expected:
        assert name in src, f"review3-M5: cap constant {name} missing"


def test_m5_typical_workload_passes_caps() -> None:
    """Sanity-pin the happy path: a reasonable conversation
    (50 messages, 8 tools) must validate cleanly. If this fails
    after a cap tweak, the cap is too tight for legitimate use."""
    from bp_protocol.frames import LlmRequestFrame

    LlmRequestFrame(
        agent_id="a",
        trace_id="0" * 32,
        span_id="0" * 16,
        messages=[{"role": "user", "content": f"msg {i}"} for i in range(50)],
        tools=[{"name": f"t{i}", "schema": {}} for i in range(8)],
    )


# ===========================================================================
# M-6: admin login email PII removed from log
# ===========================================================================


def test_m6_admin_login_failed_log_does_not_include_email() -> None:
    """Source pin: `auth_pages.login_submit`'s `admin_login_failed`
    log call MUST NOT include `"email": email` in its `extra`
    payload. The router-side `auth.login_failed` audit row
    already captures the email scoped to the admin-only audit
    table; duplicating it in the BFF log puts PII into broader-
    access log streams (Loki / Cloud Logging shipping)."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import auth_pages

    src = inspect.getsource(auth_pages.login_submit)
    # Find the failed-log block specifically.
    idx = src.find("admin_login_failed")
    assert idx > 0
    # Look at the surrounding extra={...} block.
    extra_start = src.find("extra={", idx)
    extra_end = src.find("}", extra_start) + 1
    extra_block = src[extra_start:extra_end]
    assert '"email"' not in extra_block, (
        "review3-M6 regression: admin_login_failed log carries the "
        f"email PII: {extra_block}"
    )


# ===========================================================================
# M-7: log_prompts dead code deleted
# ===========================================================================


def test_m7_log_prompts_removed_from_settings() -> None:
    """The `log_prompts` field had zero readers and its
    prod-blocking validator gave a false sense of "this is
    gated" (the lever did nothing). Delete the field and
    the validator."""
    from bp_router import settings as settings_module

    src = inspect.getsource(settings_module)
    assert "log_prompts" not in src, (
        "review3-M7 regression: log_prompts field re-added to Settings"
    )
    assert "_no_prompt_logging_in_prod" not in src


def test_m7_log_prompts_not_referenced_anywhere() -> None:
    """Belt-and-braces: confirm no other module reads `log_prompts`
    (which would crash at runtime now that the field is gone)."""
    import subprocess

    result = subprocess.run(
        ["grep", "-rn", "log_prompts", "/home/user/backplaned-next/bp_router",
         "/home/user/backplaned-next/bp_admin", "/home/user/backplaned-next/bp_sdk",
         "/home/user/backplaned-next/bp_protocol"],
        capture_output=True,
        text=True,
    )
    assert result.stdout == "", (
        f"review3-M7 regression: log_prompts is still referenced:\n"
        f"{result.stdout}"
    )
