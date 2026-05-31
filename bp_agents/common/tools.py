"""bp_agents.common.tools — local tools + the peer-catalog projection.

Two tool surfaces feed `run_llm_loop`:

  - **Peer tools** — the ACL-filtered router catalog, projected into
    neutral `ToolSpec`s via `peer_tool_specs`. Names stay in lockstep
    with `peers.spawn_from_tool_call` / `resolve_tool_name` (both go
    through `build_tools`), so a model-emitted `call_<agent>[_<mode>]`
    always round-trips to the right (agent, mode).
  - **Local tools** — in-process functions an agent runs itself
    (`current_time`, file tools, an agent's own toolset). A `LocalTool`
    pairs a `ToolSpec` with an async handler; `LocalToolset` dispatches
    a model tool call to the handler and packages the result as a
    tool-response `Message`.

Local tool names MUST NOT start with `call_` — that prefix is reserved
for peer-agent tools so the loop's dispatch can tell them apart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bp_sdk import Message, ToolSpec
from bp_sdk.tools import build_tools

if TYPE_CHECKING:
    from bp_sdk import TaskContext

# Handler: (ctx, args) -> a tool-response content shape
# (str, structured dict, or multimodal list of content parts).
LocalToolHandler = Callable[
    ["TaskContext", dict[str, Any]],
    Awaitable[str | dict[str, Any] | list[dict[str, Any]]],
]

_NO_ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


@dataclass
class LocalTool:
    """An in-process tool: a `ToolSpec` advertised to the model + the
    async handler that runs it."""

    spec: ToolSpec
    handler: LocalToolHandler

    def __post_init__(self) -> None:
        if self.spec.name.startswith("call_"):
            raise ValueError(
                f"local tool name {self.spec.name!r} must not start with "
                "'call_' (reserved for peer-agent tools)"
            )


class LocalToolset:
    """A collection of `LocalTool`s with name-keyed dispatch."""

    def __init__(self, tools: list[LocalTool] | None = None) -> None:
        self._by_name: dict[str, LocalTool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: LocalTool) -> None:
        if tool.spec.name in self._by_name:
            raise ValueError(f"duplicate local tool {tool.spec.name!r}")
        self._by_name[tool.spec.name] = tool

    def has(self, name: str) -> bool:
        return name in self._by_name

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._by_name.values()]

    async def dispatch(self, ctx: TaskContext, tool_call: Any) -> Message:
        """Run a model tool call against its handler and package the
        result as a tool-response `Message`. A handler exception is fed
        back as the tool result (string) so the model can recover rather
        than the turn dying."""
        tool = self._by_name[tool_call.name]
        try:
            result = await tool.handler(ctx, tool_call.args or {})
        except Exception as exc:  # noqa: BLE001
            result = f"tool error: {exc}"
        return Message.tool_response(
            tool_call_id=tool_call.id, name=tool_call.name, response=result
        )


def peer_tool_specs(
    ctx: TaskContext, *, for_user_level: str | None = None
) -> list[ToolSpec]:
    """Project the ACL-filtered peer catalog into neutral `ToolSpec`s.

    Reuses `build_tools` (OpenAI shape, then unwrapped to neutral
    `{name, description, parameters}`) so tool names match
    `peers.spawn_from_tool_call`. `for_user_level` overrides the
    catalog tier filter (defaults to the task's user level)."""
    visible = ctx.peers.visible(for_user_level=for_user_level)
    fns = build_tools(visible, provider="openai")
    return [
        ToolSpec(
            name=f["function"]["name"],
            description=f["function"]["description"],
            parameters=f["function"]["parameters"],
        )
        for f in fns
    ]


def make_current_time_tool(timezone: str = "UTC") -> LocalTool:
    """The `current_time` tool every l0/l1 agent carries — user turns are
    stored without a timestamp, so the wall clock is read on demand
    ([agents.md], [sessions.md] §2). Reads the clock in the user's
    timezone (falling back to UTC on an unknown tz)."""

    async def _handler(ctx: TaskContext, args: dict[str, Any]) -> str:
        try:
            tz = ZoneInfo(timezone)
            tz_label = timezone
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
            tz_label = "UTC"
        now = datetime.now(tz)
        return now.strftime(f"%Y-%m-%d %H:%M:%S {tz_label} (%A)")

    return LocalTool(
        spec=ToolSpec(
            name="current_time",
            description=(
                "Get the current wall-clock date and time in the user's "
                "timezone. Call this whenever you need to know the current "
                "time or date."
            ),
            parameters=dict(_NO_ARGS_SCHEMA),
        ),
        handler=_handler,
    )


async def _stash_has(ctx: TaskContext, name: str) -> bool:
    """Best-effort existence check tolerant of the `persist/` prefix —
    `files.list` may return persistent names bare or prefixed."""
    if name.startswith("persist/"):
        names = await ctx.files.list(persistent=True)
        return name in names or name[len("persist/"):] in names
    return name in await ctx.files.list(persistent=False)


def make_send_file_tool(outbound: list[str]) -> LocalTool:
    """A tool the user-facing agents (orchestrator, l1 delegated turns,
    cron) carry so the model can DELIVER a stash file to the user. The
    handler records the name into `outbound`; the caller passes that list
    as `AgentOutput.files`, which the channel resolves + sends
    ([channel.md] §7). Files are only sent when explicitly named here —
    scratch files the model writes are not auto-delivered."""

    async def _handler(ctx: TaskContext, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "send_file needs a non-empty 'name'."
        if not await _stash_has(ctx, name):
            return (
                f"No stash file named '{name}'. Create it first with "
                "write_file, or pass a name a specialist returned."
            )
        if name not in outbound:
            outbound.append(name)
        return (
            f"OK — '{name}' is queued and will be delivered ONLY with your "
            "final text reply. You have NOT answered the user yet: write your "
            "normal final message now (do not stop here, and do not end the "
            "delegation, until you have). A file is never sent on its own."
        )

    return LocalTool(
        spec=ToolSpec(
            name="send_file",
            description=(
                "Queue a file to deliver to the user as an attachment "
                "ALONGSIDE your final text reply. Pass a stash file name — one "
                "you created with write_file, or a name a specialist returned "
                "to you. Use this whenever the user should receive an actual "
                "file (a document, export, image, etc.), not just text about "
                "it. IMPORTANT: this only QUEUES the file — it is delivered "
                "only with your final answer, so after calling it you must "
                "still write your normal text reply. Calling send_file and then "
                "stopping (or ending the delegation) with no final message "
                "sends nothing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Stash file name: '{filename}' (session stash) or "
                            "'persist/{filename}' (persistent stash)."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        handler=_handler,
    )
