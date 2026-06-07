"""bp_protocol — Shared frame and type definitions for the reworked Backplaned.

This package is the single source of truth for the wire protocol between
router and agents. Both `bp_router` and `bp_sdk` depend on it.

See `docs/backplaned/router/protocol.md` for the protocol specification and
`docs/backplaned/overview.md` for the overall architecture.

Public-API stability: the names listed in `__all__` are the stable
public surface, covered by semver from 1.0 onward. The submodules
(`bp_protocol.frames`, `bp_protocol.types`) are import-path only —
the field set and validators may shift between minor releases as
long as the wire-level semantics documented in
`docs/backplaned/router/protocol.md` are preserved.
"""

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    CatalogUpdateFrame,
    ErrorCode,
    ErrorFrame,
    Frame,
    HelloFrame,
    LlmCallKind,
    LlmDeltaFrame,
    LlmRequestFrame,
    LlmResultFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)
from bp_protocol.types import (
    AgentInfo,
    AgentOutput,
    LLMData,
    TaskPriority,
    TaskState,
    TaskStatus,
)

PROTOCOL_VERSION = "1"

__all__ = [
    "PROTOCOL_VERSION",
    # types
    "AgentInfo",
    "AgentOutput",
    "LLMData",
    "TaskPriority",
    "TaskState",
    "TaskStatus",
    # frames
    "AckFrame",
    "CancelFrame",
    "CatalogUpdateFrame",
    "ErrorCode",
    "ErrorFrame",
    "Frame",
    "HelloFrame",
    "LlmCallKind",
    "LlmDeltaFrame",
    "LlmRequestFrame",
    "LlmResultFrame",
    "NewTaskFrame",
    "PingFrame",
    "PongFrame",
    "ProgressFrame",
    "ResultFrame",
    "WelcomeFrame",
    "parse_frame",
    "serialize_frame",
]
