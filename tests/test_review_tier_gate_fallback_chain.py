"""Tier gate must account for the WHOLE fallback chain, not just the
requested preset.

Second-pass regression: the dispatch tier gate (review fix #72) resolved the
caller's trusted ``user_level`` only when the *requested* preset was gated
(``min_user_level != "*"``). For a ``*`` (ungated) preset whose
``fallback_preset`` chain contains a gated preset, it left ``user_level=None``
— and ``_call_with_fallback`` re-checks the gate on every fallback hop, where
``user_level_satisfies(None, "tierN")`` is False. So the gated fallback was
SILENTLY SKIPPED for *every* caller, regardless of their real tier — breaking
the documented "mix permissive + restricted-tier presets in a single chain"
configuration (degraded resilience exactly when the primary is failing).

The fix adds ``LlmService.chain_needs_tier`` (walks the whole chain) so the
gate resolves the level whenever any preset in the chain is gated, while
keeping the ungated-hot-path lookup-free and never refusing a ``*`` request.
"""

from __future__ import annotations

from tests.conftest import make_llm_service, make_preset


def _svc_with(*presets):  # type: ignore[no-untyped-def]
    svc = make_llm_service()
    for p in presets:
        svc._register_preset_for_test(p)
    return svc


def test_star_preset_with_gated_fallback_needs_tier() -> None:
    """THE regression: an ungated primary whose fallback is tier-gated must
    report needs-tier so dispatch resolves the level (else the gated fallback
    is skipped for everyone)."""
    svc = _svc_with(
        make_preset("primary", min_user_level="*", fallback_preset="restricted"),
        make_preset("restricted", min_user_level="tier0"),
    )
    assert svc.chain_needs_tier("primary") is True


def test_star_preset_with_ungated_fallback_does_not_need_tier() -> None:
    """Hot path preserved: a fully-ungated chain needs no level lookup."""
    svc = _svc_with(
        make_preset("primary", min_user_level="*", fallback_preset="backup"),
        make_preset("backup", min_user_level="*"),
    )
    assert svc.chain_needs_tier("primary") is False


def test_single_star_preset_does_not_need_tier() -> None:
    svc = _svc_with(make_preset("solo", min_user_level="*"))
    assert svc.chain_needs_tier("solo") is False


def test_gated_first_preset_needs_tier() -> None:
    svc = _svc_with(make_preset("restricted", min_user_level="tier0"))
    assert svc.chain_needs_tier("restricted") is True


def test_gate_deep_in_chain_is_detected() -> None:
    """A gated preset two hops down the chain still triggers needs-tier."""
    svc = _svc_with(
        make_preset("a", min_user_level="*", fallback_preset="b"),
        make_preset("b", min_user_level="*", fallback_preset="c"),
        make_preset("c", min_user_level="tier1"),
    )
    assert svc.chain_needs_tier("a") is True


def test_unknown_preset_does_not_need_tier() -> None:
    """Unknown name → False; the generate path raises PresetUnknownError on
    its own, and we must not over-trigger a level lookup / refusal."""
    svc = _svc_with(make_preset("known", min_user_level="*"))
    assert svc.chain_needs_tier("does-not-exist") is False


def test_chain_with_cycle_terminates_and_detects_gate() -> None:
    """walk_fallback_chain caps cycles; chain_needs_tier must not hang and
    still detect a gated member."""
    svc = _svc_with(
        make_preset("x", min_user_level="*", fallback_preset="y"),
        make_preset("y", min_user_level="tier0", fallback_preset="x"),  # cycle
    )
    assert svc.chain_needs_tier("x") is True
