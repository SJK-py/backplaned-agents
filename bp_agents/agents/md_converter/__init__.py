"""md_converter (group l4) — file / webpage → Markdown (MarkItDown).

`convert` (tool-visible) turns a stash file into Markdown; `webpage`
(tool=false — restricts URL fetching to handcrafted callers like
research's html_fetch) fetches a URL and converts it. See [agents.md].
"""

from bp_agents.agents.md_converter.agent import (
    MD_CONVERTER_AGENT_ID,
    Convert,
    Webpage,
    agent,
    run_convert,
    run_webpage,
)

__all__ = [
    "MD_CONVERTER_AGENT_ID",
    "Convert",
    "Webpage",
    "agent",
    "run_convert",
    "run_webpage",
]
