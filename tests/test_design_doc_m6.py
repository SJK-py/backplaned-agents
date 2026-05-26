"""Lightweight checks on the M6 design doc (`docs/design/llm-retriable-errors.md`).

The doc proposes constants and code names. Once PR #1 (the protocol
bump) lands, these constants will exist in `bp_protocol.frames`. Until
then, the doc is the source of truth; this test guards against
drift between what the doc claims will exist and what the doc says
the wire format will be.

Run while the design is in "draft for review" status — once PR #1
lands and ships actual `ErrorCode` constants, this file flips to
checking that the live constants match the doc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOC_PATH = Path("/home/user/backplaned-next/docs/design/llm-retriable-errors.md")


def _read_doc() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Doc structure
# ---------------------------------------------------------------------------


def test_doc_exists_and_is_marked_draft() -> None:
    """While the design is up for review, the doc must self-identify
    as draft so contributors don't act on it as if it were final."""
    doc = _read_doc()
    assert "**Status:** draft" in doc


def test_doc_has_all_required_sections() -> None:
    """Reviewable design docs need a stable shape — problem, design
    space, recommendation, wire shape, sequencing. If a section gets
    deleted before review, fail loud."""
    doc = _read_doc()
    required = [
        "## 1. The bug today",
        "## 2. Goal",
        "## 3. Design space",
        "### Recommendation",
        "## 4. Wire shape",
        "## 5. Per-provider exception classifier",
        "## 6. Streaming retry timing",
        "## 7. SDK retry policy",
        "### 7.1 Visible retry-pending hint during streaming setup",
        "## 8. Implementation sequence",
        "## 9. Migration / compatibility",
        "## 10. What not to do",
        # §11 was "Open questions" in the first draft; resolved into
        # "Resolved design decisions" once reviewer answers landed.
        "## 11. Resolved design decisions",
    ]
    for header in required:
        assert header in doc, f"design doc missing section: {header!r}"


# ---------------------------------------------------------------------------
# Wire-shape claims
# ---------------------------------------------------------------------------


_PROPOSED_CODES = (
    "upstream_timeout",
    "upstream_rate_limited",
    "upstream_unavailable",
    "upstream_invalid_request",
    "upstream_auth_failed",
    "upstream_content_filter",
    "upstream_quota_exhausted",
    "stream_interrupted",
)


@pytest.mark.parametrize("code", _PROPOSED_CODES)
def test_doc_lists_each_proposed_error_code(code: str) -> None:
    """Each new wire code must appear in §3's typed-codes table.
    Catches a regression where someone adds a constant in §4 but
    forgets to update the table that justifies it."""
    doc = _read_doc()
    assert code in doc, (
        f"proposed error code {code!r} not mentioned in design doc"
    )


def test_doc_pins_recommendation_to_option_c_hybrid() -> None:
    """The doc explicitly recommends Option C. If a future edit
    flips to A or B, the implementation sequence in §8 needs a
    rewrite — fail loud so the inconsistency is caught."""
    doc = _read_doc()
    # Match the exact recommendation line.
    assert "**Option C (hybrid).**" in doc


def test_doc_specifies_retriable_codes_subset() -> None:
    """The doc proposes which codes are retriable. Verify the
    `RETRIABLE_LLM_CODES` table appears with the expected members."""
    doc = _read_doc()
    # The frozenset literal block.
    assert "RETRIABLE_LLM_CODES" in doc
    # Retriable subset.
    for retriable in (
        "upstream_timeout",
        "upstream_rate_limited",
        "upstream_unavailable",
        "internal_error",
    ):
        assert retriable in doc


def test_doc_marks_invalid_request_as_not_retriable() -> None:
    """`upstream_invalid_request` (400 / bad prompt) MUST NOT be in
    the retriable set — retrying it would just burn credits on the
    same broken input."""
    doc = _read_doc()
    # The §3 table should explicitly mark it as not retriable.
    # Check by line containment — we just need the doc to claim "no"
    # somewhere on the same line as the code name.
    for line in doc.split("\n"):
        if "upstream_invalid_request" in line and "|" in line:
            assert "no" in line.lower(), (
                f"upstream_invalid_request line in §3 doesn't mark "
                f"as not retriable: {line!r}"
            )
            break
    else:
        pytest.fail("§3 typed-codes table doesn't have an upstream_invalid_request row")


def test_doc_marks_stream_interrupted_as_not_retriable() -> None:
    """A connection drop after deltas have been delivered cannot be
    retried — agent has partial output. Must be explicit."""
    doc = _read_doc()
    for line in doc.split("\n"):
        if "stream_interrupted" in line and "|" in line:
            assert "no" in line.lower(), (
                f"stream_interrupted line in §3 doesn't mark as not "
                f"retriable: {line!r}"
            )
            break
    else:
        pytest.fail("§3 doesn't have a stream_interrupted row")


# ---------------------------------------------------------------------------
# §11 resolved decisions — four reviewer answers folded into the doc
# ---------------------------------------------------------------------------


def _section_text(doc: str, heading: str) -> str:
    """Return the body of the section starting at `heading` up to the
    next markdown heading at the same level (or shallower)."""
    start = doc.find(heading)
    assert start != -1, f"section {heading!r} not in doc"
    level = len(heading) - len(heading.lstrip("#"))
    end = len(doc)
    cursor = start + len(heading)
    while cursor < len(doc):
        nl = doc.find("\n", cursor)
        if nl == -1:
            break
        line_start = nl + 1
        next_hash = doc.find("#", line_start)
        if next_hash == -1:
            break
        line_end = doc.find("\n", next_hash)
        if line_end == -1:
            line_end = len(doc)
        candidate = doc[line_start:line_end]
        if candidate.startswith("#"):
            cand_level = len(candidate) - len(candidate.lstrip("#"))
            if cand_level <= level:
                end = line_start
                break
        cursor = line_end
    return doc[start:end]


