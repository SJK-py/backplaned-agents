"""bp_protocol.frames — Discriminated-union frame models for the WebSocket
protocol between router and agents.

See `docs/router/protocol.md` §2 for the full specification.

Every frame is a Pydantic model; validation happens at the router edge
before any business logic runs. Callers should use `parse_frame()` to
decode JSON into the correct typed instance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from bp_protocol.types import (
    AgentInfo,
    AgentOutput,
    TaskPriority,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_correlation_id() -> str:
    """UUIDv4 used for correlation. UUIDv7 once stdlib supports it cleanly."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Per-field item-count caps
# ---------------------------------------------------------------------------
#
# `Settings.max_payload_bytes` (1 MiB default) caps each WS frame in
# bytes, but a 1 MiB payload of `[{}, {}, ...]` produces tens of
# thousands of nested-Python-object entries — much more memory and
# CPU on the receive side than the wire bytes suggest, and the same
# blob then gets re-encoded into Postgres `jsonb`, propagated through
# downstream provider HTTP clients, etc. These per-field item caps
# bound that fan-out: Pydantic v2 short-circuits at validation time,
# so an oversize list is rejected before any business logic runs.
#
# Defaults are sized for the protocol's stated intent, NOT the wire
# byte cap — generous headroom for legitimate workloads, tight
# enough to break a fan-out attack. Operators that hit a real
# legitimate cap can override per-field via env (future) but the
# current call sites are all well under these.

# LlmRequestFrame.messages: a single conversation turn list. Even
# very long sessions stay well under 1024 turns; multi-modal content
# blocks are nested inside each message dict, not separate entries.
_LLM_MAX_MESSAGES = 1024

# LlmRequestFrame.tools: function-tool definitions per call. Anthropic
# allows ~128, OpenAI ~128. Cap at 256 for headroom — anything above
# that is almost certainly an attack or a bug.
_LLM_MAX_TOOLS = 256

# LlmRequestFrame.text (embed mode): batch size. Provider limits vary
# (OpenAI: 2048; Cohere: 96; Voyage: 128). Cap to the highest
# upstream limit so we don't reject legitimate batches.
_LLM_MAX_EMBED_INPUTS = 2048

# LlmResultFrame.tool_calls: tool calls in a single response. Bounded
# by the model's output: typical responses < 10; runaway agents can
# emit dozens. Cap matches `_LLM_MAX_TOOLS` since you can't call more
# distinct tools than were defined.
_LLM_MAX_TOOL_CALLS = 256

# LlmResultFrame.reasoning_blocks: opaque provider-shaped reasoning.
# Anthropic may emit several `thinking` / `redacted_thinking` blocks
# per response; cap is generous.
_LLM_MAX_REASONING_BLOCKS = 64

# LlmResultFrame.vectors: embedding outputs, one per input. Mirrors
# `_LLM_MAX_EMBED_INPUTS` — the result list mirrors the request list.
_LLM_MAX_VECTORS = _LLM_MAX_EMBED_INPUTS


# ---------------------------------------------------------------------------
# Common header
# ---------------------------------------------------------------------------


class _FrameBase(BaseModel):
    """Fields present on every frame (`docs/router/protocol.md` §2.1)."""

    type: str
    protocol_version: str = "1"
    correlation_id: str = Field(default_factory=_new_correlation_id)
    trace_id: str
    span_id: str
    timestamp: datetime = Field(default_factory=_now)
    agent_id: str

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class HelloFrame(_FrameBase):
    """First frame on a new socket. Agent → router."""

    type: Literal["Hello"] = "Hello"
    auth_token: str
    sdk_version: str
    agent_info: AgentInfo
    resume_token: str | None = None


class WelcomeFrame(_FrameBase):
    """Router → agent, sent only after successful Hello."""

    type: Literal["Welcome"] = "Welcome"
    session_id: str
    available_destinations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    heartbeat_interval_ms: int = 20_000
    max_payload_bytes: int = 1_048_576


