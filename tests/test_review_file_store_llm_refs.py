"""Router-managed file store — Phase 5: name `file_ref` resolution in
LLM requests.

`docs/design/router-managed-file-store.md` §8.1. An LLM message may
carry `{"file_ref": {"name": "chart.png"|"persist/r.pdf", "as": …}}`.
The router resolves the name against the named store — scoped to the
`(user_id, session_id)` DERIVED from the request's `task_id`
(active-executor verified, NEVER asserted; #256) — streams the
blob's bytes, and inlines them BEFORE the provider adapter. Bytes
never ride the agent→router frame.

This file covers name-ref resolution end to end: scope derivation
(derived from the task, never asserted), inline replacement of the
`file_ref` part, and the refusal cases.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from bp_router.attachments import AttachmentResolutionError


def _store_returning(data: bytes):  # type: ignore[no-untyped-def]
    # Mirror the real FileStore.open contract: an `async def` that RETURNS an
    # async iterator (so the caller must `await` before `async for`). A plain
    # async-generator stub here would let `async for x in open(...)` work
    # without the await and mask the real bug (TypeError: got coroutine).
    async def _gen(data: bytes):  # type: ignore[no-untyped-def]
        yield data

    async def _open(_sha: str):  # type: ignore[no-untyped-def]
        return _gen(data)

    return _open


class _AcquireCtx:
    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace()  # conn unused (derive is monkeypatched)

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False


def _state(*, data: bytes = b"PNGBYTES", inline_cap: int = 5_000_000):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        settings=SimpleNamespace(
            llm_request_max_file_refs=16,
            llm_attachment_inline_max_bytes=inline_cap,
        ),
        file_store=SimpleNamespace(open=_store_returning(data)),
        # `acquire()` returns a fresh async-ctx each call; the class
        # is itself the callable.
        db_pool=SimpleNamespace(acquire=_AcquireCtx),
    )


class _FakeScope:
    def __init__(self, names: dict, blobs: dict) -> None:
        self._names = names    # (scope, bare) -> file_id
        self._blobs = blobs    # file_id -> blob row

    async def resolve_file_name(self, scope: str, bare: str):  # type: ignore[no-untyped-def]
        fid = self._names.get((scope, bare))
        if fid is None:
            return None
        return SimpleNamespace(file_id=fid, byte_size=0)

    async def get_file(self, file_id: str):  # type: ignore[no-untyped-def]
        return self._blobs.get(file_id)


def _blob(sha: str = "sha1", *, mime: str = "image/png", size: int = 8, fn: str = "chart.png"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        sha256=sha, byte_size=size, mime_type=mime, original_filename=fn,
    )


def _patch(monkeypatch, *, scope_result, fake_scope=None):  # type: ignore[no-untyped-def]
    import bp_router.llm.attachments as mod

    async def _derive(_conn, _task, _agent):
        return scope_result

    monkeypatch.setattr(mod, "derive_task_file_scope", _derive)
    if fake_scope is not None:
        monkeypatch.setattr(mod.queries.Scope, "user", lambda _conn, _uid: fake_scope)


def _msg(name: str, as_: str | None = None) -> list:
    fr = {"name": name}
    if as_:
        fr["as"] = as_
    return [{"role": "user", "content": [{"text": "see"}, {"file_ref": fr}]}]


# ---------------------------------------------------------------------------
# Happy path — session + persist scope
# ---------------------------------------------------------------------------


def test_session_name_resolves_and_inlines(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "chart.png"): "fil_a"},
        blobs={"fil_a": _blob()},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("chart.png")
    asyncio.run(resolve_request_file_refs(
        _state(), messages=msgs, user_id="ASSERTED_IGNORED",
        caller_agent_id="a", task_id="t1",
    ))
    # The file_ref part was replaced in place with an image envelope.
    part = msgs[0]["content"][1]
    assert "file_ref" not in part
    assert "image" in part
    assert part["image"]["mime_type"] == "image/png"
    assert part["image"]["display_name"] == "chart.png"


def test_persist_name_resolves_under_persist_scope(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("persist", "r.pdf"): "fil_p"},
        blobs={"fil_p": _blob(mime="application/pdf", fn="r.pdf")},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("persist/r.pdf")
    asyncio.run(resolve_request_file_refs(
        _state(), messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    # application/pdf → document modality by default.
    assert "document" in part


# ---------------------------------------------------------------------------
# Type routing — text → inline text, unsupported → reference note
# ---------------------------------------------------------------------------


def test_text_file_inlines_decoded_contents(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "notes.md"): "fil_t"},
        blobs={"fil_t": _blob(mime="text/markdown", fn="notes.md")},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("notes.md")
    asyncio.run(resolve_request_file_refs(
        _state(data=b"# Title\n\nbody text"),
        messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    # Decoded contents inlined as text, not a base64 document blob.
    assert part == {"text": "File: notes.md\n\n# Title\n\nbody text"}


def test_html_file_inlines_as_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "page.html"): "fil_h"},
        blobs={"fil_h": _blob(mime="text/html", fn="page.html")},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("page.html")
    asyncio.run(resolve_request_file_refs(
        _state(data=b"<h1>hi</h1>"),
        messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    assert "document" not in part and "image" not in part
    assert part["text"].endswith("<h1>hi</h1>")


def test_unsupported_type_becomes_reference_note(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    # A zip far OVER the inline cap — the reference path must NOT read
    # the bytes or trip the cap; it just notes the file.
    fake = _FakeScope(
        names={("session:sess_1", "bundle.zip"): "fil_z"},
        blobs={
            "fil_z": _blob(mime="application/zip", fn="bundle.zip", size=10_000_000)
        },
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("bundle.zip")
    asyncio.run(resolve_request_file_refs(
        _state(inline_cap=1000),
        messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    assert "image" not in part and "document" not in part
    assert "not a multimodal-supported type" in part["text"]
    assert "bundle.zip" in part["text"]


def test_non_utf8_text_falls_back_to_reference(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "weird.txt"): "fil_w"},
        blobs={"fil_w": _blob(mime="text/plain", fn="weird.txt")},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("weird.txt")
    asyncio.run(resolve_request_file_refs(
        _state(data=b"\xff\xfe\x00bad"),
        messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    assert "could not be decoded" in part["text"]


def test_explicit_as_document_overrides_text_routing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The `llm_ref(as_="document")` escape hatch forces the base64
    document envelope even for a text mime."""
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "notes.md"): "fil_t"},
        blobs={"fil_t": _blob(mime="text/markdown", fn="notes.md")},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    msgs = _msg("notes.md", as_="document")
    asyncio.run(resolve_request_file_refs(
        _state(data=b"# md"),
        messages=msgs, user_id="u1", caller_agent_id="a", task_id="t1",
    ))
    part = msgs[0]["content"][1]
    assert "document" in part
    assert part["document"]["mime_type"] == "text/markdown"


