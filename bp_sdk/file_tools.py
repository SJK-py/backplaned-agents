"""bp_sdk.file_tools — ready-made LLM tool definitions for the
router-managed file store, plus the executor that runs a tool call
against `ctx.files`.

`file_tools(bundle=...)` returns neutral `ToolSpec`s to hand to
`ctx.llm.generate(tools=...)`. When the model calls one, the agent's
tool-call loop dispatches it with
`dispatch_file_tool(ctx.files, tool_call)`:

  * `read_file(name)` yields a name `file_ref` tool result — the
    ROUTER resolves it into the provider call on the NEXT `generate`
    turn (scope derived from that call's task), so the bytes never
    enter the agent or the request frame.
  * the mutating tools echo the saved name / count.

Steer authors to the `read_only` bundle unless the workflow genuinely
needs the model to mutate the stash — `delete_file` accepts a `*`
glob, so the `full` bundle is the sharpest edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from bp_sdk.files import FileStoreError
from bp_sdk.llm import Message, ToolCall, ToolSpec

if TYPE_CHECKING:
    from bp_sdk.files import FileStash

_READ_ONLY = ("list_session_file", "list_persist_file", "read_file")
_MUTATING = ("write_file", "delete_file", "copy_file")

Bundle = Literal["read_only", "full"]


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


_QUERY_PROP = {
    "query": {
        "type": "string",
        "description": "Optional case-sensitive substring filter on the name.",
    }
}

_SPECS: dict[str, ToolSpec] = {
    "list_session_file": _spec(
        "list_session_file",
        "List files in the current session's stash (ephemeral — cleared "
        "when the session ends). Returns their names.",
        dict(_QUERY_PROP),
        [],
    ),
    "list_persist_file": _spec(
        "list_persist_file",
        "List files in the user-wide persistent stash (names are prefixed "
        "`persist/`). Returns their names.",
        dict(_QUERY_PROP),
        [],
    ),
    "read_file": _spec(
        "read_file",
        "Show a stash file's content so you can read it. Pass any stash file "
        "name (`{filename}` or `persist/{filename}`); text, images, and "
        "documents are all supported.",
        {
            "name": {
                "type": "string",
                "description": "File name to show, e.g. 'chart.png' or "
                "'persist/report.pdf'.",
            }
        },
        ["name"],
    ),
    "write_file": _spec(
        "write_file",
        "Write a UTF-8 text file into the stash. Returns the saved name, "
        "which may differ from `filename` if a same-named file already "
        "existed.",
        {
            "filename": {"type": "string"},
            "text": {"type": "string"},
            "persistent": {
                "type": "boolean",
                "description": "Store in the user-wide persistent stash "
                "instead of the session stash.",
            },
        },
        ["filename", "text"],
    ),
    "delete_file": _spec(
        "delete_file",
        "Delete a stash file by name, or several with a trailing `*` glob "
        "(e.g. 'draft_*'). Returns the number of files removed.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    "copy_file": _spec(
        "copy_file",
        "Copy (or move) a stash file. Returns the saved destination name.",
        {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "move": {
                "type": "boolean",
                "description": "Delete the source after copying.",
            },
        },
        ["src", "dst"],
    ),
}


def file_tools(bundle: Bundle = "read_only") -> list[ToolSpec]:
    """Ready-made file-store `ToolSpec`s for an LLM agent.

    `read_only` (default) → `list_session_file`, `list_persist_file`,
    `read_file`. `full` adds the MUTATING `write_file`, `delete_file`,
    `copy_file` — only expose it when the workflow genuinely needs the
    model to change the stash.
    """
    if bundle == "read_only":
        names = _READ_ONLY
    elif bundle == "full":
        names = _READ_ONLY + _MUTATING
    else:
        raise ValueError(f"unknown file_tools bundle: {bundle!r}")
    return [_SPECS[n] for n in names]


def is_file_tool(name: str) -> bool:
    """True if `name` is one of the `file_tools` tool names. Use it to
    branch a tool-call loop between `dispatch_file_tool` and a peer
    call (`ctx.peers.spawn_from_tool_call`)."""
    return name in _SPECS


async def dispatch_file_tool(files: FileStash, call: ToolCall) -> Message:
    """Run a `file_tools` call against `files` and build its tool
    response.

    `read_file` returns a name `file_ref` part — the ROUTER resolves it
    on the next `generate`, so the bytes never enter the agent. The
    other tools echo the result. A `FileStoreError` (denied / quota /
    not_found / …) is surfaced as `{"error": code}` so the model can
    recover instead of the turn dying.

    Raises `ValueError` if `call.name` isn't a file tool — guard with
    `is_file_tool` first.
    """
    if not is_file_tool(call.name):
        raise ValueError(f"not a file tool: {call.name!r}")
    args = call.args or {}
    try:
        response = await _run(files, call.name, args)
    except FileStoreError as exc:
        response = {"error": exc.code}
    return Message.tool_response(
        tool_call_id=call.id, name=call.name, response=response,
    )


async def _run(
    files: FileStash, name: str, args: dict[str, Any]
) -> str | dict[str, Any] | list[dict[str, Any]]:
    if name == "list_session_file":
        return {"files": await files.list(persistent=False, query=args.get("query"))}
    if name == "list_persist_file":
        return {"files": await files.list(persistent=True, query=args.get("query"))}
    if name == "read_file":
        fname = args.get("name")
        if not fname:
            return {"error": "read_file requires a 'name'"}
        # A name file_ref — resolved at the router on the next turn.
        return [files.llm_ref(fname)]
    if name == "write_file":
        fname = args.get("filename")
        text = args.get("text")
        if not fname or text is None:
            return {"error": "write_file requires 'filename' and 'text'"}
        saved = await files.write(
            fname, text, persistent=bool(args.get("persistent", False))
        )
        return {"saved_name": saved}
    if name == "delete_file":
        target = args.get("name")
        if not target:
            return {"error": "delete_file requires a 'name'"}
        return {"deleted_count": await files.delete(target)}
    if name == "copy_file":
        src = args.get("src")
        dst = args.get("dst")
        if not src or not dst:
            return {"error": "copy_file requires 'src' and 'dst'"}
        saved = await files.copy(src, dst, move=bool(args.get("move", False)))
        return {"saved_name": saved}
    raise ValueError(f"not a file tool: {name!r}")  # unreachable via is_file_tool