class CatalogUpdateFrame(_FrameBase):
    """Router → agent. Replaces the cached `available_destinations`.

    Pushed when the catalog could change for a connected agent: a new
    agent onboards, an agent is suspended/evicted, or admin mutates
    the ACL rule list. Carries the full catalog snapshot — receivers
    drop their previous cache and adopt the payload as-is.
    """

    type: Literal["CatalogUpdate"] = "CatalogUpdate"
    available_destinations: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AgentInfoUpdateFrame(_FrameBase):
    """Agent → router. Patch-update of this agent's published
    `AgentInfo`. The router merges only the non-None fields with
    the existing record, re-validates the merged shape, persists,
    and broadcasts `CatalogUpdate` to every connected agent that
    sees this one.

    PATCH semantics: `None` means "don't touch". The agent sends
    only the fields it wants to change. Pre-existing values for
    other fields are preserved.

    Mutable surface: `description`, `groups`, `capabilities`,
    `accepts_schema`, `produces_schema`, `produces_files`,
    `non_tool_modes`, `mode_descriptions`, `hidden`,
    `documentation_url`. `agent_id` is
    locked — it's the
    stable identity that refresh tokens / ACL `@<id>` rules /
    audit history depend on.

    Rate-limited per-agent at the router edge to bound
    `CatalogUpdate` broadcast frequency (see
    `Settings.agent_info_update_rate_limit_*`). Excessive frequency
    surfaces as `AckFrame(accepted=False, reason="rate_limited")`.

    Frame is acknowledged with an `AckFrame` keyed off
    `correlation_id`; payload-validation failures and
    rate-limit denials both come back as `accepted=False`
    with a typed `reason`.

    Wire-compat: this is a new frame type, so older routers
    that don't recognise it reject as `frame_invalid` (the
    standard unknown-frame path). Older agents never emit it.
    Operators upgrading the router can take advantage; operators
    upgrading agents need a router that's seen this frame
    type first.
    """

    type: Literal["AgentInfoUpdate"] = "AgentInfoUpdate"

    description: str | None = None
    groups: list[str] | None = None
    capabilities: list[str] | None = None
    accepts_schema: dict[str, Any] | None = None
    produces_schema: dict[str, Any] | None = None
    produces_files: bool | None = None
    non_tool_modes: list[str] | None = None
    mode_descriptions: dict[str, str] | None = None
    hidden: bool | None = None
    documentation_url: str | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> AgentInfoUpdateFrame:
        if all(
            getattr(self, f) is None
            for f in (
                "description",
                "groups",
                "capabilities",
                "accepts_schema",
                "produces_schema",
                "produces_files",
                "non_tool_modes",
                "mode_descriptions",
                "hidden",
                "documentation_url",
            )
        ):
            raise ValueError(
                "AgentInfoUpdateFrame must set at least one mutable field; "
                "an empty patch is a no-op and should not be sent"
            )
        return self


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


class NewTaskFrame(_FrameBase):
    """Spawn (`task_id is None`) or delegate (`task_id` set)."""

    type: Literal["NewTask"] = "NewTask"
    task_id: str | None = None
    parent_task_id: str | None = None
    destination_agent_id: str
    user_id: str
    user_level: str = ""
    """The session principal's level (admin | service | tierN). Set by
    the router on outbound delivery; ignored on inbound spawn frames
    from agents (the router looks it up from the user record)."""
    session_id: str
    priority: TaskPriority = TaskPriority.NORMAL
    deadline: datetime | None = None
    idempotency_key: str | None = None
    input_mode: str | None = None
    """Which of the destination's registered modes this payload
    targets (`AgentInfo.accepts_schema` is keyed by mode). `None` =
    the destination's sole mode (the common single-handler case) —
    the router/SDK resolve it; ambiguous (>1 mode) + `None` is
    rejected. Set explicitly for multi-mode destinations; the
    per-mode LLM tool (`call_<agent>_<mode>`) carries it
    automatically. Replaces the `is_control` discriminator —
    control is just a mode listed in `AgentInfo.non_tool_modes`."""
    payload: dict[str, Any] = Field(default_factory=dict)
    # Stamped by the router on outbound delivery when the inbound frame
    # was a delegation (an existing `task_id` reassigned to a new
    # destination). None on plain spawns. Receivers read it via
    # `ctx.delegating_agent_id` to branch on delegation if they care
    # — there is no separate delegation handler registry. Only the
    # most-recent delegator's id is carried — chain history is
    # reconstructable from the `task.delegated` audit events.
    delegating_agent_id: str | None = None


