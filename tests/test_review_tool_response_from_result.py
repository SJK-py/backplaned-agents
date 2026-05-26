"""`Message.tool_response_from_result` — auto-thread the child's
`result.output.files` (router-managed file-store NAMES the producer
chose to surface) into the LLM tool response as
`{"file_ref": {"name": …}}` parts alongside `result.output.content`.

The router resolves each name into the provider call (bytes never
cross a frame), scoped to the caller's task. Layers:

  * No `output.files` → text-only wire output identical to
    `tool_response(response=result.output.content)`. Zero
    multimodal-envelope cost on the common case.
  * One or more names → multimodal `response=[{"text": content?},
    {"file_ref": {"name": n}}, ...]`. Order preserved; the modality
    (`image` / `document`) is inferred at the ROUTER from the named
    blob's mime type, not chosen here.
  * No content and no files → empty string (a valid empty tool
    result).
  * Errored / cancelled results pass names through; the contract is
    best-effort across statuses (the caller branches on
    `result.status` if strict-success matters).
"""

from __future__ import annotations

import pytest

from bp_protocol.frames import ResultFrame
from bp_protocol.types import AgentOutput


def _result(
    *,
    content: str | None = None,
    files: list[str] | None = None,
    status: str = "succeeded",
) -> ResultFrame:
    has_output = content is not None or bool(files)
    return ResultFrame(
        agent_id="child",
        trace_id="0" * 32,
        span_id="0" * 16,
        task_id="t",
        status=status,
        status_code=200,
        output=(
            AgentOutput(content=content, files=list(files or []))
            if has_output
            else None
        ),
    )


# ---------------------------------------------------------------------------
# No names: text-only fallback (the common case)
# ---------------------------------------------------------------------------


def test_no_files_yields_plain_text_response() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1",
        name="search",
        result=_result(content="hello world"),
    )
    assert msg.role == "tool"
    assert msg.tool_call_id == "tc_1"
    assert msg.name == "search"
    assert msg.content == "hello world"


def test_no_output_yields_empty_string() -> None:
    """An LLM seeing an empty tool result is a valid conversational
    state — the helper passes through `""` rather than synthesising
    an "OK" marker."""
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="noop",
        result=_result(),  # output=None
    )
    assert msg.content == ""


# ---------------------------------------------------------------------------
# With names: multimodal envelope
# ---------------------------------------------------------------------------


def test_one_name_yields_multipart_response() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="render_chart",
        result=_result(content="rendered", files=["chart.png"]),
    )
    assert isinstance(msg.content, list)
    assert msg.content[0] == {"text": "rendered"}
    assert msg.content[1] == {"file_ref": {"name": "chart.png"}}


def test_empty_content_with_names_omits_text_part() -> None:
    """A producer that returned no text content but did surface files
    lands as a parts list WITHOUT an empty text part — Gemini in
    particular rejects parts with empty `text` fields."""
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="screenshot",
        result=_result(files=["shot.png"]),
    )
    assert isinstance(msg.content, list)
    # Only the file_ref part, no leading {"text": ""}.
    assert len(msg.content) == 1
    assert msg.content[0] == {"file_ref": {"name": "shot.png"}}


def test_multiple_names_preserve_order() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    names = [f"img{i}.png" for i in range(3)]
    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="multi",
        result=_result(content="three frames", files=names),
    )
    assert isinstance(msg.content, list)
    assert len(msg.content) == 4  # 1 text + 3 file_refs
    assert [p["file_ref"]["name"] for p in msg.content[1:]] == names


def test_persist_name_threaded_verbatim() -> None:
    """A `persist/`-scoped name is threaded unchanged — the router
    resolves the scope; the helper never rewrites the name."""
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="report",
        result=_result(content="here", files=["persist/r.pdf"]),
    )
    assert msg.content[1] == {"file_ref": {"name": "persist/r.pdf"}}


# ---------------------------------------------------------------------------
# Status-agnostic behaviour
# ---------------------------------------------------------------------------


def test_errored_result_with_names_still_threads() -> None:
    """The helper passes names through regardless of status — result
    file delivery is best-effort across statuses. A caller that wants
    strict-success semantics branches on `result.status` BEFORE
    invoking the helper."""
    pytest.importorskip("fastapi")
    from bp_sdk.llm import Message

    msg = Message.tool_response_from_result(
        tool_call_id="tc_1", name="flaky",
        result=_result(
            content="partial output before failure",
            files=["partial.png"],
            status="failed",
        ),
    )
    assert isinstance(msg.content, list)
    assert msg.content[1] == {"file_ref": {"name": "partial.png"}}
