"""bp_router.llm.attachments — resolve named `file_ref` parts in an
LlmRequest's messages into provider-ready content.

An agent puts `ctx.files.llm_ref(name)`
(`{"file_ref": {"name": "chart.png"|"persist/r.pdf", "as": …}}`) in
a `Message.content` list. This runs BEFORE the provider adapter sees
the messages: it resolves the NAME against the router-managed file
store — scoped to the `(user_id, session_id)` DERIVED from the
request's `task_id` (active-executor verified, never asserted) — and
replaces each `file_ref` part IN PLACE, routing by blob TYPE:

  * `image/*`         → base64 `{"image": …}` multimodal envelope.
  * `application/pdf` → base64 `{"document": …}` multimodal envelope.
  * text types        → the decoded contents inlined as a
    `{"text": …}` part. Providers don't accept arbitrary text mime
    types in a document slot, but text is universally accepted as
    text — so markdown / html / csv / json land readable.
  * anything else      → a `{"text": …}` reference NOTE stating the
    file isn't a multimodal-supported type; the bytes are NOT
    inlined (avoids a guaranteed provider 4xx on an unfeedable blob).

An explicit `"as": "image"|"document"` overrides the type routing
(force the base64 envelope). Bytes never ride the agent→router
frame, so a file over the WS `max_payload_bytes` cap is fed without
tripping it.

See `docs/design/router-managed-file-store.md` §8.1.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from bp_router.attachments import (
    AttachmentResolutionError,
    derive_task_file_scope,
)
from bp_router.db import queries
from bp_router.file_store import _PERSIST_PREFIX

logger = logging.getLogger(__name__)


def _downscale_image(data: bytes, mime: str, max_long_side: int) -> tuple[bytes, str]:
    """Shrink an image so its LONGER side is at most `max_long_side` px,
    preserving aspect ratio and only ever shrinking (never upscaling). Returns
    `(bytes, mime)` — the originals unchanged when resizing is off, the image
    is already small enough, or anything goes wrong (best-effort: never fail
    the LLM call over a thumbnail). Multimodal token cost is dimension-based,
    so this is what actually cuts the per-image token bill."""
    if max_long_side <= 0:
        return data, mime
    try:
        from PIL import Image  # noqa: PLC0415

        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "PNG").upper()
        if max(img.size) <= max_long_side:
            return data, mime
        # thumbnail() is in-place, aspect-preserving, and downscale-only.
        img.thumbnail((max_long_side, max_long_side))
        out = io.BytesIO()
        if fmt in ("JPEG", "JPG"):
            # JPEG can't hold alpha/palette modes — flatten to RGB.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=85)
        else:
            img.save(out, format=fmt)
        return out.getvalue(), mime
    except Exception:  # noqa: BLE001 — never break the call over a resize
        logger.warning(
            "llm_image_downscale_failed",
            extra={"event": "llm_image_downscale_failed"},
            exc_info=True,
        )
        return data, mime

# Textual `application/*` subtypes fed as text alongside every
# `text/*`. Structured-syntax `+json`/`+xml`/`+yaml` suffixes are
# matched separately.
_TEXT_APP_MIMES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/ecmascript",
        "application/x-ndjson",
        "application/toml",
        "application/x-sh",
        "application/csv",
        "application/x-csv",
    }
)


def _base_mime(mime: str | None) -> str:
    """Lowercased mime with any `; charset=…` parameters stripped."""
    return (mime or "").split(";", 1)[0].strip().lower()


def _is_text_mime(mime: str) -> bool:
    """True for a base mime fed as text: every `text/*`, a small set
    of textual `application/*` types, and the `+json`/`+xml`/`+yaml`
    structured-syntax suffixes."""
    if mime.startswith("text/"):
        return True
    if mime in _TEXT_APP_MIMES:
        return True
    return mime.endswith(("+json", "+xml", "+yaml"))


def _classify(mime: str | None, as_: Any) -> str:
    """Pick the inline strategy: `image` / `document` (base64
    multimodal envelopes), `text` (decode + inline as a text part), or
    `reference` (a text note — the type isn't multimodal-supported, so
    the bytes are not inlined).

    An explicit `as_` of `image`/`document` (the `llm_ref(as_=…)`
    escape hatch) overrides the mime routing."""
    if as_ in ("image", "document"):
        return as_  # type: ignore[return-value]
    m = _base_mime(mime)
    if m.startswith("image/"):
        return "image"
    if m == "application/pdf":
        return "document"
    if _is_text_mime(m):
        return "text"
    return "reference"


def _split_named_ref(name: str) -> tuple[str, str] | None:
    """Parse a stash `name` file_ref into `(scope_suffix, bare)` where
    `scope_suffix` is `"persist"` or the sentinel `""` (session — the
    caller substitutes the derived session_id). Returns None on an
    invalid name (empty / nested path)."""
    if name.startswith(_PERSIST_PREFIX):
        bare = name[len(_PERSIST_PREFIX):]
        suffix = "persist"
    else:
        bare = name
        suffix = ""
    if not bare or "/" in bare:
        return None
    return suffix, bare


def _collect_file_refs(
    messages: list[dict[str, Any]],
) -> list[tuple[list[Any], int, dict[str, Any]]]:
    """Find every `file_ref` part in document order. Returns
    `(content_list, index_in_list, file_ref_payload)` so each can be
    replaced in place after resolution."""
    found: list[tuple[list[Any], int, dict[str, Any]]] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if isinstance(part, dict) and isinstance(
                part.get("file_ref"), dict
            ):
                found.append((content, i, part["file_ref"]))
    return found


async def _inline_blob(
    state: Any,
    *,
    content: list[Any],
    idx: int,
    fr: dict[str, Any],
    sha256: str,
    byte_size: int | None,
    mime_type: str | None,
    display_name: str | None,
    inline_cap: int,
    image_max_long_side: int = 0,
) -> None:
    """Resolve a blob into a provider-ready content part IN PLACE,
    routing by type (see the module docstring): images/PDFs base64-
    inline as `{"image"|"document": …}`; text files inline their
    decoded contents as `{"text": …}`; any other type becomes a
    `{"text": …}` reference note (no bytes inlined)."""
    name = display_name or "file"
    shown_mime = _base_mime(mime_type) or "application/octet-stream"
    strategy = _classify(mime_type, fr.get("as"))

    if strategy == "reference":
        # Not a multimodal-supported type — reference only, no bytes
        # inlined (so this never trips the cap, and never 4xxs a
        # provider with a blob it can't read).
        content[idx] = {
            "text": f"[attached file {name!r} ({shown_mime}): not a "
            f"multimodal-supported type — contents not inlined]"
        }
        return

    # image / document / text all inline the bytes — enforce the cap.
    if byte_size is not None and byte_size > inline_cap:
        raise AttachmentResolutionError(
            "attachment_too_large",
            f"LLM file attachment is {byte_size} bytes, over the "
            f"{inline_cap}-byte inline cap (provider-native upload "
            f"for large media is not yet supported — send a smaller "
            f"file or a provider-native reference part)",
        )
    file_store = state.file_store  # type: ignore[attr-defined]
    buf = bytearray()
    # `open()` is `async def` returning an async iterator — await to get the
    # iterator, THEN `async for`. (`async for x in open(...)` iterates the
    # coroutine itself → "got coroutine".) Matches api/files.py.
    stream = await file_store.open(sha256)
    async for chunk in stream:
        buf += chunk
        if len(buf) > inline_cap:
            raise AttachmentResolutionError(
                "attachment_too_large",
                f"LLM file attachment exceeds the {inline_cap}-byte "
                f"inline cap",
            )

    if strategy == "text":
        try:
            text = bytes(buf).decode("utf-8")
        except UnicodeDecodeError:
            # Labelled text but not valid UTF-8 — reference, don't inline.
            content[idx] = {
                "text": f"[attached file {name!r} ({shown_mime}): could "
                f"not be decoded as UTF-8 text — contents not inlined]"
            }
            return
        content[idx] = {"text": f"File: {name}\n\n{text}"}
        return

    # image / document → base64 multimodal envelope. Downscale images first
    # (token cost is dimension-based) — documents/PDFs are passed through.
    blob_bytes = bytes(buf)
    if strategy == "image":
        blob_bytes, mime_type = _downscale_image(
            blob_bytes, mime_type or "application/octet-stream", image_max_long_side
        )
    envelope: dict[str, Any] = {
        "mime_type": mime_type or "application/octet-stream",
        "data": base64.b64encode(blob_bytes).decode("ascii"),
    }
    if display_name:
        envelope["display_name"] = display_name
    content[idx] = {strategy: envelope}


async def resolve_request_file_refs(
    state: Any,
    *,
    messages: list[dict[str, Any]],
    user_id: str,
    caller_agent_id: str,
    task_id: str | None = None,
) -> None:
    """Resolve + inline every `file_ref` part in `messages`, in
    place. No-op when there are none. Raises
    `AttachmentResolutionError` (caller maps it to an LlmResult
    error) on an authz refusal or an over-cap file.

    A `file_ref` is `{"name": "{filename}"|"persist/{filename}",
    "as": …}` — a router-managed named-store ref. Authority is the
    `(user_id, scope, filename)` tuple (no per-file key), so the
    scope is DERIVED from the task row (`task_id` + active-executor
    check), NEVER from the asserted `user_id`. `task_id` is therefore
    REQUIRED when any name ref is present.

    Bytes are streamed + inlined HERE, at the router, before the
    provider adapter — they never ride the agent→router frame, so a
    file over the WS `max_payload_bytes` cap is fed without tripping
    it."""
    locations = _collect_file_refs(messages)
    if not locations:
        return

    settings = state.settings  # type: ignore[attr-defined]
    max_refs = settings.llm_request_max_file_refs
    if len(locations) > max_refs:
        raise AttachmentResolutionError(
            "too_many_file_refs",
            f"LLM request references {len(locations)} files; the "
            f"per-request limit is {max_refs}",
        )

    inline_cap = settings.llm_attachment_inline_max_bytes
    image_max_long_side = settings.llm_image_max_long_side_px
    # Every file_ref must carry a name.
    if any(loc[2].get("name") is None for loc in locations):
        raise AttachmentResolutionError(
            "invalid_attachment",
            "each file_ref must carry a 'name'",
        )
    if not task_id:
        raise AttachmentResolutionError(
            "file_ref_requires_task",
            "a name file_ref requires the LlmRequest to carry a "
            "task_id (the scope is derived from the task, never "
            "from an asserted user_id/session_id)",
        )
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        scope_t = await derive_task_file_scope(conn, task_id, caller_agent_id)
        if scope_t is None:
            raise AttachmentResolutionError(
                "denied",
                "name file_ref task is unknown or the agent is not "
                "its active executor",
            )
        owner_user_id, session_id = scope_t
        sq = queries.Scope.user(conn, owner_user_id)
        for content, idx, fr in locations:
            parsed = _split_named_ref(fr["name"])
            if parsed is None:
                raise AttachmentResolutionError(
                    "invalid_attachment",
                    f"invalid file name {fr['name']!r}",
                )
            suffix, bare = parsed
            scope = "persist" if suffix == "persist" else f"session:{session_id}"
            name_row = await sq.resolve_file_name(scope, bare)
            if name_row is None:
                raise AttachmentResolutionError(
                    "attachment_not_found",
                    f"no stash file named {fr['name']!r}",
                )
            blob = await sq.get_file(name_row.file_id)
            if blob is None:
                raise AttachmentResolutionError(
                    "attachment_not_found",
                    f"stash file {fr['name']!r} blob is missing",
                )
            await _inline_blob(
                state, content=content, idx=idx, fr=fr,
                sha256=blob.sha256, byte_size=blob.byte_size,
                mime_type=blob.mime_type,
                display_name=blob.original_filename or bare,
                inline_cap=inline_cap,
                image_max_long_side=image_max_long_side,
            )
