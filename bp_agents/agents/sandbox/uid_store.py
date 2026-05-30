"""bp_agents.agents.sandbox.uid_store — local per-user uid allocation.

The sandbox drops each user's `bash` subprocess to a distinct OS uid so users
are isolated from each other inside the shared container (filesystem perms,
process visibility, signals). The mapping `user_id → uid` is owned HERE, in a
JSON file on the agent's `AGENT_STATE_DIR` (`/state`) volume — NOT in the
suite DB. Two reasons:

  1. The sandbox runs untrusted code and is deliberately isolated from
     Postgres (prod compose puts it on the `agents` network only — no db).
     A DB-backed uid would re-couple it to the database it must not reach.
  2. uid assignment is the sandbox's own concern; nothing else needs it.

Allocation is **sequential from a base**: the first user_id seen gets
`base + 0`, the next `base + 1`, … persisted so a returning user keeps its
uid. Sequential (not hash-of-user_id) because a uid MUST be unique — a hash
collision would map two users to the same uid and silently destroy the
isolation this exists to provide; sequential is collision-free by
construction. The file is the source of truth: if `/state` is wiped, uids are
reassigned, but workspaces are per-user dirs that get re-chowned on next use,
so that's tolerable.

Concurrency: a same-host advisory file lock (`fcntl.flock`) serialises the
read-modify-write so two tasks racing on a brand-new user can't both claim the
next uid. Single-process today, but cheap insurance.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_NAME = "sandbox_uids.json"


class UidStore:
    """File-backed `user_id → uid` allocator. Construct once at startup with
    the agent's state dir and the uid base/cap, then call `uid_for(user_id)`
    per task."""

    def __init__(self, *, state_dir: Path, base: int, maximum: int) -> None:
        self._path = Path(state_dir) / _STORE_NAME
        self._base = base
        self._max = maximum

    def _load(self) -> dict[str, int]:
        try:
            data: Any = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            # Corrupt/unreadable store: start fresh rather than crash. The
            # workspaces are re-chowned on use, so reassignment is safe.
            logger.warning(
                "sandbox_uid_store_unreadable",
                extra={"event": "sandbox_uid_store_unreadable",
                       "path": str(self._path)},
            )
            return {}
        # Coerce to the expected shape; drop anything malformed.
        out: dict[str, int] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, int):
                    out[k] = v
        return out

    def _atomic_write(self, mapping: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(mapping, indent=2, sort_keys=True))
        os.replace(tmp, self._path)  # atomic on POSIX

    def uid_for(self, user_id: str) -> int | None:
        """Return the uid for `user_id`, allocating + persisting a new one on
        first sight. Returns None if the base range is exhausted (the caller
        then runs without a uid drop rather than reusing a colliding uid)."""
        import fcntl  # noqa: PLC0415 — POSIX-only; the sandbox runs on Linux

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Lock on a sidecar so the lock survives the atomic replace of the
        # store file itself.
        lock_path = self._path.with_suffix(".lock")
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                mapping = self._load()
                existing = mapping.get(user_id)
                if existing is not None:
                    return existing
                # Allocate the next free uid: base + count of current entries,
                # skipping any already-taken value (defensive against a
                # hand-edited file with gaps).
                taken = set(mapping.values())
                candidate = self._base
                while candidate in taken:
                    candidate += 1
                if candidate > self._max:
                    logger.error(
                        "sandbox_uid_range_exhausted",
                        extra={"event": "sandbox_uid_range_exhausted",
                               "base": self._base, "max": self._max,
                               "allocated": len(mapping)},
                    )
                    return None
                mapping[user_id] = candidate
                self._atomic_write(mapping)
                logger.info(
                    "sandbox_uid_allocated",
                    extra={"event": "sandbox_uid_allocated",
                           "bp.user_id": user_id, "uid": candidate},
                )
                return candidate
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
