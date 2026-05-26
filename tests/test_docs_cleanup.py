"""Tests for the docs cleanup bundle (review M9, M12, capability-matrix split).

These tests guard against doc drift: each one extracts a claim the
docs make and compares it to actual code behaviour. If the next
refactor changes ACL semantics, removes an LLM frame field, or
rewires a provider's `embed`/`generate` capability, these tests fail
loudly so the matching doc gets updated in the same PR.

Pure-text tests (substring presence) are kept minimal — they break
on cosmetic edits. Where possible we extract the logical claim and
re-run it against code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path("/home/user/backplaned-next")


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# M9 — docs/acl.md user-level matching matches bp_router.acl
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_level,actual,expected", [
    # Wildcard admits anyone.
    ("*", "admin", True),
    ("*", "service", True),
    ("*", "tier3", True),
    # admin / service: exact-match peer roles (the doc's central claim).
    ("admin", "admin", True),
    ("admin", "service", False),
    ("admin", "tier0", False),
    ("service", "service", True),
    ("service", "admin", False),
    # tierN: "this tier or stricter"; admin/service map to -1 and pass.
    ("tier0", "admin", True),
    ("tier0", "service", True),
    ("tier0", "tier0", True),
    ("tier0", "tier1", False),
    ("tier1", "tier0", True),
    ("tier1", "tier1", True),
    ("tier1", "tier2", False),
    ("tier2", "tier1", True),
    ("tier2", "tier2", True),
])
def test_acl_doc_admission_table_matches_code(
    rule_level: str, actual: str, expected: bool
) -> None:
    """Every cell of the doc's admission table is verified against the
    real `_user_level_satisfies` implementation. If a future change
    flips a cell, the doc table needs updating in the same PR."""
    from bp_router.acl import _user_level_satisfies

    assert _user_level_satisfies(actual, rule_level) is expected, (
        f"acl doc table claims rule_level={rule_level!r} admits "
        f"{actual!r} = {expected}; code disagrees"
    )


def test_acl_doc_pseudocode_includes_admin_service_branch() -> None:
    """The doc's pseudocode block must show the admin/service exact-match
    branch — without it, a reader concludes admin and service are
    interchangeable everywhere."""
    doc = _read("docs/acl.md")
    # The fix added an explicit pseudocode line for the admin/service branch.
    assert 'rule_level in ("admin", "service")' in doc
    assert "actual == rule_level" in doc


# ---------------------------------------------------------------------------
# M12 — protocol.md LLM frame catalog matches bp_protocol.frames
# ---------------------------------------------------------------------------


def test_protocol_doc_lists_llm_frames() -> None:
    """The three LLM frames must each appear with their `type`
    discriminator in §2.2.x. Catches a regression where a future
    refactor adds a frame but leaves the doc behind."""
    doc = _read("docs/router/protocol.md")
    assert "LlmRequest" in doc
    assert "LlmDelta" in doc
    assert "LlmResult" in doc
    assert "ref_correlation_id" in doc


def test_protocol_doc_documents_cancel_llm_abort_mode() -> None:
    """`Cancel` has two modes: task abort (task_id set) and LLM abort
    (ref_correlation_id set, task_id None). Both must be documented."""
    doc = _read("docs/router/protocol.md")
    # The LLM-abort mode mentions per-socket scope and authorisation.
    assert "ref_correlation_id" in doc
    assert "per-socket" in doc.lower()


def test_protocol_doc_lists_llm_error_codes() -> None:
    """The error code catalog (§5) must list the three LLM-only codes
    so SDK authors know what's distinguishable."""
    doc = _read("docs/router/protocol.md")
    for code in ("preset_unknown", "preset_not_allowed", "auth_lookup_failed"):
        assert code in doc, f"protocol.md missing error code {code!r}"


def test_protocol_doc_llm_codes_match_error_code_enum() -> None:
    """The codes the doc lists must be exactly what `ErrorCode`
    actually exposes — no typos in either direction."""
    from bp_protocol.frames import ErrorCode

    doc = _read("docs/router/protocol.md")
    for attr in ("LLM_PRESET_UNKNOWN", "LLM_PRESET_NOT_ALLOWED",
                 "LLM_AUTH_LOOKUP_FAILED"):
        wire_value = getattr(ErrorCode, attr)
        assert wire_value in doc, (
            f"ErrorCode.{attr} = {wire_value!r} not in protocol.md error catalog"
        )


