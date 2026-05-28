"""bp_protocol.types — Common Pydantic models shared by router and SDK.

These models are protocol-stable: changes here are wire-breaking.
See `docs/sdk/core.md` and `docs/sdk/services.md` for the
agent-side API surface that consumes them.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

AGENT_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
"""Identifier grammar — see `docs/acl.md` §10."""

GROUP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_:.\-]{0,63}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    """Task state machine states. See `docs/router/state.md` §1."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_CHILDREN = "WAITING_CHILDREN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.TIMED_OUT,
        }


class TaskStatus(StrEnum):
    """Terminal task outcome reported on Result frames."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# Max names in one AgentOutput.files list. A result carrying more
# than this is almost certainly a bug — the cap bounds the jsonb the
# row persists and the per-name resolve loop at the LLM parent.
_MAX_ATTACHMENTS = 256


# ---------------------------------------------------------------------------
# Agent metadata
# ---------------------------------------------------------------------------


class AgentInfo(BaseModel):
    """Identity an agent publishes to the router on registration.

    `groups` and `capabilities` are the only fields the ACL evaluator
    matches against (`docs/acl.md`). `accepts_schema` /
    `produces_schema` are JSON Schema fragments validated at the
    boundary before any handler runs.

    AgentInfo is frozen for the agent's WS lifespan; admins manage
    policy via the firewall rule list, not via per-agent edits.
    """

    agent_id: str
    """Stable globally-unique identifier. Constrained alphabet so it
    cannot collide with `/`, `*`, `@` in pattern syntax."""

    description: str
    """Human-readable, used in catalog entries."""

    groups: list[str] = Field(default_factory=list)
    """Group memberships. See `docs/acl.md` §10 for the grammar."""

    capabilities: list[str] = Field(default_factory=list)
    """Capabilities this agent provides. Dotted lowercase names."""

    accepts_schema: dict[str, Any] | None = None
    """Per-mode payload schemas: `{input_mode: <JSON Schema>|null}`.

    A handler is registered under an explicit `mode` name (default:
    its payload model's class name). Each mode maps to that model's
    JSON Schema; `null` means the mode takes a free-form `dict`
    payload and the router admits it without payload validation
    (the relay / MCP-bridge escape hatch — kept explicit so the
    absence of validation is visible, not silent). The router
    validates `NewTaskFrame.payload` against `accepts_schema[frame.
    input_mode]` at admit; an unknown mode is rejected there.
    Replaces the old single-schema / `oneOf` shape — routing is now
    by explicit mode key, not structural first-match."""

    produces_schema: dict[str, Any] | None = None
    """JSON Schema for `Result.output`. Describes ONLY the typed
    `output` (content + metadata); file outputs are router-managed
    store NAMES in `AgentOutput.files` and declared separately by
    `produces_files`, mirroring how `accepts_schema` covers only
    `NewTask.payload`."""

    produces_files: bool = False
    """Advisory capability flag: this agent may return file-store
    NAMES via `AgentOutput.files`. Declarative only — surfaced to
    tooling / catalog / `build_tools`; the router does NOT reject a
    result whose producer left this False (the names are not
    schema-validated)."""

    non_tool_modes: list[str] = Field(default_factory=list)
    """Modes excluded from auto-generated LLM tool schemas
    (`bp_sdk.tools.build_tools`). The control-plane surface
    (slash commands, session-config mutations, channel-only ops):
    still a normal mode the router validates and dispatches, just
    not advertised to tool-using models. Replaces the separate
    `accepts_control_schema` field + `is_control` frame flag — one
    unified mode registry, with this list as the only tool-visibility
    knob."""

    mode_descriptions: dict[str, str] | None = None
    """Optional per-mode tool descriptions, `{mode: description}`. When a
    mode has an entry, `build_tools` uses it as that mode's tool
    description instead of the agent-level `description` — so a
    multi-mode agent can describe each `call_<agent>_<mode>` distinctly.
    Modes without an entry fall back to `description`. `None` (the
    default) reproduces the single-description behaviour."""

    documentation_url: str | None = None
    """Optional URL to fetch the agent's full markdown docs.

    Validated to require an `http://` or `https://` scheme — the
    admin UI renders this verbatim into an `<a href>`, so a
    `javascript:` payload would be a stored XSS executed in
    admin-session context. Refuse the bad shape at the protocol boundary so
    onboarded agents can't ship a malicious URL through to admin
    consumers."""

    hidden: bool = False
    """SDK convenience flag: suppress this agent from auto-generated
    LLM tool schemas. Does NOT affect ACL — a hidden agent is still
    callable if rules allow it."""

    @field_validator("agent_id")
    @classmethod
    def _agent_id_grammar(cls, v: str) -> str:
        if not AGENT_ID_PATTERN.match(v):
            raise ValueError(
                "agent_id must match [A-Za-z_][A-Za-z0-9_-]{0,63} — see docs/acl.md §10"
            )
        return v

    @field_validator("documentation_url")
    @classmethod
    def _documentation_url_scheme(cls, v: str | None) -> str | None:
        # Stored-XSS guard: the admin UI renders this into
        # `<a href="...">` without scheme inspection. Anything
        # other than `http(s)://` could be a `javascript:` /
        # `data:` / `vbscript:` payload that runs in admin context
        # when the docs link is clicked. Refuse upfront.
        if v is None or v == "":
            return v
        # Case-insensitive check on the scheme. We DON'T accept
        # protocol-relative `//host/...` URLs — those inherit the
        # current page's scheme but would be safe in `<a href>`
        # contexts; nonetheless the convention here is "explicit
        # absolute URL or nothing".
        lower = v.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            raise ValueError(
                "documentation_url must start with http:// or https:// "
                "(other schemes are XSS vectors when rendered in "
                "<a href>)"
            )
        return v

    @field_validator("groups")
    @classmethod
    def _group_grammar(cls, v: list[str]) -> list[str]:
        for g in v:
            if not GROUP_NAME_PATTERN.match(g):
                raise ValueError(
                    f"group {g!r} must match [a-z][a-z0-9_:.-]{{0,63}} — see docs/acl.md §10"
                )
        return v

    @field_validator("capabilities")
    @classmethod
    def _cap_grammar(cls, v: list[str]) -> list[str]:
        for c in v:
            if not CAPABILITY_PATTERN.match(c):
                raise ValueError(
                    f"capability {c!r} must match [a-z][a-z0-9_]*(.[a-z0-9_]+)+ — see docs/acl.md §10"
                )
        return v


# ---------------------------------------------------------------------------
# LLM payloads
# ---------------------------------------------------------------------------


class LLMData(BaseModel):
    """High-level LLM prompt container forwarded to LLM-backed agents."""

    prompt: str
    agent_instruction: str | None = None
    context: str | None = None


# ---------------------------------------------------------------------------
# Standard agent output
# ---------------------------------------------------------------------------


class AgentOutput(BaseModel):
    """Standardised return value produced by handlers.

    `content` and/or `metadata` carry the typed result. `files` is a
    list of NAMES in the router-managed file store
    (`docs/design/router-managed-file-store.md`) the producing agent
    wants auto-fed to an LLM parent as tool-result content —
    `{filename}` (the session stash) or `persist/{filename}` (the
    persistent stash). The producer writes them with
    `ctx.files.store(...)` / `ctx.files.write(...)` first; the LLM
    parent threads each name as a `file_ref` into its next request
    (e.g. via `Message.tool_response_from_result`), and the ROUTER
    resolves the name into the provider call — bytes never cross a
    frame. Unlike the old out-of-band ref channel, names ride inside
    the wire `output` (they're just strings), so a received
    `AgentOutput` carries them on `result.output.files`.
    """

    content: str | None = None
    files: list[str] = Field(
        default_factory=list, max_length=_MAX_ATTACHMENTS
    )
    """File-store NAMES (`{filename}` / `persist/{filename}`) the
    producer wants an LLM parent to see — auto-threaded as
    `file_ref`s and resolved at the router. See the class docstring."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form additional output (token usage, citations, etc.)."""
