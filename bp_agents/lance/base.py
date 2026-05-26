"""bp_agents.lance.base — per-user LanceDB connection."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def user_db_path(root: str | Path, user_id: str) -> Path:
    """Filesystem path for a user's LanceDB. `user_id` is sanitized to a
    safe directory segment (the alphabet is already constrained, but be
    defensive about path traversal)."""
    return Path(root) / _SAFE.sub("_", user_id)


async def connect(root: str | Path, user_id: str) -> Any:
    """Open (creating if needed) the user's LanceDB connection."""
    import lancedb  # noqa: PLC0415

    path = user_db_path(root, user_id)
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    return await asyncio.to_thread(lancedb.connect, str(path))