def test_resolution_internal_error_stays_retriable() -> None:
    """§11.1: `internal_error` stays in `RETRIABLE_LLM_CODES`. The
    review-time alternative (split into `internal_error` not retriable
    + `internal_retriable`) was rejected — the catch-all bucket exists
    for unknown transients."""
    doc = _read_doc()
    section = _section_text(doc, "### 11.1")
    assert "KEEP" in section
    assert "RETRIABLE_LLM_CODES" in section


def test_resolution_backoff_cap_shrunk_to_10s() -> None:
    """§11.2: `max_backoff_s` cap shrinks from 30s (first draft) to
    10s. The §7 RetryPolicy code block must reflect the new value."""
    doc = _read_doc()
    section_11_2 = _section_text(doc, "### 11.2")
    assert "10.0" in section_11_2 or "10s" in section_11_2
    section_7 = _section_text(doc, "## 7. SDK retry policy")
    assert "max_backoff_s: float = 10.0" in section_7


def test_resolution_total_attempts_cap_added() -> None:
    """§11.3: yes — add `total_attempts_cap` to `RetryPolicy`. Default
    8. Bounds the SDK × router multiplication."""
    doc = _read_doc()
    section_11_3 = _section_text(doc, "### 11.3")
    assert "total_attempts_cap" in section_11_3
    section_7 = _section_text(doc, "## 7. SDK retry policy")
    assert "total_attempts_cap: int = 8" in section_7


def test_resolution_meta_field_on_llm_delta() -> None:
    """§11.4: retry-pending hint emitted as `LlmDelta.meta`, not a
    new frame type. Must include the mutual-exclusivity invariant."""
    doc = _read_doc()
    section_11_4 = _section_text(doc, "### 11.4")
    assert "LlmDelta.meta" in section_11_4 or "meta` field" in section_11_4
    assert "Mutual-exclusivity" in section_11_4 or "mutually exclusive" in section_11_4

    section_7_1 = _section_text(doc, "### 7.1")
    assert "LlmDeltaMeta" in section_7_1
    assert "retry_pending" in section_7_1
    assert "retry_after_seconds" in section_7_1


def test_no_open_questions_remain() -> None:
    """The first draft had `## 11. Open questions`. Once the four
    answers landed, the section heading flipped to "Resolved design
    decisions". Catches a regression where someone re-introduces an
    open question without a resolution."""
    doc = _read_doc()
    assert "## 11. Open questions" not in doc
    assert "## 11. Resolved design decisions" in doc


def test_streaming_retry_pseudocode_yields_meta_delta() -> None:
    """The §6 retry state machine must yield a meta delta during the
    backoff sleep; otherwise the UI spinner never gets a signal."""
    doc = _read_doc()
    section_6 = _section_text(doc, "## 6. Streaming retry timing")
    assert "LlmDeltaMeta" in section_6
    assert 'kind="retry_pending"' in section_6


def test_pr1_scope_includes_llm_delta_meta() -> None:
    """The protocol bump (PR #1) MUST land the `LlmDelta.meta` field
    alongside `LlmResultError`. If PR #1's row in §8 doesn't say so,
    PR #3 (streaming setup-retry) has nothing to emit into."""
    doc = _read_doc()
    section_8 = _section_text(doc, "## 8. Implementation sequence")
    pr1_row = next(
        line for line in section_8.split("\n")
        if line.startswith("| 1 |")
    )
    assert "LlmDeltaMeta" in pr1_row or "LlmDelta.meta" in pr1_row


# ---------------------------------------------------------------------------
# Implementation-sequence claims
# ---------------------------------------------------------------------------


def test_doc_implementation_sequence_lists_five_prs() -> None:
    """§8 lists five PRs. If the count drifts, the sequence prose
    above (PRs 1, 2, 3 are protocol-side; 4 is SDK; 5 is docs) is
    wrong too."""
    doc = _read_doc()
    section_8_start = doc.find("## 8. Implementation sequence")
    section_9_start = doc.find("## 9. Migration / compatibility")
    section_8 = doc[section_8_start:section_9_start]

    # Each row in the sequence table starts with `| <number> |`.
    pr_rows = sum(1 for line in section_8.split("\n")
                  if line.startswith("| 1 |") or line.startswith("| 2 |")
                  or line.startswith("| 3 |") or line.startswith("| 4 |")
                  or line.startswith("| 5 |"))
    assert pr_rows == 5, (
        f"§8 sequence table has {pr_rows} rows; expected 5"
    )


def test_doc_pr1_is_the_protocol_gate() -> None:
    """The doc identifies PR #1 as the gate — design decisions there
    block everything else. Make sure the prose says so."""
    doc = _read_doc()
    assert "PR 1 is" in doc and "gate" in doc


# ---------------------------------------------------------------------------
# Backwards-compatibility claim
# ---------------------------------------------------------------------------


def test_doc_promises_no_behavioural_regression_on_old_sdks() -> None:
    """The migration story must explicitly promise that old SDKs keep
    working. Catches a regression where someone "tightens" the design
    in a way that breaks pre-existing clients."""
    doc = _read_doc()
    section_9_start = doc.find("## 9. Migration / compatibility")
    section_10_start = doc.find("## 10. What not to do")
    section_9 = doc[section_9_start:section_10_start]

    assert "No behavioural regression" in section_9, (
        "§9 must explicitly promise no behavioural regression for "
        "old SDKs reading the existing error shape"
    )
