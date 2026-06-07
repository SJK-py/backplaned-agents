"""bp_sdk — Python SDK for writing agents against the bp_router.

See `docs/backplaned/sdk/core.md` and `docs/backplaned/sdk/services.md`.

The expected agent surface is small:

    from bp_sdk import Agent, TaskContext
    from bp_protocol import AgentInfo, AgentOutput, LLMData

    agent = Agent(info=AgentInfo(...))

    @agent.handler
    async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
        ...

    if __name__ == "__main__":
        agent.run()

LLM-side imports for agents that build their own messages, tools,
and stream consumers:

    from bp_sdk import (
        Message, ToolCall, ToolSpec,
        LlmResponse, LlmDelta, TokenUsage,
        StreamAccumulator,
        document_part,
        image_part,
        RetryPolicy,
    )

Public-API stability: the names listed in `__all__` are the stable
public surface, covered by semver from 1.0 onward. Anything imported
from a submodule path (`bp_sdk.dispatch`, `bp_sdk.peers`,
`bp_sdk.transport`, etc.) is considered internal and may move or
change shape between minor releases without notice.
"""

from bp_sdk.agent import Agent
from bp_sdk.context import TaskContext
from bp_sdk.errors import (
    CancellationError,
    HandlerError,
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    UpstreamError,
)
from bp_sdk.file_tools import dispatch_file_tool, file_tools, is_file_tool
from bp_sdk.files import FileStash, FileStoreError
from bp_sdk.llm import (
    LlmCallError,
    LlmDelta,
    LlmResponse,
    LlmServiceClient,
    Message,
    RetryPolicy,
    StreamAccumulator,
    TokenUsage,
    ToolCall,
    ToolSpec,
    document_part,
    image_part,
)
from bp_sdk.peers import (
    AckTimeout,
    PeerCallError,
    ResultTimeout,
    SpawnRejected,
    UnexpectedResponse,
)
from bp_sdk.settings import AgentConfig, load_agent_config

__all__ = [
    "AckTimeout",
    "Agent",
    "AgentConfig",
    "CancellationError",
    "HandlerError",
    "InputValidationError",
    "LlmCallError",
    "LlmDelta",
    "LlmResponse",
    "LlmServiceClient",
    "Message",
    "NotFoundError",
    "PeerCallError",
    "PermissionDeniedError",
    "ResultTimeout",
    "RetryPolicy",
    "SpawnRejected",
    "StreamAccumulator",
    "TaskContext",
    "TokenUsage",
    "ToolCall",
    "ToolSpec",
    "UnexpectedResponse",
    "UpstreamError",
    "document_part",
    "image_part",
    "FileStash",
    "FileStoreError",
    "dispatch_file_tool",
    "file_tools",
    "is_file_tool",
    "load_agent_config",
]