class ResultFrame(_FrameBase):
    """Terminal outcome of a task. Exactly one per task, ever."""

    type: Literal["Result"] = "Result"
    task_id: str
    parent_task_id: str | None = None
    status: TaskStatus
    status_code: int
    output: AgentOutput | None = None
    error: dict[str, Any] | None = None


class ProgressFrame(_FrameBase):
    """Interim event during long-running tasks."""

    type: Literal["Progress"] = "Progress"
    task_id: str
    event: str
    """thinking | tool_call | tool_result | chunk | status | <custom>"""
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelFrame(_FrameBase):
    """Request abort of an in-flight task or LLM call.

    Two modes:
      - task abort:  task_id set, ref_correlation_id None (the common
        path; cancels the task and propagates to descendants).
      - LLM abort:   ref_correlation_id set to an in-flight LlmRequest's
        correlation_id, task_id None. Cancels just that one provider
        call so an agent's own cancellation can free server-side
        tokens without tearing down the surrounding task.
    """

    type: Literal["Cancel"] = "Cancel"
    task_id: str | None = None
    ref_correlation_id: str | None = None
    reason: str = "user_aborted"


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


class ErrorFrame(_FrameBase):
    """Protocol-level failure. Not used for task-level failures (use Result)."""

    type: Literal["Error"] = "Error"
    code: str
    message: str
    ref_correlation_id: str | None = None
    retryable: bool = False


class AckFrame(_FrameBase):
    """Receipt acknowledgement."""

    type: Literal["Ack"] = "Ack"
    ref_correlation_id: str
    accepted: bool = True
    # R8 (HIGH): bounded length so an agent that rejects with a
    # crafted long string can't flood the caller's WS. Pre-R8 the
    # reason was unbounded, and `tasks.py:626` reflected the
    # destination agent's `reason` verbatim into the caller's Ack
    # — a cross-agent reflection / amplification vector. `Field(
    # max_length=256)` matches the R2 `safe_validator_message`
    # bound for consistency; serialised reasons longer than that
    # fail Pydantic validation at parse_frame time.
    reason: str | None = Field(default=None, max_length=256)
    task_id: str | None = None
    """Set on ack of NewTask: the assigned task_id."""


class PingFrame(_FrameBase):
    type: Literal["Ping"] = "Ping"


class PongFrame(_FrameBase):
    type: Literal["Pong"] = "Pong"
    ref_correlation_id: str


# ---------------------------------------------------------------------------
# LLM service (router-side LlmService over the same WS channel)
# ---------------------------------------------------------------------------


LlmCallKind = Literal["generate", "embed", "count_tokens"]


