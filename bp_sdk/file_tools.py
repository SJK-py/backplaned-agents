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

import asyncio
import codecs
import mimetypes
from typing import TYPE_CHECKING, Any, Literal

from bp_sdk.files import FileStoreError
from bp_sdk.llm import Message, ToolCall, ToolSpec

if TYPE_CHECKING:
    from bp_sdk.files import FileStash

_READ_ONLY = ("list_session_file", "list_persist_file", "stat_file", "read_file")
_MUTATING = ("write_file", "delete_file", "copy_file")

Bundle = Literal["read_only", "full"]

# read_file windowing (text files only) — the model reads a bounded slice and
# pages with `offset`, so a large file can't flood context and a giant log is
# reachable in chunks. Images/PDFs ignore these (shown whole via the router).
_DEFAULT_READ_CHARS = 20_000   # returned when the model passes no max_chars
_MAX_READ_CHARS = 500_000      # hard ceiling on one window (clamp, don't error)
_READ_CHUNK_BYTES = 1 << 16    # 64 KiB streaming-decode chunk

# Textual `application/*` types that decode as UTF-8 like `text/*` (mirrors the
# router's `_is_text_mime`, so the SDK windowed-read and the router's text
# classification agree on what counts as text).
_TEXT_APP_MIMES = frozenset({
    "application/json", "application/yaml", "application/x-yaml",
    "application/xml", "application/javascript", "application/x-javascript",
    "application/toml", "application/x-sh", "application/csv",
    "application/x-csv", "application/x-ndjson",
})


def _is_textual_mime(mime: str | None) -> bool:
    """True for a mime fed as UTF-8 text: any `text/*`, a small set of
    textual `application/*`, and `+json`/`+xml`/`+yaml` suffixes."""
    m = (mime or "").split(";", 1)[0].strip().lower()
    return (
        m.startswith("text/")
        or m in _TEXT_APP_MIMES
        or m.endswith(("+json", "+xml", "+yaml"))
    )


def _is_text_name(name: str) -> bool:
    """Whether a stash NAME looks like a text file, by extension — the SDK
    decides text-vs-binary here (no stat round-trip) so an image/PDF read
    stays a one-shot `file_ref`. A mislabelled binary that slips through is
    caught by the UTF-8 decode and falls back to the `file_ref` path."""
    return _is_textual_mime(mimetypes.guess_type(name)[0])


def _clamp_int(value: Any, *, default: int, lo: int, hi: int | None) -> int:
    """Parse a tool arg into a bounded int (clamp, never error — a model
    sometimes sends a string or an out-of-range number)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    n = max(lo, n)
    return n if hi is None else min(n, hi)


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
        "when the session ends). Returns each file's name, size, and type.",
        dict(_QUERY_PROP),
        [],
    ),
    "list_persist_file": _spec(
        "list_persist_file",
        "List files in the user-wide persistent stash (names are prefixed "
        "`persist/`). Returns each file's name, size, and type.",
        dict(_QUERY_PROP),
        [],
    ),
    "stat_file": _spec(
        "stat_file",
        "Get a stash file's metadata — size, type (MIME), and when it was "
        "stored — WITHOUT reading its contents. Use this to check how large "
        "a file is or what kind it is before deciding whether/how to read it.",
        {
            "name": {
                "type": "string",
                "description": "File name, e.g. 'chart.png' or "
                "'persist/report.pdf'.",
            }
        },
        ["name"],
    ),
    "read_file": _spec(
        "read_file",
        "Show a stash file's content so you can read it. Pass any stash file "
        "name (`{filename}` or `persist/{filename}`); text, images, and "
        "documents are all supported. A TEXT file returns a bounded window "
        "(by default the first ~20000 characters) — if it's truncated the "
        "result says how many characters remain and the `offset` to continue "
        "from, so you can page through a large file. `max_chars` / `offset` "
        "apply to text only; images and PDFs are always shown whole.",
        {
            "name": {
                "type": "string",
                "description": "File name to show, e.g. 'chart.png' or "
                "'persist/report.pdf'.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Text only. Max characters to return in this "
                "read (default 20000). Raise it to pull more at once.",
            },
            "offset": {
                "type": "integer",
                "description": "Text only. Character position to start from "
                "(default 0) — pass the offset the previous read reported to "
                "continue past a truncation.",
            },
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


# Optional `purpose` arg, added to `read_file` only when the caller opts in
# (`read_file_intent=True`). It lets the model state WHAT it needs from a
# file, which a suite-side vision sidecar threads to a separate multimodal
# model reading image/PDF files on a text-only model's behalf
# ([../docs/design/multimodal-vision-sidecar.md]). The plain
# `dispatch_file_tool` ignores it; only the suite loop reads it.
_PURPOSE_PROP = {
    "purpose": {
        "type": "string",
        "description": (
            "Optional but recommended: what you need from this file — e.g. "
            "'the total due and the due date', 'what the error dialog says', "
            "'transcribe the handwriting'. For an image/PDF read by a "
            "separate vision model this focuses the extraction; if you omit "
            "it the whole file is read. Ignored for plain text files."
        ),
    }
}


def _read_file_spec(with_intent: bool) -> ToolSpec:
    """The `read_file` spec, optionally carrying the `purpose` arg."""
    base = _SPECS["read_file"]
    if not with_intent:
        return base
    props = dict(base.parameters["properties"])
    props.update(_PURPOSE_PROP)
    return ToolSpec(
        name=base.name,
        description=base.description,
        parameters={
            "type": "object",
            "properties": props,
            "required": list(base.parameters["required"]),
        },
    )


def file_tools(
    bundle: Bundle = "read_only", *, read_file_intent: bool = False
) -> list[ToolSpec]:
    """Ready-made file-store `ToolSpec`s for an LLM agent.

    `read_only` (default) → `list_session_file`, `list_persist_file`,
    `read_file`. `full` adds the MUTATING `write_file`, `delete_file`,
    `copy_file` — only expose it when the workflow genuinely needs the
    model to change the stash.

    `read_file_intent` adds an optional `purpose` arg to `read_file`
    (the suite enables it when a vision sidecar is active for the turn).
    """
    if bundle == "read_only":
        names = _READ_ONLY
    elif bundle == "full":
        names = _READ_ONLY + _MUTATING
    else:
        raise ValueError(f"unknown file_tools bundle: {bundle!r}")
    return [
        _read_file_spec(read_file_intent) if n == "read_file" else _SPECS[n]
        for n in names
    ]


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


def _human_size(n: int) -> str:
    """Compact human size: 0 B / 12.3 KB / 4.5 MB. Model-friendly so the
    file's heft is obvious at a glance."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


