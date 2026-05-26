"""knowledge_base.chunking — Markdown chunking.

A pragmatic version of the fallback chain ([data-model.md] §2.1): split on
paragraph boundaries and accumulate into chunks within `[min_len,
max_len]`, carrying a small character overlap between consecutive chunks.
A single paragraph longer than `max_len` is hard-split. (The full
header→…→char chain is a later refinement.)
"""

from __future__ import annotations


def chunk_markdown(
    text: str, *, max_len: int = 2000, min_len: int = 1000, overlap: int = 100
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}".strip() if buf else para
        if len(candidate) <= max_len:
            buf = candidate
            continue
        # `buf` is full enough — flush it (with overlap carried forward).
        if buf:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = f"{tail}\n\n{para}".strip() if tail else para
        else:
            buf = para
        # A single oversized paragraph: hard-split on length.
        while len(buf) > max_len:
            chunks.append(buf[:max_len])
            carry = buf[max_len - overlap : max_len] if overlap else ""
            buf = carry + buf[max_len:]
    if buf.strip():
        # Fold a too-small trailing remainder into the previous chunk.
        if chunks and len(buf) < min_len:
            chunks[-1] = f"{chunks[-1]}\n\n{buf}".strip()
        else:
            chunks.append(buf)
    return chunks