class LlmRequestFrame(_FrameBase):
    """Agent → router. Invoke the router-side `LlmService`.

    `kind` selects between generate / embed / count_tokens. Field set
    used by each:
      generate:     messages, tools, tool_choice, temperature,
                    max_tokens, stream, provider_options
      embed:        text
      count_tokens: messages

    `preset` is the new (preferred) way to pick a configuration —
    a name registered in the `llm_presets` table that bundles
    provider + concrete_model + sampling defaults + provider_options
    + min_user_level. The legacy `model` field maps onto the same
    name space (built-in default presets share names with the old
    aliases) so existing agents using `model="..."` keep working
    unchanged. When both are present, `preset` wins.
    """

    type: Literal["LlmRequest"] = "LlmRequest"
    kind: LlmCallKind = "generate"
    # Legacy: kept for back-compat; agents are encouraged to use
    # `preset` instead. Resolution: `preset` first, then `model`.
    model: str = "default"
    preset: str | None = None

    # generate
    messages: list[dict[str, Any]] = Field(
        default_factory=list, max_length=_LLM_MAX_MESSAGES
    )
    tools: list[dict[str, Any]] = Field(
        default_factory=list, max_length=_LLM_MAX_TOOLS
    )
    tool_choice: Any | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    provider_options: dict[str, Any] | None = None

    # embed
    text: list[str] | None = Field(default=None, max_length=_LLM_MAX_EMBED_INPUTS)

    # context (propagated for quotas + audit)
    user_id: str | None = None
    task_id: str | None = None


# ---------------------------------------------------------------------------
# Typed sub-models for LLM frames
# ---------------------------------------------------------------------------


class LlmResultError(BaseModel):
    """Typed error payload for `LlmResultFrame.error`.

    Replaces the previous `Optional[dict[str, str]]` shape so SDK
    clients can rely on the field set without dict-key archaeology.
    Backwards-compatible at the wire level: clients reading
    `error["code"]` and `error["message"]` keep working unchanged.

    `retriable` is auto-set from `code` via `RETRIABLE_LLM_CODES`
    when the caller doesn't provide it explicitly. Constructed errors
    can override (e.g. an `internal_error` bug the operator wants
    flagged not-retriable for telemetry purposes), but the default
    keeps the wire flag and the typed code from drifting.

    `retry_after_seconds` mirrors HTTP `Retry-After` from rate-limited
    upstreams. The router populates it via the per-provider
    classifiers; SDK retry policy honours it verbatim.

    `upstream_class` is provider exception class name — telemetry
    only, never used for routing logic on either side.
    """

    code: str
    message: str = ""
    retriable: bool | None = None
    retry_after_seconds: float | None = None
    upstream_class: str | None = None

    # `extra="ignore"` (NOT `forbid`) — this is a payload sub-model
    # nested inside `LlmResultFrame.error`, not a frame itself. Frame-
    # level fields are versioned via `protocol_version` and grow only
    # on a major bump (`_FrameBase` keeps `extra=forbid`), but error
    # payload growth is additive: the design promises (§9 of
    # `docs/design/llm-retriable-errors.md`) that older SDKs reading
    # `error["code"]` keep working when the router ships new fields.
    # `forbid` would break that promise — every old SDK would
    # `ValidationError` on a future field. `ignore` lets the router
    # add `provider_request_id`, `attempt_number`, etc. without a
    # protocol bump.
    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _derive_retriable(self) -> LlmResultError:
        # Auto-fill `retriable` from the code when the caller didn't
        # set it. `RETRIABLE_LLM_CODES` is the single source of truth.
        if self.retriable is None:
            object.__setattr__(
                self, "retriable", self.code in RETRIABLE_LLM_CODES
            )
        return self


class LlmDeltaMeta(BaseModel):
    """Status hint emitted alongside (instead of) content on a
    `LlmDeltaFrame`. Used by the streaming setup-retry loop to let
    UI clients show a "retrying" spinner during the backoff sleep
    between attempts.

    See `docs/design/llm-retriable-errors.md` §7.1 for the rationale.

    Mutual-exclusivity invariant: when this object is set on a
    `LlmDeltaFrame`, every content field on the parent frame
    (`text`, `tool_call`, `reasoning_block`, `finish_reason`,
    `usage`, etc.) MUST be None. The frame-level validator on
    `LlmDeltaFrame` enforces this.
    """

    kind: Literal["retry_pending"]
    # 1-indexed attempt number that just failed.
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    # Wait time before the next attempt. Sourced from `Retry-After`
    # for rate-limited upstreams; otherwise from the adapter's
    # backoff schedule.
    retry_after_seconds: float = Field(ge=0.0)
    # The classified code from the just-failed attempt. Lets the UI
    # show "rate limited; retrying in 5s" vs "upstream timeout;
    # retrying in 1s".
    reason_code: str

    # Same forward-compat policy as `LlmResultError` — payload sub-
    # model, not a frame; new fields (e.g. a future `progress_text`
    # for richer UI hints) must not break older agents.
    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _attempt_within_max(self) -> LlmDeltaMeta:
        if self.attempt > self.max_attempts:
            raise ValueError(
                f"LlmDeltaMeta.attempt ({self.attempt}) cannot exceed "
                f"max_attempts ({self.max_attempts})"
            )
        return self


