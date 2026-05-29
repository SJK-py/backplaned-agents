"""Tests for Phase 9c — agent-detail rendering of accepts_control_schema.

Read-only render. Source pins on the template so:
  - the new schema panel only appears when the agent published a
    control-plane surface,
  - the data-plane / control-plane distinction is visually marked
    so operators can tell them apart at a glance,
  - the explanatory copy about build_tools invisibility is
    co-located with the panel that motivates it.
"""

from __future__ import annotations

from pathlib import Path


def _detail_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "agents"
        / "detail.html"
    ).read_text()


# ===========================================================================
# Conditional panel
# ===========================================================================


def test_schemas_section_renders_when_only_control_schema_set() -> None:
    """Pre-9c logic was `if accepts_schema or produces_schema` — an
    agent with ONLY a control schema would be missing the entire
    section. The new condition must include accepts_control_schema."""
    body = _detail_html()
    assert "_has_control" in body
    assert "_has_accepts or _has_produces or _has_control" in body


def test_control_schema_panel_renders_when_set() -> None:
    body = _detail_html()
    assert '{% if _has_control %}' in body
    assert 'agent.agent_info["accepts_control_schema"]' in body


def test_control_schema_panel_uses_tojson_pretty_print() -> None:
    """Mirror the accepts_schema / produces_schema rendering — the
    operator reads JSON, not Python repr."""
    body = _detail_html()
    # The panel renders the schema JSON-pretty-printed via the same
    # filter as the other two panels.
    assert 'agent.agent_info["accepts_control_schema"] | tojson(indent=2)' in body


# ===========================================================================
# Visual data-plane / control-plane distinction
# ===========================================================================


def test_data_plane_accepts_schema_has_badge() -> None:
    """Pre-9c the two panels were unlabelled — now that there's a
    third, the data-plane panel needs an explicit `data-plane`
    badge so the operator can tell them apart at a glance."""
    body = _detail_html()
    assert "data-plane" in body
    # Adjacent to the accepts_schema label.
    accepts_idx = body.index("Accepts (NewTask payload)")
    badge_idx = body.index("data-plane")
    assert abs(accepts_idx - badge_idx) < 500


def test_control_plane_panel_has_is_control_badge() -> None:
    body = _detail_html()
    assert "is_control" in body
    # The badge sits next to the control-plane label.
    label_idx = body.index("Accepts (control-plane)")
    badge_idx = body.index("is_control")
    assert abs(label_idx - badge_idx) < 500


def test_badges_use_distinct_colours() -> None:
    """Operator scanning the page needs the two surfaces to be
    visually distinct. Source pin on different Tailwind colour
    families so a future style refactor that collapses them is
    caught."""
    body = _detail_html()
    # Data-plane: emerald (safe / standard).
    assert "bg-emerald-50" in body
    # Control-plane: purple (distinct, signals "special intent").
    assert "bg-purple-50" in body


# ===========================================================================
# Explanatory copy
# ===========================================================================


def test_control_schema_section_explains_build_tools_invisibility() -> None:
    """The whole reason for the split is that control payloads are
    invisible to build_tools. That decision is non-obvious from
    the schemas alone — the section header carries the
    explanation when there's a control surface present."""
    body = _detail_html()
    # The header copy mentions build_tools and the LLM-tool
    # implication.
    assert "build_tools" in body
    assert "LLM picking this agent" in body or "LLM-callable" in body


def test_explanatory_copy_only_shows_when_control_schema_set() -> None:
    """An agent with no control surface shouldn't carry copy about
    build_tools invisibility — it would just be noise. The
    explanation is gated on `_has_control`."""
    body = _detail_html()
    # The explanation block lives inside `{% if _has_control %}`.
    control_intro_idx = body.index("{% if _has_control %}")
    build_tools_idx = body.index("build_tools")
    # Build-tools copy appears AFTER the conditional, not before.
    assert build_tools_idx > control_intro_idx


# ===========================================================================
# Layout — three-column grid scales without crowding
# ===========================================================================


def test_grid_scales_to_three_columns_on_large_viewports() -> None:
    """Pre-9c was `md:grid-cols-2` (max two side-by-side); with
    three possible panels we need a `lg:grid-cols-3` step so all
    three render legibly on a wide screen."""
    body = _detail_html()
    assert "lg:grid-cols-3" in body
    # Two-column step still present for medium viewports.
    assert "md:grid-cols-2" in body


def test_grid_uses_divider_lines_responsively() -> None:
    """On md+ horizontal dividers, on sm vertical stacking. Pin
    the responsive divider classes so the stacked-mobile layout
    doesn't end up with a 1px line between sections at narrow
    widths."""
    body = _detail_html()
    assert "divide-y" in body  # stacked
    assert "md:divide-y-0" in body  # disabled at md+
    assert "md:divide-x" in body  # vertical at md+


# ===========================================================================
# Backwards-compat — agents with NO control surface unaffected
# ===========================================================================


def test_pre_phase_9c_two_panel_layout_still_works() -> None:
    """An existing agent with only accepts_schema + produces_schema
    (no control surface) should still render its two panels as
    before. The template's `_has_*` flags + the conditional render
    keep this working."""
    body = _detail_html()
    # The data-plane and produces panels still gate on their own
    # `_has_*` flag — they don't depend on control being absent.
    assert "{% if _has_accepts %}" in body
    assert "{% if _has_produces %}" in body
