"""bp_router.attachments — task-scope derivation for the file store.

Agents reference files by NAME in the router-managed store; the
router never makes outbound fetches on their behalf (no SSRF
surface). What lives here is the shared authz helper that both the
file-store frame handlers and the LLM name-`file_ref` resolver
depend on.
"""

from __future__ import annotations

from typing import Any


class AttachmentResolutionError(Exception):
    """Caller-agnostic file-resolution refusal. `code` and `message`
    are safe to surface to the requesting agent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def derive_task_file_scope(
    conn: Any, task_id: str, agent_id: str
) -> tuple[str, str] | None:
    """Return the authoritative `(user_id, session_id)` for `task_id`
    IFF `agent_id` is the task's active executor.

    The named file store's authority is the `(user_id, scope,
    filename)` tuple — there is no per-file signed key — so every
    named-store operation (the `FileStore`/`FileFetch`/`FileManage`
    handlers AND name `file_ref` resolution in an `LlmRequest`) must
    derive identity from the task row and verify active-executor,
    never trust an agent-asserted `user_id`/`session_id`. Mirrors the
    `_handle_file_upload_request` / `complete_task` authz. Returns
    None on unknown-task / not-active so the caller refuses opaquely
    (non-enumerable)."""
    row = await conn.fetchrow(
        "SELECT user_id, session_id, active_agent_id FROM tasks "
        "WHERE task_id = $1",
        task_id,
    )
    if row is None or row["active_agent_id"] != agent_id:
        return None
    return row["user_id"], row["session_id"]