# ---------------------------------------------------------------------------
# LLM streaming + result frames
# ---------------------------------------------------------------------------


class LlmDeltaFrame(_FrameBase):
    """Router → agent. One streaming chunk in a generate(stream=True) call.

    The terminal `LlmResultFrame` always follows the last delta and ends
    the iterator on the SDK side.

    A delta with `meta` set is a **status hint** (e.g. "retrying after
    a rate-limit"); when meta is populated, every content field on
    this frame MUST be None. See `LlmDeltaMeta` and the validator
    below.
    """

    type: Literal["LlmDelta"] = "LlmDelta"
    ref_correlation_id: str
    text: str | None = None
    tool_call: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    # True when this delta's text is a thought-summary chunk
    # (`include_thoughts=True` on the request).
    thought: bool = False
    # Round-tripped on assistant-role messages; required on the first
    # function call of any Gemini 3 multi-turn function-calling loop.
    # Base64 of the provider-supplied opaque bytes.
    thought_signature: str | None = None
    # A completed reasoning block (Anthropic `thinking` /
    # `redacted_thinking`) emitted at content-block-stop time during
    # streaming. The dispatch aggregator concatenates these into the
    # final `LlmResultFrame.reasoning_blocks` for round-trip support.
    reasoning_block: dict[str, Any] | None = None
    # Status-hint payload. RESERVED — emitted by the streaming
    # setup-retry path. Set this and all content fields above MUST
    # be None — the validator below enforces.
    meta: LlmDeltaMeta | None = None

    @model_validator(mode="after")
    def _meta_excludes_content(self) -> LlmDeltaFrame:
        if self.meta is None:
            return self
        # `thought` is a flag (default False) and stays as-is; every
        # other content field must be None when meta is set.
        bad: list[str] = []
        if self.text is not None:
            bad.append("text")
        if self.tool_call is not None:
            bad.append("tool_call")
        if self.finish_reason is not None:
            bad.append("finish_reason")
        if self.usage is not None:
            bad.append("usage")
        if self.thought_signature is not None:
            bad.append("thought_signature")
        if self.reasoning_block is not None:
            bad.append("reasoning_block")
        if self.thought:
            bad.append("thought")
        if bad:
            raise ValueError(
                "LlmDeltaFrame: when `meta` is set, content fields must "
                f"be None / False — got non-empty: {sorted(bad)}"
            )
        return self


