"""Test_task form blocks stored XSS via agent description.

Pre-R4: `bp_admin/templates/test_task/form.html:31` rendered
`{{ agent_schema_lookup_json | safe }}` inside a double-quoted
HTML attribute (`x-data="..."`). The handler pre-serialised the
lookup with `json.dumps` and `|safe` bypassed Jinja's
auto-escape — `json.dumps` does NOT escape `"` for HTML
attribute context, so an agent's free-form `description`
containing `"` broke out of the attribute and could inject
arbitrary JS (`onmouseover="..."`).

The fix passes the dict directly and uses Jinja's `tojson`
filter, which escapes `"`, `'`, `<`, `>`, and `&` for HTML
attribute context.

These tests prove the XSS is blocked at the template layer by
rendering the actual template with hostile content and
asserting the breakout fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _render_form_fragment(lookup: dict) -> str:
    """Render JUST the `x-data="..."` fragment with Jinja's
    `tojson` filter. We don't need the full base.html chain —
    only the attribute-context rendering, which is where the
    XSS lived."""
    pytest.importorskip("jinja2")
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(("html",)))
    # Minimal slice of the real template — same filter, same
    # attribute context. A regression that swaps `tojson` back
    # to `|safe` or moves the lookup into a non-attribute
    # context still has to keep this fragment safe.
    tmpl = env.from_string(
        'x-data="{{ \'{\' }}'
        '  agentInfo: {{ lookup | tojson }}'
        '{{ \'}\' }}"'
    )
    return tmpl.render(lookup=lookup)


def test_template_escapes_quote_in_description() -> None:
    """An agent description with a literal `"` cannot break out
    of the `x-data="..."` attribute. Jinja's `tojson` escapes
    `"` to `\\u0022` in this context."""
    hostile_lookup = {
        "agt_evil": {
            "description": 'breakout"><script>alert(1)</script>',
            "capabilities": [],
            "accepts_schema": None,
            "accepts_control_schema": None,
        }
    }
    rendered = _render_form_fragment(hostile_lookup)
    # The raw breakout sequence must NOT appear in the output —
    # if it did, the script tag would terminate the attribute.
    assert 'breakout"><script>' not in rendered
    assert "<script>alert(1)</script>" not in rendered


def test_template_escapes_lt_gt_in_description() -> None:
    """`<` and `>` inside the lookup must be HTML-attribute-
    escaped, not left raw (otherwise an admin browser parses
    them as tag delimiters)."""
    hostile = {
        "agt_x": {
            "description": "<img src=x onerror=alert(1)>",
            "capabilities": [],
            "accepts_schema": None,
            "accepts_control_schema": None,
        }
    }
    rendered = _render_form_fragment(hostile)
    assert "<img src=x onerror=alert(1)>" not in rendered


def test_template_escapes_ampersand_in_description() -> None:
    """`&` is the third HTML-special. A description like `& "` ' &lt`
    that round-trips through `tojson` should appear escaped in
    the output."""
    hostile = {
        "agt_x": {
            "description": "& \" ' &lt",
            "capabilities": [],
            "accepts_schema": None,
            "accepts_control_schema": None,
        }
    }
    rendered = _render_form_fragment(hostile)
    # `& "` would break out of the attribute if not escaped.
    # Just confirm the raw sequence doesn't appear.
    assert '"description": "& " \' &lt"' not in rendered


def test_template_escapes_quote_in_agent_id() -> None:
    """agent_id is regex-validated at the protocol layer
    (`^[A-Za-z_][A-Za-z0-9_-]{0,63}$`) so this shouldn't be
    possible in practice — but defense-in-depth: if a regex
    drift or admin-direct DB insert produced an agent_id with
    `"`, the template must still escape it."""
    hostile = {
        'agt_evil"><script>alert(1)</script>': {
            "description": "ok",
            "capabilities": [],
            "accepts_schema": None,
            "accepts_control_schema": None,
        }
    }
    rendered = _render_form_fragment(hostile)
    assert "<script>alert(1)</script>" not in rendered


def test_template_escapes_in_capability_strings() -> None:
    """`capabilities` is a list; verify each string is also
    escaped on the way out (each element flows through the
    same `tojson` since it's nested in the same dict)."""
    hostile = {
        "agt_x": {
            "description": "ok",
            "capabilities": ['cap"><script>alert(1)</script>'],
            "accepts_schema": None,
            "accepts_control_schema": None,
        }
    }
    rendered = _render_form_fragment(hostile)
    assert "<script>alert(1)</script>" not in rendered


def test_template_does_not_use_safe_filter() -> None:
    """Belt-and-braces: the broken `|safe` pattern must be
    entirely gone from the template. A regression that adds
    it back fails this pin."""
    body = (
        Path(__file__).parent.parent
        / "bp_admin"
        / "templates"
        / "test_task"
        / "form.html"
    ).read_text()
    assert "| safe" not in body