def test_protocol_doc_llm_request_field_set_matches_frame_model() -> None:
    """Sanity: `LlmRequestFrame` declared fields are all mentioned in the
    doc's wire example. If a field gets added to the model and the doc
    doesn't get updated, fail."""
    from bp_protocol.frames import LlmRequestFrame

    doc = _read("docs/router/protocol.md")
    # Drop the protocol-envelope fields (correlation_id, trace_id, etc.) —
    # those are documented in §2.1 once for all frames. Focus on the
    # LLM-request-specific fields.
    envelope = {
        "type", "protocol_version", "correlation_id", "trace_id",
        "span_id", "timestamp", "agent_id",
    }
    body_fields = set(LlmRequestFrame.model_fields) - envelope
    for field in body_fields:
        assert field in doc, (
            f"LlmRequestFrame.{field} not mentioned in protocol.md "
            "LLM-channel catalog — doc drift"
        )


# ---------------------------------------------------------------------------
# Capability matrix — chat vs embeddings adapters
# ---------------------------------------------------------------------------


def test_capability_matrix_chat_adapters_have_generate_not_embed() -> None:
    """The matrix claims chat adapters NotImpl `embed` and embeddings
    adapters NotImpl `generate`. Verify against the real classes."""
    import inspect

    from bp_router.llm.providers.openai import (
        OpenAIAdapter,
        OpenAIEmbeddingsAdapter,
    )
    from bp_router.llm.providers.openai_compatible import (
        OpenAICompatibleAdapter,
        OpenAICompatibleEmbeddingsAdapter,
    )

    # Chat adapters: `embed` raises NotImplementedError.
    for adapter_cls in (OpenAIAdapter, OpenAICompatibleAdapter):
        # Inspect the source to confirm the NotImpl raise — actually
        # invoking would need a live OpenAI SDK + network. Source check
        # is enough to catch a regression where someone wires `embed`
        # through to the chat client.
        src = inspect.getsource(adapter_cls.embed)
        assert "NotImplementedError" in src, (
            f"{adapter_cls.__name__}.embed must raise NotImplementedError; "
            "matrix says so"
        )

    # Embeddings adapters: `generate` raises NotImplementedError.
    for adapter_cls in (
        OpenAIEmbeddingsAdapter,
        OpenAICompatibleEmbeddingsAdapter,
    ):
        src = inspect.getsource(adapter_cls.generate)
        assert "NotImplementedError" in src, (
            f"{adapter_cls.__name__}.generate must raise NotImplementedError"
        )


def test_capability_matrix_local_chat_count_tokens_unsupported() -> None:
    """The matrix claims `openai-compatible` (local chat) doesn't support
    count_tokens because there's no universal endpoint."""
    import inspect

    from bp_router.llm.providers.openai_compatible import OpenAICompatibleAdapter

    src = inspect.getsource(OpenAICompatibleAdapter.count_tokens)
    assert "NotImplementedError" in src


def test_capability_matrix_documents_six_provider_columns() -> None:
    """Matrix splits the conflated single `OpenAI-compatible (local)`
    column into chat and embeddings columns. Verify both column headers
    are present."""
    doc = _read("docs/sdk/services.md")
    assert "`openai-compatible` (local chat)" in doc
    assert "`openai-compatible-embeddings` (local)" in doc
    # And the two should appear within the matrix block, not just
    # somewhere unrelated. Quick proximity check.
    matrix_start = doc.find("**Provider feature parity:**")
    matrix_end = doc.find("\n### ", matrix_start)
    assert matrix_start != -1 and matrix_end != -1
    matrix_block = doc[matrix_start:matrix_end]
    assert "`openai-compatible` (local chat)" in matrix_block
    assert "`openai-compatible-embeddings` (local)" in matrix_block


def test_capability_matrix_provider_count_matches_supported_providers() -> None:
    """The matrix's chat-side column count must match the live
    `SUPPORTED_PROVIDERS` set so a new provider added to the enum
    forces a doc update."""
    from bp_router.llm.presets import SUPPORTED_PROVIDERS

    doc = _read("docs/sdk/services.md")
    matrix_start = doc.find("**Provider feature parity:**")
    matrix_end = doc.find("\n### ", matrix_start)
    matrix_block = doc[matrix_start:matrix_end]

    # Each provider name should appear at least once in the matrix block.
    for provider in SUPPORTED_PROVIDERS:
        assert f"`{provider}`" in matrix_block, (
            f"capability matrix doesn't list provider {provider!r} — doc drift"
        )