class LlmResultFrame(_FrameBase):
    """Router → agent. Terminal response for a `LlmRequestFrame`.

    Field set populated depends on `kind`:
      generate:     text, tool_calls, finish_reason, usage, raw,
                    thought_summary, thought_signature
      embed:        vectors
      count_tokens: total_tokens

    `error` is set when the call failed; SDK raises on receipt.

    `tool_calls` entries carry `id`, `name`, `args`, and an optional
    `thought_signature` (set on the first call of any response with
    function calls — must be round-tripped back on the next turn for
    Gemini 3).
    """

    type: Literal["LlmResult"] = "LlmResult"
    ref_correlation_id: str

    # generate
    text: str = ""
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list, max_length=_LLM_MAX_TOOL_CALLS
    )
    finish_reason: str = "stop"
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    # Gemini-style thought summary (`include_thoughts=True`) — joined
    # text of every part with `thought=True`. None when not requested
    # or when the model returned no thought parts.
    thought_summary: str | None = None
    # Signature on the last text part — recommended (not required) to
    # round-trip; preserves reasoning quality across turns. Required on
    # function-call responses, but those signatures live on each
    # `tool_calls` entry above.
    thought_signature: str | None = None
    # Provider-shaped reasoning blocks (Anthropic `thinking` /
    # `redacted_thinking`). Opaque pass-through; the SDK helper
    # prepends them to the rebuilt assistant turn so multi-turn
    # tool use round-trips without 400ing the upstream.
    reasoning_blocks: list[dict[str, Any]] = Field(
        default_factory=list, max_length=_LLM_MAX_REASONING_BLOCKS
    )

    # embed
    vectors: list[list[float]] = Field(
        default_factory=list, max_length=_LLM_MAX_VECTORS
    )

    # count_tokens
    total_tokens: int = 0

    # Set when the call failed; SDK raises on receipt.
    #
    # Backwards-compatibility note: the wire shape is the JSON form
    # of `LlmResultError`. Old SDKs reading `error["code"]` and
    # `error["message"]` keep working — those fields are still
    # present at the same positions. New optional fields
    # (`retriable`, `retry_after_seconds`, `upstream_class`) appear
    # alongside; older SDKs ignore them.
    error: LlmResultError | None = None


# ---------------------------------------------------------------------------
# File-upload negotiation (Phase 2)
# ---------------------------------------------------------------------------


class FileUploadRequestFrame(_FrameBase):
    """Agent → router. Negotiate a scoped one-shot upload credential
    over the already-authenticated ws.

    The agent hashes the file LOCALLY first and sends only the
    digest + size + metadata — never bytes (those go over a
    separate HTTP connection to the granted URL, keeping bulk
    transfer off the control pump).

    `task_id` is the task the `ctx.files.put()` is happening
    within. It is NOT trusted as proof of anything: the router
    derives the owning `user_id` from the task row and only after
    verifying the connection's authenticated `agent_id` is that
    task's current active executor (mirrors the `complete_task`
    authz). Any `user_id` an agent might try to assert is ignored.
    """

    type: Literal["FileUploadRequest"] = "FileUploadRequest"
    task_id: str
    sha256: str
    byte_size: int
    mime_type: str | None = None
    filename: str | None = None


class FileUploadGrantFrame(_FrameBase):
    """Router → agent. Correlated response to a
    `FileUploadRequestFrame`.

    Exactly one outcome is populated:
      * `error` set        → negotiation refused. A single opaque
                              `"denied"` covers unknown-task /
                              not-your-task / too-large so a
                              caller can't enumerate task ids;
                              `"rate_limited"` is distinguished so
                              clients can back off.
      * `upload_url`+`upload_token`+`expires_at` → stream the bytes
                              to `upload_url` with the bearer
                              `upload_token` (a short-TTL,
                              content-bound `file-upload` JWT).
    """

    type: Literal["FileUploadGrant"] = "FileUploadGrant"
    ref_correlation_id: str
    error: str | None = None
    upload_url: str | None = None
    upload_token: str | None = None
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Router-managed named file store (docs/design/router-managed-file-store.md)
# ---------------------------------------------------------------------------


class FileStoreFrame(_FrameBase):
    """Agent → router. Bind an already-uploaded blob to a NAME in the
    user's stash.

    The bytes were streamed earlier via the content-bound
    upload-with-grant path (`FileUploadRequest` → `POST
    /v1/files/upload`), which created the `files` blob row. This
    frame names it. `task_id` is NOT trusted as proof: the router
    derives `(user_id, session_id)` from the task row after
    verifying the connection's authenticated `agent_id` is the
    task's active executor (same authz as `FileUploadRequest` /
    `complete_task`). The router replies `FileResult` with the
    ACTUAL saved name (which may differ from `filename` after a
    dedup append)."""

    type: Literal["FileStore"] = "FileStore"
    task_id: str
    sha256: str
    byte_size: int
    filename: str | None = None
    persistent: bool = False
    dedup: Literal["append_count", "overwrite", "error"] = "append_count"
    mime_type: str | None = None


