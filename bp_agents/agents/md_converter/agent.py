"""md_converter agent — file / webpage → Markdown via MarkItDown.

`convert` reads a stash file and returns Markdown (inline content for
small results, or a stored `.md` stash name for large ones — `auto`
decides on a threshold). `webpage` fetches a URL and converts the HTML.
MarkItDown is synchronous, so conversions run in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel

from bp_agents.common import text_output
from bp_agents.common.urlsafe import ensure_fetchable_url
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

logger = logging.getLogger(__name__)

MD_CONVERTER_AGENT_ID = "md_converter"

_AUTO_CONTENT_LIMIT = 2000  # auto → content if ≤ this many chars, else file
_CONTENT_HARD_CAP = 100_000  # content mode force-truncates here
_WEBPAGE_FETCH_CAP = 5 * 1024 * 1024  # 5 MiB

# A browser-like UA + Accept: many sites 403 the default `python-httpx` agent.
# Headers don't change which host is fetched, so this stays SSRF-safe (we keep
# redirects OFF — following them would skip the `ensure_fetchable_url` guard).
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_reason(exc: Exception) -> str:
    """A short, human-readable reason for a failed webpage fetch."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "the request timed out"
    if isinstance(exc, httpx.HTTPError):
        return "a network error"
    return str(exc) or type(exc).__name__


class Convert(BaseModel):
    name: str
    output_type: str = "auto"  # file | content | auto


class Webpage(BaseModel):
    url: str
    output_type: str = "content"
    truncate: int = 2000


agent = Agent(
    info=AgentInfo(
        agent_id=MD_CONVERTER_AGENT_ID,
        description="Converts files and webpages to Markdown.",
        groups=["l4"],
        capabilities=["document.convert", "web.convert", "file.full"],
    ),
)


def _markitdown_file(path: str) -> str:
    from markitdown import MarkItDown  # noqa: PLC0415

    return MarkItDown().convert(path).text_content


def _markitdown_bytes(data: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        return _markitdown_file(tmp.name)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


def _normalize(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f﻿]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(raw_html: str) -> str:
    """Regex HTML→text fallback when MarkItDown can't parse a page —
    preserves headers/lists/paragraph breaks so a bad page still yields
    something usable instead of failing the fetch."""
    text = re.sub(
        r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
        lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n", raw_html, flags=re.I,
    )
    text = re.sub(r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I)
    text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
    return _normalize(_strip_tags(text))


async def run_convert(ctx: TaskContext, payload: Convert) -> AgentOutput:
    path = await ctx.files.read(payload.name)
    md = await asyncio.to_thread(_markitdown_file, str(path))

    output_type = payload.output_type
    if output_type == "auto":
        output_type = "content" if len(md) <= _AUTO_CONTENT_LIMIT else "file"

    if output_type == "file":
        stem = PurePosixPath(payload.name).stem
        saved = await ctx.files.write(f"{stem}.md", md)
        return AgentOutput(content=f"Converted '{payload.name}' → {saved}", files=[saved])
    return text_output(md[:_CONTENT_HARD_CAP])


async def run_webpage(
    ctx: TaskContext,
    payload: Webpage,
    *,
    fetch: Callable[[str], Awaitable[bytes]] | None = None,
) -> AgentOutput:
    try:
        data = await (fetch(payload.url) if fetch else _default_fetch(payload.url))
    except Exception as exc:  # noqa: BLE001 — a fetch failure must not crash the handler
        logger.warning(
            "webpage_fetch_failed",
            extra={
                "event": "webpage_fetch_failed",
                "url": payload.url,
                "error": type(exc).__name__,
            },
        )
        return text_output(f"[Couldn't fetch {payload.url}: {_fetch_reason(exc)}.]")
    try:
        md = await asyncio.to_thread(_markitdown_bytes, data, ".html")
    except Exception:  # noqa: BLE001 — one bad page must not break the fetch
        logger.warning(
            "markitdown_webpage_failed_fallback_regex",
            extra={"event": "markitdown_webpage_failed", "url": payload.url},
        )
        md = _html_to_text(data.decode("utf-8", errors="replace"))
    truncate = min(max(payload.truncate, 0), _CONTENT_HARD_CAP)
    return text_output(md[:truncate])


async def _default_fetch(url: str) -> bytes:
    await ensure_fetchable_url(url)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), headers=_FETCH_HEADERS
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > _WEBPAGE_FETCH_CAP:
                    raise ValueError("webpage exceeds fetch cap")
            return bytes(buf)


@agent.handler(
    mode="convert",
    description="Convert an uploaded file (PDF, DOCX, spreadsheet, image, "
    "…) to Markdown text.",
)
async def convert_mode(ctx: TaskContext, payload: Convert) -> AgentOutput:
    return await run_convert(ctx, payload)


@agent.handler(
    mode="webpage", tool=False,
    description="Fetch a URL and convert the page to Markdown (internal; "
    "used by research's web pipeline).",
)
async def webpage_mode(ctx: TaskContext, payload: Webpage) -> AgentOutput:
    return await run_webpage(ctx, payload)


if __name__ == "__main__":
    agent.run()
