"""Server-side Markdown rendering for the webapp chat page.

Assistant replies render as sanitized HTML; user messages stay plain.
Covers (1) the converter produces the expected formatting, (2) the
sanitizer neutralizes the XSS vectors this newly-rendered HTML exposes,
and (3) the `_message.html` template wires role → markdown vs plain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bp_agents.agents.webapp.markdown import render_markdown


def test_basic_formatting_rendered() -> None:
    html = str(render_markdown("# Title\n\nSome **bold** and `code`."))
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_lists_code_blocks_and_tables() -> None:
    html = str(render_markdown("- a\n- b"))
    assert "<ul>" in html and html.count("<li>") == 2

    fenced = str(render_markdown("```\nprint(1)\n```"))
    assert "<pre>" in fenced and "<code>" in fenced

    table = str(render_markdown("| h |\n| - |\n| v |"))
    assert "<table>" in table and "<td>" in table


def test_safe_links_kept_and_hardened() -> None:
    html = str(render_markdown("[ok](https://example.com)"))
    assert 'href="https://example.com"' in html
    # nh3 adds rel hardening to anchors.
    assert "noopener" in html and "nofollow" in html


# --- XSS posture: this is the webapp's first rendered HTML -----------------

def test_raw_html_in_source_is_not_live() -> None:
    # html=False escapes literal tags — present only as inert text.
    html = str(render_markdown("<script>alert(1)</script>"))
    assert "<script" not in html
    assert "&lt;script&gt;" in html


def test_javascript_url_scheme_stripped() -> None:
    # markdown-it rejects the javascript: link, so no live anchor is
    # emitted (the text remains, inert).
    html = str(render_markdown("[click](javascript:alert(1))"))
    assert 'href="javascript' not in html.lower()
    assert "<a " not in html


def test_event_handler_image_stripped() -> None:
    # A raw <img onerror=...> must not survive as a live tag/attribute;
    # html=False escapes the whole thing to inert text.
    html = str(render_markdown('<img src=x onerror="alert(1)">'))
    assert "<img" not in html


def test_empty_input_is_safe() -> None:
    assert str(render_markdown("")) == ""


# --- template wiring -------------------------------------------------------

def _message_template():  # noqa: ANN202
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tdir = (
        Path(__file__).resolve().parents[1]
        / "bp_agents" / "agents" / "webapp" / "templates"
    )
    env = Environment(
        loader=FileSystemLoader(str(tdir)),
        autoescape=select_autoescape(("html",)),
    )
    env.filters["markdown"] = render_markdown
    return env.get_template("chat/_message.html")


def test_template_renders_assistant_markdown() -> None:
    out = _message_template().render(
        role="assistant", content="**hi**", tag="", files=[], session_id="s1"
    )
    assert "<strong>hi</strong>" in out


def test_template_leaves_user_text_plain_and_escaped() -> None:
    out = _message_template().render(
        role="user", content="**not bold** <b>x</b>", tag="", files=[], session_id="s1"
    )
    # User text is NOT markdown-rendered and IS auto-escaped.
    assert "<strong>" not in out
    assert "**not bold**" in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