class FileFetchFrame(_FrameBase):
    """Agent → router. Request an ephemeral signed download URL for a
    stash file by NAME (`{filename}` or `persist/{filename}`). The
    router resolves the name under the task-derived scope and replies
    `FileResult` with a short-TTL `fetch_url` + `fetch_token` the
    agent pulls over plain HTTP."""

    type: Literal["FileFetch"] = "FileFetch"
    task_id: str
    name: str


class ListFileRequest(BaseModel):
    kind: Literal["list"] = "list"
    persistent: bool = False
    query: str | None = None
    stored_after: datetime | None = None
    model_config = {"extra": "forbid"}


class DeleteFileRequest(BaseModel):
    kind: Literal["delete"] = "delete"
    name: str  # exact or `*`-glob; `{filename}` or `persist/{filename}`
    model_config = {"extra": "forbid"}


class CopyFileRequest(BaseModel):
    kind: Literal["copy"] = "copy"
    src: str
    dst: str
    delete_original: bool = False  # True → move
    model_config = {"extra": "forbid"}


class WriteFileRequest(BaseModel):
    kind: Literal["write"] = "write"
    filename: str
    text: str
    persistent: bool = False
    dedup: Literal["append_count", "overwrite", "error"] = "append_count"
    model_config = {"extra": "forbid"}


FileCommand = Annotated[
    ListFileRequest | DeleteFileRequest | CopyFileRequest | WriteFileRequest,
    Field(discriminator="kind"),
]


class FileManageFrame(_FrameBase):
    """Agent → router. A typed file-management command (list / delete
    / copy / write). `task_id` derives the authoritative
    `(user_id, session_id)` scope as for `FileStore`. The router
    replies `FileResult` with the command-specific outcome."""

    type: Literal["FileManage"] = "FileManage"
    task_id: str
    command: FileCommand


class FileResultFrame(_FrameBase):
    """Router → agent. Correlated response to `FileStore` /
    `FileFetch` / `FileManage`.

    Exactly one outcome shape is populated (alongside the always-set
    `ref_correlation_id`):
      * `error` set → refused. `denied` (unknown-task / not-active-
        executor — non-enumerable), `quota_exceeded`,
        `filename_exists` (dedup="error" collision), `not_found`
        (fetch / blob missing), `invalid_filename`, `rate_limited`.
      * `saved_name` → FileStore / WriteFile / CopyFile: the ACTUAL
        stored name (post-dedup). Reference THIS, never the
        requested name.
      * `fetch_url` + `fetch_token` + `fetch_expires_at` → FileFetch.
      * `names` → ListFile: matching stash names (newest first).
      * `deleted_count` → DeleteFile: directory rows removed.
    """

    type: Literal["FileResult"] = "FileResult"
    ref_correlation_id: str
    error: str | None = None
    saved_name: str | None = None
    fetch_url: str | None = None
    fetch_token: str | None = None
    fetch_expires_at: datetime | None = None
    names: list[str] | None = None
    deleted_count: int | None = None


# ---------------------------------------------------------------------------
# Discriminated union + parser
# ---------------------------------------------------------------------------


Frame = Annotated[
    HelloFrame | WelcomeFrame | CatalogUpdateFrame | AgentInfoUpdateFrame | NewTaskFrame | ResultFrame | ProgressFrame | CancelFrame | ErrorFrame | AckFrame | PingFrame | PongFrame | LlmRequestFrame | LlmDeltaFrame | LlmResultFrame | FileUploadRequestFrame | FileUploadGrantFrame | FileStoreFrame | FileFetchFrame | FileManageFrame | FileResultFrame,
    Field(discriminator="type"),
]


