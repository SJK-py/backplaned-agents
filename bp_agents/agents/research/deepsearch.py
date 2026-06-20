"""research.deepsearch — SearXNG content ranking.

SearXNG relays whatever snippet the upstream engine gave, which is often a
thin meta-description — a weak signal for the model to decide which page to
open. This module trades a little latency for a much stronger signal: fetch
the top results, reduce each to visible text, chunk + embed it, and rank
pages by how well their *content* (not their snippet) matches the query.

Pipeline (all stdlib + the embedding preset — no LanceDB, nothing persisted):

  1. raw-fetch `count * multiplier` result URLs in parallel (SSRF-guarded);
  2. html → visible text → `chunk_markdown`; un-fetchable pages fall back to
     their snippet as a single, flagged chunk;
  3. embed every chunk once (batched) and cosine-score it against the query;
  4. page score = sum of its top-N chunk scores squared (rewards concentrated
     relevance, bounds long-page bias);
  5. return the top `count` pages, each shown via its best-matching chunk.

Embeddings/retrieval are far cheaper than an LLM call, so this stays in the
"cheap" lane — the expensive `extract_query` distillation is still reserved
for the handful of winners the model actually opens.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from bp_agents.common.chunking import chunk_markdown
from bp_agents.common.htmltext import html_to_text
from bp_agents.common.urlsafe import safe_stream_get

if TYPE_CHECKING:
    from bp_agents.settings import SuiteSettings
    from bp_sdk import TaskContext

logger = logging.getLogger(__name__)

BytesFetcher = Callable[[str], Awaitable[bytes]]

_CONNECT_TIMEOUT_S = 5.0
_EMBED_BATCH = 96  # Keep each embedding request within provider batch limits.
_DEEP_SNIPPET_CAP = 600  # Per-result best-chunk length shown to the model.
_UNVERIFIED = " [unverified — page could not be fetched; snippet only]"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested in isolation)
# --------------------------------------------------------------------------- #


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _page_score(scores: list[float], *, top_n: int) -> float:
    """Sum of the top-N chunk scores squared. Squaring rewards a page with one
    strongly-matching chunk over a page with many weakly-matching ones; the
    top-N cap stops a long page from accumulating score on volume alone."""
    top = sorted(scores, reverse=True)[:top_n]
    return sum(s * s for s in top)


def snippets_too_thin(rows: list[dict[str, Any]], settings: SuiteSettings) -> bool:
    """`auto`-mode trigger: are the result snippets too thin to choose on? A
    deterministic, model-independent measure of the exact failure mode deep
    search fixes."""
    if not rows:
        return False
    thin = sum(
        1 for r in rows
        if len((r.get("content") or "").strip()) < settings.web_deep_min_snippet_chars
    )
    return thin / len(rows) > settings.web_deep_thin_fraction


# --------------------------------------------------------------------------- #
# Fetch + rank
# --------------------------------------------------------------------------- #


async def _default_fetch_bytes(url: str, *, settings: SuiteSettings) -> bytes:
    t = httpx.Timeout(
        settings.web_fetch_timeout_s,
        connect=min(_CONNECT_TIMEOUT_S, settings.web_fetch_timeout_s),
    )
    headers = {"User-Agent": settings.web_fetch_user_agent}
    async with httpx.AsyncClient(timeout=t, headers=headers) as client:
        return await safe_stream_get(
            client, url, cap=settings.web_fetch_max_bytes,
            max_redirects=settings.web_fetch_max_redirects,
        )


async def _fetch_text(
    url: str, *, settings: SuiteSettings, get_bytes: BytesFetcher | None,
) -> str | None:
    """Visible text for `url`, capped — or `None` if it couldn't be fetched."""
    try:
        data = (
            await get_bytes(url) if get_bytes
            else await _default_fetch_bytes(url, settings=settings)
        )
    except Exception as exc:  # noqa: BLE001 — any fetch failure → snippet fallback
        logger.info(
            "deep_search_fetch_failed",
            extra={"event": "deep_search_fetch_failed", "url": url, "error": str(exc)},
        )
        return None
    text = html_to_text(data.decode("utf-8", errors="replace"))
    return text[: settings.web_extract_fetch_chars]


async def deep_searxng_search(
    ctx: TaskContext,
    query: str,
    *,
    rows: list[dict[str, Any]],
    count: int,
    settings: SuiteSettings,
    embedding_preset: str | None,
    get_bytes: BytesFetcher | None = None,
) -> str:
    """Rank pre-fetched SearXNG `rows` by fetched-content relevance and render
    the top `count`. `rows` is passed in (already fetched by the caller) so the
    SearXNG query isn't repeated."""
    if not rows:
        return f"No results for {query!r}."

    # 1. Fetch every candidate's visible text in parallel.
    texts = await asyncio.gather(
        *(_fetch_text(r.get("url", ""), settings=settings, get_bytes=get_bytes)
          for r in rows)
    )

    # 2. Build chunks with page provenance. Un-fetchable pages contribute their
    #    snippet as a single flagged chunk so they still rank (and the model is
    #    told the evidence is weaker).
    pages: list[dict[str, Any]] = []
    chunk_texts: list[str] = []
    owner: list[int] = []  # chunk index → page index
    for r, text in zip(rows, texts, strict=True):
        page = {
            "url": r.get("url", ""), "title": r.get("title", ""),
            "unfetchable": False, "chunks": [],
        }
        page_chunks = chunk_markdown(
            text or "", max_len=settings.web_deep_chunk_chars
        ) if text else []
        if not page_chunks:
            page["unfetchable"] = True
            snippet = (r.get("content") or "").strip()
            page_chunks = [snippet] if snippet else []
        pidx = len(pages)
        for c in page_chunks:
            owner.append(pidx)
            chunk_texts.append(c)
            page["chunks"].append(len(chunk_texts) - 1)
        pages.append(page)

    if not chunk_texts:
        return f"No usable content for {query!r}."

    # 3. Embed the query + every chunk (batched), then cosine-score each chunk.
    qv = (await ctx.llm.embed([query], preset=embedding_preset))[0]
    vectors: list[list[float]] = []
    for i in range(0, len(chunk_texts), _EMBED_BATCH):
        vectors.extend(
            await ctx.llm.embed(chunk_texts[i : i + _EMBED_BATCH], preset=embedding_preset)
        )
    chunk_scores = [max(0.0, _cosine(qv, v)) for v in vectors]

    # 4. Aggregate to a page score and pick each page's best chunk.
    for page in pages:
        idxs = page["chunks"]
        page["score"] = _page_score([chunk_scores[i] for i in idxs], top_n=settings.web_deep_top_chunks)
        best = max(idxs, key=lambda i: chunk_scores[i])
        page["best"] = chunk_texts[best].strip()[:_DEEP_SNIPPET_CAP]

    # 5. Rank and render the top `count`.
    ranked = sorted(pages, key=lambda p: p["score"], reverse=True)[:count]
    blocks = []
    for i, p in enumerate(ranked):
        flag = _UNVERIFIED if p["unfetchable"] else ""
        blocks.append(f"{i + 1}. {p['title']}{flag}\n{p['url']}\n{p['best']}")
    header = (
        "Content-ranked results (top pages were fetched and scored against your "
        "query, not ranked on snippets):"
    )
    return f"{header}\n\n" + "\n\n".join(blocks)