def _entry_view(entry: Any) -> dict[str, Any]:
    """Render a `FileStat` for the model: name + human size + type."""
    return {
        "name": entry.name,
        "size": _human_size(entry.byte_size),
        "type": entry.mime_type or "unknown",
    }


def _slice_text_file(path: Any, offset: int, max_chars: int) -> tuple[str, int, int]:
    """Read a UTF-8 text file from disk and return `(window, start,
    total_chars)` for the character range `[offset, offset+max_chars)`.

    Streams the file in chunks with an incremental decoder, keeping only the
    window (plus a 64 KiB buffer) in memory — so a multi-GB file slices
    without ever being loaded whole. Raises `UnicodeDecodeError` if the bytes
    aren't valid UTF-8 (the caller falls back to the `file_ref` path)."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pos = 0            # chars decoded so far
    want_hi = offset + max_chars
    parts: list[str] = []
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK_BYTES)
            final = not chunk
            text = decoder.decode(chunk, final)
            n = len(text)
            if n:
                lo, hi = offset - pos, want_hi - pos  # window range within chunk
                if hi > 0 and lo < n:
                    parts.append(text[max(lo, 0):min(hi, n)])
                pos += n
            if final:
                break
    return "".join(parts), min(offset, pos), pos


async def _read_file(
    files: FileStash, name: str, args: dict[str, Any]
) -> str | dict[str, Any] | list[dict[str, Any]]:
    """Resolve a `read_file` call.

    A TEXT file (decided by extension) is read SDK-side and returned as a
    bounded CHARACTER window — `max_chars` (default 20000) from `offset`
    (default 0) — with a marker + the next `offset` when truncated, so a
    large file can't flood context and is page-able. The file is streamed off
    a local temp copy and sliced incrementally (`_slice_text_file`), so even a
    multi-GB file is read with bounded memory. Anything else (image / PDF /
    unknown type) returns a name `file_ref` the ROUTER resolves into
    multimodal content on the next turn, unchanged. A file that looked like
    text by extension but isn't valid UTF-8 also falls back to `file_ref`.

    A `FileStoreError` (e.g. `not_found`) propagates to `dispatch_file_tool`,
    which surfaces it as `{"error": code}`."""
    if not _is_text_name(name):
        return [files.llm_ref(name)]
    max_chars = _clamp_int(
        args.get("max_chars"), default=_DEFAULT_READ_CHARS, lo=1, hi=_MAX_READ_CHARS
    )
    offset = _clamp_int(args.get("offset"), default=0, lo=0, hi=None)

    # `read` streams the blob to a local temp file (memory-safe download); we
    # slice the window off disk and drop the copy so paging can't pile up.
    path = await files.read(name)
    try:
        window, start, total = await asyncio.to_thread(
            _slice_text_file, path, offset, max_chars
        )
    except UnicodeDecodeError:
        return [files.llm_ref(name)]  # not UTF-8 → let the router decide
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    end = start + len(window)
    remaining = total - end
    header = f"File: {name} (characters {start}–{end} of {total})"
    footer = (
        f"\n\n…[{remaining} more characters — call read_file again "
        f"with offset={end} to continue]"
        if remaining > 0 else ""
    )
    return f"{header}\n\n{window}{footer}"


async def _run(
    files: FileStash, name: str, args: dict[str, Any]
) -> str | dict[str, Any] | list[dict[str, Any]]:
    if name == "list_session_file":
        entries = await files.list_detailed(persistent=False, query=args.get("query"))
        return {"files": [_entry_view(e) for e in entries]}
    if name == "list_persist_file":
        entries = await files.list_detailed(persistent=True, query=args.get("query"))
        return {"files": [_entry_view(e) for e in entries]}
    if name == "stat_file":
        fname = args.get("name")
        if not fname:
            return {"error": "stat_file requires a 'name'"}
        entry = await files.stat(fname)
        return {
            "name": entry.name,
            "size": _human_size(entry.byte_size),
            "bytes": entry.byte_size,
            "type": entry.mime_type or "unknown",
            "stored_at": entry.created_at.isoformat(),
        }
    if name == "read_file":
        fname = args.get("name")
        if not fname:
            return {"error": "read_file requires a 'name'"}
        return await _read_file(files, fname, args)
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