_FRAME_ADAPTER: TypeAdapter[Frame] = TypeAdapter(Frame)


def parse_frame(data: dict[str, Any] | str | bytes) -> Frame:
    """Validate and parse a JSON object/string/bytes into a typed Frame.

    Raises `pydantic.ValidationError` on invalid input. The router edge
    should catch and respond with `Error{code:"frame_invalid"}` rather
    than letting the exception propagate.
    """
    if isinstance(data, (str, bytes)):
        return _FRAME_ADAPTER.validate_json(data)
    return _FRAME_ADAPTER.validate_python(data)


def serialize_frame(frame: Frame) -> str:
    """Serialise a frame to a JSON string ready for `ws.send_text()`."""
    return frame.model_dump_json()


# ---------------------------------------------------------------------------
# Error code catalog (`docs/router/protocol.md` §6)
# ---------------------------------------------------------------------------


class ErrorCode:
    """Canonical error codes used in `ErrorFrame.code` and
    `LlmResultFrame.error.code`.

    Two namespaces share this enum:
      - **Protocol-level** errors (frame validation, auth, session,
        ACL, transport) are emitted as `ErrorFrame`.
      - **LLM-call** errors are emitted in `LlmResultFrame.error.code`.
        These are always one of the LLM_* codes; everything else is
        protocol-level.
    """

    PROTOCOL_VERSION = "protocol_version"
    FRAME_INVALID = "frame_invalid"
    AUTH_FAILED = "auth_failed"
    AUTH_EXPIRED = "auth_expired"
    AGENT_SUSPENDED = "agent_suspended"
    AGENT_REMOVED = "agent_removed"
    SESSION_UNKNOWN = "session_unknown"
    SESSION_CLOSED = "session_closed"
    SCHEMA_MISMATCH = "schema_mismatch"
    ACL_DENIED = "acl_denied"
    ACL_GRANT_INVALID = "acl_grant_invalid"
    QUOTA_EXCEEDED = "quota_exceeded"
    BACKPRESSURE_TIMEOUT = "backpressure_timeout"
    ACK_TIMEOUT = "ack_timeout"
    AGENT_DISCONNECTED = "agent_disconnected"
    AGENT_NOT_FOUND = "agent_not_found"
    INTERNAL_ERROR = "internal_error"

    # LLM-call errors (returned in `LlmResultFrame.error.code`).
    LLM_PRESET_UNKNOWN = "preset_unknown"
    LLM_PRESET_NOT_ALLOWED = "preset_not_allowed"
    LLM_AUTH_LOOKUP_FAILED = "auth_lookup_failed"

    # LLM upstream-classification codes. The router classifies provider
    # SDK exceptions into these via per-provider `_classify`.
    # Until then these constants are RESERVED — they
    # exist on the wire vocabulary so SDK clients can match against
    # them, but the router doesn't emit them yet. See
    # `docs/design/llm-retriable-errors.md`.
    LLM_UPSTREAM_TIMEOUT = "upstream_timeout"
    LLM_UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    LLM_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    LLM_UPSTREAM_INVALID_REQUEST = "upstream_invalid_request"
    LLM_UPSTREAM_AUTH_FAILED = "upstream_auth_failed"
    LLM_UPSTREAM_CONTENT_FILTER = "upstream_content_filter"
    LLM_UPSTREAM_QUOTA_EXHAUSTED = "upstream_quota_exhausted"
    LLM_STREAM_INTERRUPTED = "stream_interrupted"


# Codes for which `LlmResultError.retriable` is auto-set to True.
# Consumed by both router (when constructing the error) and SDK
# (when deciding whether to retry transparently). Single source of
# truth so the wire flag and the typed code can't drift.
RETRIABLE_LLM_CODES: frozenset[str] = frozenset({
    ErrorCode.LLM_UPSTREAM_TIMEOUT,
    ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
    ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
    ErrorCode.LLM_AUTH_LOOKUP_FAILED,
    ErrorCode.INTERNAL_ERROR,
})
