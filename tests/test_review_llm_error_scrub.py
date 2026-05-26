"""`_err_result` scrubs LLM upstream exception messages.

R4 second-pass review found that the typed-branch error paths
(LlmUpstreamError, StreamInterrupted) flow `str(exc)` from
provider SDK exceptions verbatim to `LlmResultFrame.error.message`
on the wire. Those messages can leak:
  - bearer tokens / api keys (some SDKs format Authorization
    headers into exception text)
  - request bodies (some SDKs serialize the failed request)
  - upstream endpoint hostnames / request IDs (internal infra)

The catch-all branch at `_handle_llm_request` already redacted to
"internal_error"; the typed branches did not. The scrubber
applied in `_err_result` bounds the message and strips obvious
secret patterns.
"""

from __future__ import annotations

import inspect

import pytest


def test_scrubber_strips_bearer_tokens() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    msg = "401 Unauthorized: Bearer sk-abc123def456 expired"
    out = _scrub_upstream_message(msg)
    assert "sk-abc123def456" not in out
    assert "Bearer ***" in out


def test_scrubber_strips_api_key_params() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    for raw in [
        "Bad request: api_key=AIzaSyAbc123",
        "config: api-key:SuperSecret123",
        "API_KEY=secret",
    ]:
        out = _scrub_upstream_message(raw)
        assert "Abc123" not in out
        assert "SuperSecret" not in out
        assert "secret" not in out or "api_key=***" in out


def test_scrubber_strips_openai_style_sk_keys() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    out = _scrub_upstream_message(
        "Provider error using key sk-proj-AbCdEf123456789 ghi"
    )
    assert "sk-proj-AbCdEf123456789" not in out
    assert "sk-***" in out


def test_scrubber_truncates_at_256_chars() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _SCRUB_MAX_LEN, _scrub_upstream_message

    long = "x" * 1000
    out = _scrub_upstream_message(long)
    assert len(out) == _SCRUB_MAX_LEN
    assert out.endswith("…")


def test_scrubber_passes_safe_messages_unchanged() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    safe = "Upstream returned 503 service unavailable"
    out = _scrub_upstream_message(safe)
    assert out == safe


def test_scrubber_handles_empty_message() -> None:
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    assert _scrub_upstream_message("") == ""


def test_err_result_uses_scrubber() -> None:
    """Source pin: `_err_result` calls `_scrub_upstream_message`
    on the `message` payload before building the wire frame.
    `_err_result` is a closure inside `_run_llm_call` (line 488)
    where the LLM service result is translated to a wire frame."""
    pytest.importorskip("fastapi")
    import bp_router.dispatch as dispatch

    src = inspect.getsource(dispatch._run_llm_call)
    assert "_scrub_upstream_message(message)" in src


def test_scrubber_does_not_strip_unrelated_text_resembling_keys() -> None:
    """Defensive: a message mentioning "sk-" outside a key-shaped
    context (e.g. a help URL) shouldn't be over-redacted. The
    regex requires `sk-` followed by `[A-Za-z0-9_-]{8,}` so a
    short hyphen-prefixed identifier (less than 8 chars) passes
    through."""
    pytest.importorskip("fastapi")
    from bp_router.dispatch import _scrub_upstream_message

    # `sk-1` is too short to match the regex; passes through.
    out = _scrub_upstream_message("Status code 503 (sk-1)")
    assert "sk-***" not in out
