"""md_converter agent — file / webpage → Markdown via MarkItDown.

`convert` reads a stash file and returns Markdown (inline content for
small results, or a stored `.md` stash name for large ones — `auto`
decides on a threshold). `webpage` fetches a URL and converts the HTML.
MarkItDown is synchronous, so conversions run in `asyncio.to_thread`.

When `SUITE_MD_OCR_*` is configured, conversions additionally run the
`markitdown-ocr` plugin: a vision LLM OCRs images embedded in
PDF/DOCX/PPTX/XLSX files (and full pages of scanned PDFs). The plugin
needs a synchronous OpenAI-compatible client, which can't ride the
router's frame channel, so OCR uses its own dedicated provider
credentials (see `bp_agents.settings`). Unconfigured → unchanged.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel

from bp_agents.common import text_output
from bp_agents.common.urlsafe import safe_stream_get
from bp_agents.settings import load_suite_settings
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext
from bp_sdk.errors import UpstreamError

try:
    import resource  # POSIX-only; the suite runs on Linux containers.
except ImportError:  # pragma: no cover — non-POSIX dev box
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_settings = load_suite_settings()

MD_CONVERTER_AGENT_ID = "md_converter"

_AUTO_CONTENT_LIMIT = 2000  # auto → content if ≤ this many chars, else file
_CONTENT_HARD_CAP = 100_000  # content mode force-truncates here
_WEBPAGE_FETCH_CAP = 5 * 1024 * 1024  # 5 MiB
# Interactive-path fetch timeouts (seconds): connect fails fast on a dead host;
# the overall read is bounded so a slow/forbidden page can't stall a turn.
_FETCH_CONNECT_TIMEOUT_S = 5.0
_FETCH_TIMEOUT_S = 15.0

# Configurable UA (honest default) + Accept; the default `python-httpx` agent
# gets 403'd by many sites. `safe_stream_get` follows up to
# `web_fetch_max_redirects` hops with a per-hop SSRF re-check.
_FETCH_HEADERS = {
    "User-Agent": _settings.web_fetch_user_agent,
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


# 1-slot memo for the lazily-built OCR client. A mutable container mutated
# in place (not rebound), so the builder needs no `global`. Empty until the
# first OCR conversion builds the client.
_ocr_client_box: list = []


def _ocr_llm_client():  # -> openai.OpenAI | None (lazy import)
    """The OpenAI-compatible client MarkItDown's OCR plugin uses for image
    OCR, or None when OCR is not configured. Built from the dedicated
    `SUITE_MD_OCR_*` credentials (the router owns the suite's normal LLM
    routing, but a synchronous third-party-library client can't ride the
    frame channel, so OCR gets its own provider config). Cached so the
    client is built once, not per conversion. OCR engages only when BOTH a
    key and a model are set — otherwise the `openai` import never runs."""
    if _settings.md_ocr_api_key is None or not _settings.md_ocr_model:
        return None
    if _ocr_client_box:
        return _ocr_client_box[0]
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(
        api_key=_settings.md_ocr_api_key.get_secret_value(),
        # None → the OpenAI SDK's default endpoint; set for Azure/vLLM/etc.
        base_url=_settings.md_ocr_base_url or None,
    )
    _ocr_client_box.append(client)
    return client


def _make_markitdown():  # -> markitdown.MarkItDown (lazy import)
    """A MarkItDown instance, OCR-enabled when `SUITE_MD_OCR_*` is configured
    and plain otherwise. With OCR on, the `markitdown-ocr` plugin registers
    vision-OCR converters (PDF/DOCX/PPTX/XLSX + scanned-PDF fallback) ahead of
    the built-ins; with it off, behaviour is unchanged from a bare
    `MarkItDown()`."""
    from markitdown import MarkItDown  # noqa: PLC0415

    client = _ocr_llm_client()
    if client is None:
        return MarkItDown()
    kwargs = {
        "enable_plugins": True,
        "llm_client": client,
        "llm_model": _settings.md_ocr_model,
    }
    if _settings.md_ocr_prompt:  # else the plugin's default prompt
        kwargs["llm_prompt"] = _settings.md_ocr_prompt
    return MarkItDown(**kwargs)


def _markitdown_file(path: str) -> str:
    return _make_markitdown().convert(path).text_content


def _markitdown_bytes(data: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        return _markitdown_file(tmp.name)


def _worker_argv(in_path: str, out_path: str) -> list[str]:
    """argv for the isolated conversion worker. A seam so tests can swap in a
    stand-in command that simulates timeout / signal-death / non-zero exit."""
    return [
        sys.executable, "-m", "bp_agents.agents.md_converter._worker",
        in_path, out_path,
    ]


def _rlimit_preexec(mem_bytes: int):
    """A child preexec that caps RLIMIT_AS to `mem_bytes` (child-only; the
    parent's limits are untouched). Returns None when no cap applies."""
    if not mem_bytes or resource is None:
        return None

    def _apply() -> None:  # pragma: no cover — runs in the forked child
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    return _apply


async def _convert_isolated(in_path: str) -> str:
    """Convert a file to Markdown in a bounded CHILD process.

    This is the fix for silent conversion deaths: a segfault / OOM-kill on a
    pathological document kills only the worker, and a hang is bounded by a
    timeout — both surface as a clean `UpstreamError` (→ failed result + log)
    instead of taking the shared agent down with no trace. On success the
    worker's Markdown is read back from a temp file."""
    out_fd, out_path = tempfile.mkstemp(suffix=".md")
    os.close(out_fd)
    preexec = _rlimit_preexec(_settings.md_convert_mem_limit_mb * 1024 * 1024)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_worker_argv(in_path, out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_settings.md_convert_timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "md_convert_timeout",
                extra={
                    "event": "md_convert_timeout",
                    "timeout_s": _settings.md_convert_timeout_s,
                },
            )
            raise UpstreamError(
                f"conversion timed out after {_settings.md_convert_timeout_s:g}s "
                "(the file is too large or complex to convert)"
            ) from None
        if proc.returncode != 0:
            reason = (stderr or b"").decode("utf-8", "replace").strip()
            logger.warning(
                "md_convert_worker_failed",
                extra={
                    "event": "md_convert_worker_failed",
                    "returncode": proc.returncode,
                    # Bounded; the worker prints a single safe line. The full
                    # detail is here for ops, never on the wire.
                    "stderr": reason[:500],
                },
            )
            # A negative return code is death by signal (e.g. -9 SIGKILL = the
            # cgroup/kernel OOM-killer or rlimit). Surface a bounded, path-free
            # message either way — the log carries the specifics.
            if proc.returncode < 0:
                raise UpstreamError(
                    "the conversion ran out of memory or was killed "
                    "(the file is too large or complex to convert)"
                )
            raise UpstreamError("could not convert this file")
        with open(out_path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


def _normalize(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f﻿]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode_html(data: bytes) -> str:
    """Decode fetched HTML bytes, DETECTING the charset (BOM / `<meta charset>`
    / statistical) like a browser — Korean sites are frequently EUC-KR / CP949,
    not UTF-8, so a naive `decode("utf-8")` turns their text into `�`. Uses
    bs4's UnicodeDammit (present via markitdown); UTF-8 + replace is the last
    resort. (The primary markitdown path already does this; this is only for
    the regex fallback below.)"""
    try:
        from bs4 import UnicodeDammit  # noqa: PLC0415

        decoded = UnicodeDammit(data).unicode_markup
        if decoded is not None:
            return decoded
    except Exception:  # noqa: BLE001 — detection must never break the fallback
        logger.warning(
            "html_charset_detect_failed",
            extra={"event": "html_charset_detect_failed"},
        )
    return data.decode("utf-8", errors="replace")


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
    # Convert in an isolated, bounded child process: a crash / OOM / hang on a
    # pathological document fails this one task cleanly instead of silently
    # killing the shared agent (see `_convert_isolated`).
    md = await _convert_isolated(str(path))

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
        md = _html_to_text(_decode_html(data))
    truncate = min(max(payload.truncate, 0), _CONTENT_HARD_CAP)
    return text_output(md[:truncate])


async def _default_fetch(url: str) -> bytes:
    # Fail fast: this is the interactive research path, so an unreachable or
    # slow page shouldn't stall the turn. Short connect, bounded read — much
    # tighter than the bulk `web_fetch_timeout_s` download path.
    timeout = httpx.Timeout(_FETCH_TIMEOUT_S, connect=_FETCH_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout, headers=_FETCH_HEADERS) as client:
        return await safe_stream_get(
            client, url, cap=_WEBPAGE_FETCH_CAP,
            max_redirects=_settings.web_fetch_max_redirects,
        )


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
