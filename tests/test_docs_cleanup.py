"""Tests for the docs cleanup bundle (review M9 + capability-matrix split).

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

import pytest


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
