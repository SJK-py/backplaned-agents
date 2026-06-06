"""bp_agents.common.payloads — shared inter-agent payload models.

`message` / `cron_message` modes take a bare `{prompt}` — the agent
builds its own system prompt from config + history ([agents.md]). The
channel dispatches it; the orchestrator receives it.
"""

from __future__ import annotations

from pydantic import BaseModel


class MessagePayload(BaseModel):
    """Bare user input for the orchestrator's `message` mode (and the
    delegated `delegated_message` mode). The agent reconstructs context
    from session history; this carries only the turn's text."""

    prompt: str


class MemAdd(BaseModel):
    """memory `add` payload — channel fire-and-forget after a turn. Lives
    here (not in the memory agent) so the channel can build it without
    importing LanceDB."""

    user_prompt: str
    assistant_response: str


class MemRetrieve(BaseModel):
    """memory `retrieve` payload."""

    query: str
    count: int = 3
    child_count: int = 2


# -- webapp Memory page (tool:false management modes) -----------------------

# A single request returns at most this many items (the page paginates).
MAX_PAGE = 50


class MemList(BaseModel):
    """memory `list` — browse facts for the Memory page. No query → newest
    first (by `last_used_at`); with query → ranked by the retrieval formula."""

    query: str | None = None
    kind: str | None = None
    start: int = 0
    end: int = MAX_PAGE


class MemDelete(BaseModel):
    """memory `delete` — remove one fact by uid."""

    uid: str


class MemManualAdd(BaseModel):
    """memory `manual_add` — store a user-authored fact, bypassing phase-1
    extraction (kind overrides the reconcile default)."""

    fact: str
    kind: str = "personal_info"


class PurgeUserData(BaseModel):
    """memory `purge_user_data` — erase a user's entire per-user LanceDB
    (memory + KB share the dir). `user_id` is the TARGET to erase, carried in
    the payload because the task runs as the calling service principal, not as
    the (already-deleted) target user."""

    user_id: str


# -- webapp Knowledge base page (tool:false management modes) ---------------


class KbBrowse(BaseModel):
    """knowledge_base `browse` — list documents for the KB page, newest
    first, with optional title-substring / collection / tag filters."""

    query: str | None = None
    collection: str | None = None
    tag: str | None = None
    start: int = 0
    end: int = MAX_PAGE


class KbDelete(BaseModel):
    """knowledge_base `delete` — remove a document by title (+ collection to
    disambiguate duplicate titles across collections)."""

    title: str
    collection: str | None = None

