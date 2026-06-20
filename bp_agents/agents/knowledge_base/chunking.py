"""knowledge_base.chunking — Markdown chunking.

The implementation moved to `bp_agents.common.chunking` so the research
agent (pyarrow-free runtime) can reuse it without importing this LanceDB-
backed package. Re-exported here for the knowledge_base call sites and tests.
"""

from __future__ import annotations

from bp_agents.common.chunking import _split_recursive, chunk_markdown

__all__ = ["_split_recursive", "chunk_markdown"]
