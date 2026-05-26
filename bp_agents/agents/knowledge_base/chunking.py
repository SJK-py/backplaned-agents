"""knowledge_base.chunking — Markdown chunking ([data-model.md] §2.1).

Recursive fallback chain: split on the most semantic boundary present —
Markdown header → blank line (paragraph) → newline → sentence → word →
character — recursing into any span still longer than `max_len`. The
resulting atomic spans are then accumulated into chunks within
`[min_len, max_len]`, carrying a small character overlap between
consecutive chunks so a match near a boundary keeps its context.
"""

from __future__ import annotations

# Most- to least-semantic boundaries. `"\n#"` catches every ATX header
# level (`#`, `##`, …); `""` is the terminal hard char-split.
_SEPARATORS = ["\n#", "\n\n", "\n", ". ", " ", ""]


def _split_recursive(text: str, separators: list[str], max_len: int) -> list[str]:
    """Break `text` into spans each ≤ `max_len`, preferring earlier (more
    semantic) separators and recursing with the rest for oversized spans."""
    if len(text) <= max_len:
        return [text] if text else []
    for idx, sep in enumerate(separators):
        if sep == "":
            return [text[i : i + max_len] for i in range(0, len(text), max_len)]
        if sep not in text:
            continue
        spans: list[str] = []
        parts = text.split(sep)
        for j, part in enumerate(parts):
            # Re-attach the separator we split on (except before the first
            # part) so content is preserved and header lines stay intact.
            piece = part if j == 0 else sep + part
            if not piece:
                continue
            if len(piece) <= max_len:
                spans.append(piece)
            else:
                spans.extend(_split_recursive(piece, separators[idx + 1 :], max_len))
        return spans
    return [text]


def chunk_markdown(
    text: str, *, max_len: int = 2000, min_len: int = 1000, overlap: int = 100
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    spans = _split_recursive(text, _SEPARATORS, max_len)
    chunks: list[str] = []
    buf = ""
    for span in spans:
        candidate = buf + span if buf else span
        if len(candidate) <= max_len:
            buf = candidate
            continue
        # Adding `span` would overflow — flush `buf`, carry an overlap tail.
        if buf:
            chunks.append(buf.strip())
            tail = buf[-overlap:] if overlap else ""
            buf = tail + span
        else:
            buf = span
        # A single span wider than max_len (only from the char-split leaf):
        # hard-cut it down.
        while len(buf) > max_len:
            chunks.append(buf[:max_len].strip())
            buf = buf[max_len - overlap : max_len] + buf[max_len:] if overlap else buf[max_len:]
    if buf.strip():
        # Fold a too-small trailing remainder into the previous chunk.
        if chunks and len(buf) < min_len:
            chunks[-1] = f"{chunks[-1]}{buf}".strip()
        else:
            chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]
