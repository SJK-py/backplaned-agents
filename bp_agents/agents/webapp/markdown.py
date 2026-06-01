"""Server-side Markdown rendering for chat messages.

Assistant replies are authored in Markdown; the chat page used to show
them verbatim (`whitespace-pre-wrap`), so `**bold**`, headings, lists, and
fenced code appeared as literal source. This renders that Markdown to HTML
on the server, in the single `chat/_message.html` chokepoint both the
initial page load and the SSE stream pass through.

Security: this is the webapp's first rendered HTML, so the XSS posture
matters. Two layers:
  1. markdown-it runs with `html=False`, so any literal tags in the model
     output are escaped, not emitted as live markup;
  2. nh3 (ammonia) then sanitizes the result against an explicit tag /
     attribute allowlist and a safe URL-scheme list, stripping anything
     the first layer might let through (e.g. a crafted link protocol).
Only after both layers is the string wrapped in `markupsafe.Markup` so
Jinja emits it without re-escaping. User messages are NOT rendered through
this — they stay plain, auto-escaped text.
"""

from __future__ import annotations

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

# CommonMark + GFM tables, raw HTML disabled. `breaks` stays False so a
# single newline collapses (standard Markdown) — models separate
# paragraphs with blank lines, matching how Claude.ai / ChatGPT render.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

# Allowlist for the sanitizer. markdown-it only emits this small set; we
# pin it explicitly rather than trust nh3's broader default so adding a
# Markdown feature can't silently widen the rendered surface.
_ALLOWED_TAGS: set[str] = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "del", "s", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRS: dict[str, set[str]] = {"a": {"href", "title"}}
_URL_SCHEMES: set[str] = {"http", "https", "mailto"}


def render_markdown(text: str) -> Markup:
    """Render assistant Markdown to sanitized, Jinja-safe HTML."""
    html = _md.render(text or "")
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    return Markup(clean)
