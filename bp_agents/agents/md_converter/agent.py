"""md_converter agent — file / webpage → Markdown via MarkItDown.

`convert` reads a stash file and returns Markdown (inline content for
small results, or a stored `.md` stash name for large ones — `auto`
decides on a threshold). `webpage` fetches a URL and converts the HTML.
MarkItDown is synchronous, so conversions run in `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel

from bp_agents.common import text_output
from bp_protocol.types import AgentInfo, AgentOutput
from bp_sdk import Agent, TaskContext

logger = logging.getLogger(__name__)

MD_CONVERTER_AGENT_ID = "md_converter"

_AUTO_CONTENT_LIMIT = 2000  # auto → content if ≤ this many chars, else file
_CONTENT_HARD_CAP = 100_000  # content mode force-truncates here
_WEBPAGE_FETCH_CAP = 5 * 1024 * 1024  # 5 MiB


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
        description="Convert files and webpages to Markdown.",
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
    data = await (fetch(payload.url) if fetch else _default_fetch(payload.url))
    md = await asyncio.to_thread(_markitdown_bytes, data, ".html")
    truncate = min(max(payload.truncate, 0), _CONTENT_HARD_CAP)
    return text_output(md[:truncate])


async def _default_fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > _WEBPAGE_FETCH_CAP:
                    raise ValueError("webpage exceeds fetch cap")
            return bytes(buf)


@agent.handler(mode="convert")
async def convert_mode(ctx: TaskContext, payload: Convert) -> AgentOutput:
    return await run_convert(ctx, payload)


@agent.handler(mode="webpage", tool=False)
async def webpage_mode(ctx: TaskContext, payload: Webpage) -> AgentOutput:
    return await run_webpage(ctx, payload)


if __name__ == "__main__":
    agent.run()