# ---------------------------------------------------------------------------
# Authority — derived scope, never asserted
# ---------------------------------------------------------------------------


def test_name_ref_requires_task_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    # No task_id but a name ref present → refused before any lookup.
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(), messages=_msg("chart.png"), user_id="u1",
            caller_agent_id="a", task_id=None,
        ))
    assert ei.value.code == "file_ref_requires_task"


def test_name_ref_denied_when_not_active_executor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    # derive returns None (unknown task / not active) → opaque denied.
    _patch(monkeypatch, scope_result=None)
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(), messages=_msg("chart.png"), user_id="u1",
            caller_agent_id="a", task_id="t1",
        ))
    assert ei.value.code == "denied"


def test_name_resolution_uses_derived_user_not_asserted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The Scope is built from the DERIVED owner_user_id, not the
    frame's `user_id` — pin that the asserted value never reaches
    `Scope.user`."""
    import bp_router.llm.attachments as mod
    from bp_router.llm.attachments import resolve_request_file_refs

    seen_uid = {}

    async def _derive(_c, _t, _a):
        return ("DERIVED_U", "sess_1")

    fake = _FakeScope(names={("session:sess_1", "x.png"): "f"},
                      blobs={"f": _blob()})

    def _scope_user(_conn, uid):
        seen_uid["uid"] = uid
        return fake

    monkeypatch.setattr(mod, "derive_task_file_scope", _derive)
    monkeypatch.setattr(mod.queries.Scope, "user", _scope_user)

    asyncio.run(resolve_request_file_refs(
        _state(), messages=_msg("x.png"), user_id="ASSERTED_U",
        caller_agent_id="a", task_id="t1",
    ))
    assert seen_uid["uid"] == "DERIVED_U"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_name_not_found(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(names={}, blobs={})
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(), messages=_msg("missing.png"), user_id="u1",
            caller_agent_id="a", task_id="t1",
        ))
    assert ei.value.code == "attachment_not_found"


def test_invalid_name_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=_FakeScope({}, {}))
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(), messages=_msg("nested/path.png"), user_id="u1",
            caller_agent_id="a", task_id="t1",
        ))
    assert ei.value.code == "invalid_attachment"


def test_ref_without_name_rejected() -> None:
    from bp_router.llm.attachments import resolve_request_file_refs

    msgs = [{"role": "user", "content": [{"file_ref": {"as": "image"}}]}]
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(), messages=msgs, user_id="u1", caller_agent_id="a",
            task_id="t1",
        ))
    assert ei.value.code == "invalid_attachment"


def test_oversize_name_blob_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from bp_router.llm.attachments import resolve_request_file_refs

    fake = _FakeScope(
        names={("session:sess_1", "big.png"): "fil_big"},
        blobs={"fil_big": _blob(size=10_000_000)},
    )
    _patch(monkeypatch, scope_result=("u1", "sess_1"), fake_scope=fake)
    with pytest.raises(AttachmentResolutionError) as ei:
        asyncio.run(resolve_request_file_refs(
            _state(inline_cap=1000), messages=_msg("big.png"),
            user_id="u1", caller_agent_id="a", task_id="t1",
        ))
    assert ei.value.code == "attachment_too_large"


# ---------------------------------------------------------------------------
# Wiring source pins
# ---------------------------------------------------------------------------


def test_dispatch_threads_task_id_into_resolver() -> None:
    import bp_router.dispatch as d

    src = inspect.getsource(d)
    # The LLM dispatch passes the frame's task_id so name refs can
    # derive their scope.
    assert "task_id=frame.task_id," in src
    assert "resolve_request_file_refs(" in src


def test_resolver_uses_shared_derive_helper() -> None:
    import bp_router.llm.attachments as mod

    src = inspect.getsource(mod.resolve_request_file_refs)
    assert "derive_task_file_scope(conn, task_id, caller_agent_id)" in src
    # Name refs require a task_id.
    assert '"file_ref_requires_task"' in src


def test_derive_helper_is_shared_not_duplicated() -> None:
    """`derive_task_file_scope` lives in `bp_router.attachments` and
    is used by BOTH the file-frame handlers (dispatch) and the LLM
    name-ref resolver — one authz definition, no drift."""
    import bp_router.attachments as shared
    import bp_router.dispatch as d
    import bp_router.llm.attachments as llm

    assert hasattr(shared, "derive_task_file_scope")
    # dispatch imports it (aliased) and the llm resolver imports it.
    assert "derive_task_file_scope" in inspect.getsource(d)
    assert "derive_task_file_scope" in inspect.getsource(llm)
