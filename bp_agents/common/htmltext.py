"""bp_agents.common.htmltext — minimal, stdlib-only HTML → visible text.

For *ranking*, not display: the research agent fetches candidate pages,
strips them to plain visible text, and embeds that to score relevance. We
deliberately avoid md_converter's markdown-preserving conversion (and its
markitdown/bs4 dependencies + per-page agent spawn) — for an embedding
relevance signal, bare visible text with script/style removed is both
sufficient and cheaper. The winning pages are re-opened through the proper
md_converter path afterward.
"""

from __future__ import annotations

import html
import re

# Drop the *contents* of these elements wholesale (not just their tags) —
# scripts/styles/etc. are never visible text and would pollute embeddings.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
# Block-level tags become paragraph/line breaks so words don't run together.
_BLOCK_BREAKS = re.compile(
    r"</?(p|div|section|article|br|hr|li|ul|ol|tr|h[1-6]|header|footer|"
    r"nav|main|aside|blockquote|pre|table)\b[^>]*>",
    re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_WS_RUNS = re.compile(r"[ \t\f\v]+")
_BLANK_RUNS = re.compile(r"\n\s*\n\s*")


def html_to_text(raw: str) -> str:
    """Reduce an HTML document to plain visible text — scripts/styles/comments
    removed, block tags turned into line breaks, remaining tags stripped,
    entities unescaped, and whitespace collapsed."""
    if not raw:
        return ""
    text = _DROP_BLOCKS.sub(" ", raw)
    text = _COMMENTS.sub(" ", text)
    text = _BLOCK_BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RUNS.sub(" ", text)
    text = _BLANK_RUNS.sub("\n\n", text)
    return text.strip()
