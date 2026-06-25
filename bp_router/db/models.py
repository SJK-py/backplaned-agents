"""bp_router.db.models — Row dataclasses for DB results.

Pydantic-validated for runtime safety; cheap to instantiate from
asyncpg `Record` rows via `Model.model_validate(dict(record))`.

Schema is owned by Alembic migrations. Adding a column means adding a
migration AND updating the model here. CI checks the two stay in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from bp_protocol.types import TaskPriority, TaskState


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# ---------------------------------------------------------------------------
# Identity / users
# ---------------------------------------------------------------------------


class UserRow(_Row):
    user_id: str
    level: str  # admin | service | tierN  (see bp_router.principals)
    auth_kind: str  # password | oidc | api_key
    auth_secret_hash: str | None  # password hash, OIDC sub, or API key hash
    email: str | None
    created_at: datetime
    suspended_at: datetime | None = None
    # Terminal soft-delete marker. Distinct from
    # `suspended_at` which is reversible. Auth paths refuse a user
    # with non-null `deleted_at`; the F8 serviced_by sweep clears
    # references; the admin UI hides deleted users by default. The
    # row stays in the table so `actor_id` / `user_id` FK
    # references from audit and task history don't dangle.
    deleted_at: datetime | None = None
    # Permanent-erasure marker (GDPR). Set by `purge_user` AFTER `deleted_at`:
    # the user's content is hard-deleted, PII (`email`/`auth_secret_hash`) is
    # scrubbed, and this stamp is the durable signal the suite reconcile loop
    # keys off to erase the user's suite rows + per-user LanceDB. The tombstone
    # row itself is kept for FK integrity + the append-only audit chain.
    purged_at: datetime | None = None
    # F8: list of service-principal user_ids authorised to mint
    # credentials (refresh tokens, password-reset tokens) on this
    # user's behalf. Each entry MUST point at a user with
    # `level="service"`; the admin grant endpoint enforces this
    # app-side. Empty = default-deny.
    serviced_by: list[str] = []


class OidcIdentityRow(_Row):
    """An external OIDC subject linked to a local user. SSO login resolves a
    validated `(issuer, sub)` to `user_id` here. One user may have many rows
    (multiple OPs); `(issuer, sub)` is globally unique (one OP identity maps
    to exactly one account). `email_at_link` is a profile snapshot taken at
    link time — never an authority for matching."""

    issuer: str
    sub: str
    user_id: str
    email_at_link: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None



class SessionRow(_Row):
    session_id: str
    user_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentRow(_Row):
    agent_id: str
    kind: str  # external | embedded
    status: str  # active | suspended | pending
    capabilities: list[str]
    groups: list[str]
    agent_info: dict[str, Any]
    auth_token_hash: str | None
    public_key: str | None
    registered_at: datetime
    last_seen_at: datetime | None = None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskRow(_Row):
    task_id: str
    parent_task_id: str | None
    root_task_id: str
    user_id: str
    session_id: str
    agent_id: str
    # Agent that issued the task. Fan-out target for Progress / Result
    # relay. For tasks issued from a TaskContext, `caller_agent_id`
    # equals the running agent's id; for tasks admitted via the
    # admin HTTP path, it's the synthetic `admin_console` agent.
    caller_agent_id: str
    # Agent currently executing the task. Equals `agent_id` at admit
    # time; reassigned by DelegationFrame to support L0→L1 handover.
    active_agent_id: str
    state: TaskState
    status_code: int | None = None
    idempotency_key: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    deadline: datetime | None = None
    created_at: datetime
    updated_at: datetime
    input: dict[str, Any] = {}
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TaskEventRow(_Row):
    # `event_id` is declared `UUID` (not `str`) to match the DB
    # column type — `task_events.event_id` is `uuid PRIMARY KEY
    # DEFAULT gen_random_uuid()`, and asyncpg returns it as a
    # `uuid.UUID` object. Typing it as `str` here was a real bug:
    # every `INSERT INTO task_events ... RETURNING *` round-trip
    # crashed `TaskEventRow.model_validate` with `Input should be
    # a valid string`. Pydantic
    # serialises `UUID` as the canonical string at JSON-encode
    # time, so the wire shape is unchanged.
    event_id: UUID
    task_id: str
    ts: datetime
    kind: str  # transition | dispatch | ack | grant | progress
    actor_agent_id: str | None
    from_state: TaskState | None = None
    to_state: TaskState | None = None
    payload: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class FileRow(_Row):
    file_id: str
    sha256: str
    user_id: str
    session_id: str | None = None
    task_id: str | None = None
    byte_size: int
    mime_type: str | None = None
    storage_url: str  # backend-internal locator (e.g. s3://bucket/key)
    original_filename: str | None = None
    created_at: datetime
    expires_at: datetime | None = None


class FileNameRow(_Row):
    """A row in the `file_names` directory: a NAME → blob mapping in
    the router-managed file store. `scope` is 'session:{session_id}'
    or 'persist'; `file_id` points at the `files` blob registry;
    `byte_size` is denormalised for the per-user quota SUM."""

    user_id: str
    scope: str
    filename: str
    file_id: str
    byte_size: int
    created_at: datetime
    updated_at: datetime


class FileEntryRow(_Row):
    """A directory row joined to its blob's `mime_type` — the `stat` /
    detailed-`list` projection. `byte_size` + `created_at` come from the
    `file_names` directory; `mime_type` from the `files` blob (null when
    the blob was stored without one)."""

    filename: str
    byte_size: int
    mime_type: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# ACL / audit / invitations
# ---------------------------------------------------------------------------


class AclRuleRow(_Row):
    rule_id: str
    ord: int
    name: str | None = None
    description: str | None = None
    effect: str  # allow | deny
    user_level: str  # * | admin | service | tierN
    caller_pattern: str
    callee_pattern: str
    created_at: datetime
    created_by: str | None = None


class AuditLogRow(_Row):
    # Same `uuid`-typed column as `TaskEventRow.event_id`.
    # `audit_log.event_id` is
    # `uuid PRIMARY KEY DEFAULT gen_random_uuid()`; asyncpg
    # returns a `UUID` object.
    event_id: UUID
    # Monotonic chain-order key (bigserial). `append_audit_event`
    # picks the predecessor by `ORDER BY seq DESC`; ts/event_id are
    # not insertion-ordered (see queries.append_audit_event).
    seq: int
    ts: datetime
    actor_kind: str  # user | agent | admin | system
    actor_id: str | None
    event: str
    target_kind: str | None = None
    target_id: str | None = None
    payload: dict[str, Any] = {}
    prev_hash: str | None = None
    self_hash: str


class InvitationRow(_Row):
    token_hash: str
    level: str  # admin | service | tierN — see bp_router.principals
    expires_at: datetime
    used_at: datetime | None = None
    used_by: str | None = None
    created_by: str
    # `created_at` backs the list `ORDER BY created_at DESC,
    # token_hash DESC` pagination (Adm-M3). `idempotency_key` is
    # the per-admin retry-dedup key from the `Idempotency-Key`
    # header (Adm-M2); partial-unique on (created_by,
    # idempotency_key) WHERE idempotency_key IS NOT NULL.
    created_at: datetime
    idempotency_key: str | None = None
    # When true, consuming this invitation at `POST /v1/onboard` also
    # provisions a co-located `usr_service_{agent_id}` service principal
    # (see migration 0002 / api/onboard.py).
    provisions_service_user: bool = False


class PendingRegistrationRow(_Row):
    """A pending user-registration request. Admin approves to convert
    into a real user row (which creates the matching `users.serviced_by`
    auto-grant entry when `submitted_by_service_user_id` is set)."""

    registration_id: str
    channel: str
    external_id: str
    display_name: str | None = None
    requested_email: str | None = None
    metadata: dict[str, Any] = {}
    requested_at: datetime
    attempts: int
    last_attempt_at: datetime
    submitted_by_service_user_id: str | None = None
    # Self-service web signup: argon2 hash of the password the user chose on
    # the public form. NULL for channel-submitted registrations (password set
    # later via the reset-token flow). Approval seeds the user's hash from this
    # when present, so a web user can log in the moment they're approved.
    requested_password_hash: str | None = None


class RefreshTokenRow(_Row):
    token_hash: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    replaced_by: str | None = None


# ---------------------------------------------------------------------------
# LLM presets
# ---------------------------------------------------------------------------


class LlmPresetRow(_Row):
    """A bundled provider + model + sampling-defaults configuration
    that agents reference by name. See `bp_router.llm.presets`.

    `min_user_level` follows the ACL user-level grammar:
      * | admin | service | tierN
    Callers must satisfy this gate via `acl._user_level_satisfies`.

    `api_key` is the inline alternative to `api_key_ref` — when set,
    it wins. The admin API never returns it; it's masked into a
    `has_api_key: bool` indicator on the response view.

    `fallback_preset` + `max_retries` form the retry / fallback chain
    consulted by `LlmService._call_with_fallback`. See the service
    docstring for the exact attempt order.
    """

    name: str
    description: str | None
    provider: str  # gemini | anthropic | openai | openai-embeddings | openai-compatible | openai-compatible-embeddings
    concrete_model: str
    api_key_ref: str
    api_key: str | None = None
    # Endpoint base URL for openai-compatible(-embeddings) providers
    # (vLLM / LM Studio / llama.cpp-server / etc.). Required for those
    # providers; unused for hosted ones.
    base_url: str | None = None
    min_user_level: str  # * | admin | service | tierN
    default_temperature: float | None = None
    default_max_tokens: int | None = None
    default_provider_options: dict[str, Any] = {}
    fallback_preset: str | None = None
    max_retries: int = 0
    # TRUE → catalogue-owned (re-synced every boot); FALSE → admin-created.
    managed: bool = False
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None

    @field_validator("default_provider_options", mode="before")
    @classmethod
    def _coerce_null_options(cls, v: Any) -> Any:
        # The column is jsonb and nominally non-null, but a PATCH carrying an
        # explicit JSON `null` could write SQL NULL. Coerce None → {} on read
        # so a (legacy/edge) NULL row validates instead of 500-ing every
        # subsequent list/get/load_presets. The write path also COALESCEs
        # (see queries.update_llm_preset); this is belt-and-suspenders.
        return {} if v is None else v


class McpServerRow(_Row):
    """Admin-managed config for one MCP server bridged into the
    backplane. PK is `server_id`, not `agent_id` — one row generates one
    runtime agent `mcp_<server_id>` (one mode per MCP tool, exposed to the
    LLM as `call_mcp_<server_id>_<tool>`) when the bridge connects."""

    server_id: str
    description: str
    url: str | None = None  # null for stdio
    transport: str  # sse | streamable_http | stdio
    auth_kind: str  # none | bearer | header
    auth_value_ref: str | None = None  # env:// or secret:// ref; never raw
    auth_header_name: str | None = None  # required when auth_kind=header
    groups: list[str] = []
    expose_to_llm: bool = True
    tools_cache: dict[str, Any] | None = None
    refresh_requested_at: datetime | None = None
    created_at: datetime
    last_connected_at: datetime | None = None
    created_by: str | None = None
    # Transient onboarding handoff: an admin-minted short-TTL invitation the
    # bridge consumes to onboard `mcp_<server_id>`, cleared once it connects.
    pending_invitation_token: str | None = None
    pending_invitation_expires_at: datetime | None = None
    # Admin-settable: extra agent capabilities (merged with the auto-derived
    # mcp.bridge / mcp.tool.* set) + tool names the bridge must not expose.
    capabilities: list[str] = []
    disabled_tools: list[str] = []
    # stdio transport: the subprocess the bridge spawns. `env_refs` maps env var
    # names to env://VAR / secret://… refs the bridge resolves at spawn (never
    # raw secrets). All null/empty for url transports.
    command: str | None = None
    args: list[str] = []
    env_refs: dict[str, str] = {}
