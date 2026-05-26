"""research (group l1) — web + RAG + document research; owns the KB.

Standard l1 modes via l1_common, plus local web tools (web_search,
html_fetch, web_download) and the knowledge_base / md_converter peer
tools (via the catalog). web_search targets a configurable
Brave-API-compatible endpoint (SearXNG); html_fetch routes non-raw URLs
through md_converter.webpage. See [agents.md].
"""

from bp_agents.agents.research.agent import RESEARCH_AGENT_ID, agent

__all__ = ["RESEARCH_AGENT_ID", "agent"]
