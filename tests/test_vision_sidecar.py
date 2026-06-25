"""Unit tests for the multimodal vision sidecar
([docs/design/multimodal-vision-sidecar.md]): config gating, the
`read_file` intent arg, mime gating, and the `read_file` → vision
sub-call routing in the dispatch path. DB-free; the LLM and file store
are faked.
"""

from __future__ import annotations

import asyncio

from bp_agents.common import loop as loop_mod
from bp_agents.common import multimodal_preset_for
from bp_sdk import FileStoreError, ToolCall, file_tools

# --------------------------------------------------------------------------
# config gating
# --------------------------------------------------------------------------

def test_multimodal_preset_for_gating() -> None:
    # engages only when configured AND the turn's preset is text-only
    assert multimodal_preset_for(
        configured="vision", text_only=["lite", "orch"], preset="orch"
    ) == "vision"
    # preset not declared text-only → no proxy (a multimodal main model)
    assert multimodal_preset_for(
        configured="vision", text_only=["lite"], preset="pro"
    ) is None
    # no vision preset configured → inert
    assert multimodal_preset_for(
        configured="", text_only=["orch"], preset="orch"
    ) is None
    # preset unknown / None
    assert multimodal_preset_for(
        configured="vision", text_only=["orch"], preset=None
    ) is None


# --------------------------------------------------------------------------
# read_file intent arg
# --------------------------------------------------------------------------

def test_read_file_intent_arg_toggle() -> None:
    with_intent = {s.name: s for s in file_tools("read_only", read_file_intent=True)}
    assert "purpose" in with_intent["read_file"].parameters["properties"]
    # required list unchanged (purpose is optional)
    assert with_intent["read_file"].parameters["required"] == ["name"]

    plain = {s.name: s for s in file_tools("read_only")}
    assert "purpose" not in plain["read_file"].parameters["properties"]
    # other tools are untouched either way
    assert set(with_intent) == set(plain)


# --------------------------------------------------------------------------
# mime gating
# --------------------------------------------------------------------------

def test_is_visual() -> None:
    # authoritative mime wins
    assert loop_mod._is_visual("image/png", "chart")  # no extension, mime says image
    assert loop_mod._is_visual("application/pdf", "doc")
    assert not loop_mod._is_visual("text/plain", "weird.png")  # mime overrides ext
    # extension fallback when mime is unknown
    assert loop_mod._is_visual(None, "photo.JPG")
    assert loop_mod._is_visual(None, "persist/report.pdf")
    assert not loop_mod._is_visual(None, "notes.txt")
    assert not loop_mod._is_visual(None, "noext")


# --------------------------------------------------------------------------
# fakes for the dispatch path
# --------------------------------------------------------------------------

class _FakeLlm:
    def __init__(self, text: str = "TRANSCRIBED", boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.calls: list[dict] = []

    async def generate(self, messages, *, preset, **kw):
        self.calls.append({"preset": preset, "messages": messages})
        if self.boom:
            raise RuntimeError("vision upstream down")

        class _Resp:
            text = self.text

        return _Resp()


class _FakeStat:
    def __init__(self, mime: str | None) -> None:
        self.mime_type = mime


class _FakeFiles:
    """`stat` reports a mime guessed from the name (override via `mimes`),
    or raises `FileStoreError` for names in `missing`."""

    def __init__(self, *, mimes: dict | None = None, missing: tuple = ()) -> None:
        self._mimes = mimes or {}
        self._missing = missing
        self.stat_calls: list[str] = []

    def llm_ref(self, name, *, as_=None):
        return {"file_ref": {"name": name}}

    async def list(self, **kw):
        return []

    async def stat(self, name):
        self.stat_calls.append(name)
        if name in self._missing:
            raise FileStoreError("not_found")
        if name in self._mimes:
            return _FakeStat(self._mimes[name])
        import mimetypes
        return _FakeStat(mimetypes.guess_type(name)[0])


class _FakeCtx:
    def __init__(self, llm: _FakeLlm, files: _FakeFiles | None = None) -> None:
        self.llm = llm
        self.files = files or _FakeFiles()
        self.task_id = "task_1"


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="tc1", name=name, args=args)


# --------------------------------------------------------------------------
# authoritative gate: the stash `stat` mime decides, not the extension
# --------------------------------------------------------------------------

def test_gate_uses_stat_mime_over_extension() -> None:
    async def _drive() -> None:
        # name LOOKS like an image but the stash says it's text → NOT routed
        files = _FakeFiles(mimes={"weird.png": "text/plain"})
        ctx = _FakeCtx(_FakeLlm(), files)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "weird.png"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
        )
        assert files.stat_calls == ["weird.png"]
        assert ctx.llm.calls == []  # no vision call
        assert msg.content == [{"file_ref": {"name": "weird.png"}}]

    asyncio.run(_drive())


def test_gate_falls_back_to_extension_when_mime_unknown() -> None:
    async def _drive() -> None:
        # extension-less in mime terms (stat returns None) but a real image ext
        files = _FakeFiles(mimes={"shot.png": None})
        ctx = _FakeCtx(_FakeLlm(text="a chart"), files)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "shot.png"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
        )
        assert len(ctx.llm.calls) == 1  # routed via extension fallback
        assert "a chart" in msg.content

    asyncio.run(_drive())


def test_stat_not_found_falls_through_to_normal_dispatch() -> None:
    async def _drive() -> None:
        files = _FakeFiles(missing=("ghost.png",))
        ctx = _FakeCtx(_FakeLlm(), files)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "ghost.png"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
        )
        assert ctx.llm.calls == []  # no vision call
        assert msg.content == [{"file_ref": {"name": "ghost.png"}}]

    asyncio.run(_drive())


# --------------------------------------------------------------------------
# routing: read_file → vision sub-call
# --------------------------------------------------------------------------

def test_image_read_routes_through_vision_with_purpose() -> None:
    async def _drive() -> None:
        llm = _FakeLlm(text="the total is $42")
        ctx = _FakeCtx(llm)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "inv.png", "purpose": "the total"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
            vision_context="user asked for the invoice total",
        )
        # the vision preset was used, the file_ref was attached, the purpose
        # + ambient context reached the vision model
        assert len(llm.calls) == 1
        assert llm.calls[0]["preset"] == "vision"
        parts = llm.calls[0]["messages"][1].content
        assert {"file_ref": {"name": "inv.png"}} in parts
        goal_text = parts[0]["text"]
        assert "the total" in goal_text and "invoice total" in goal_text
        # the tool result is the vision model's TEXT, not a file_ref
        assert msg.content == "[Contents of 'inv.png', read by the vision model for: the total]\nthe total is $42"

    asyncio.run(_drive())


def test_text_file_not_routed_through_vision() -> None:
    async def _drive() -> None:
        llm = _FakeLlm()
        ctx = _FakeCtx(llm)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "notes.txt"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
        )
        # a text file never hits the vision model — falls through to the
        # normal file dispatch, which returns a file_ref part
        assert llm.calls == []
        assert msg.content == [{"file_ref": {"name": "notes.txt"}}]

    asyncio.run(_drive())


def test_no_sidecar_when_preset_unset() -> None:
    async def _drive() -> None:
        llm = _FakeLlm()
        ctx = _FakeCtx(llm)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "inv.png"}),
            None, file_tools_enabled=True, multimodal_preset=None,
        )
        # sidecar off → image goes through the normal file_ref path as before
        assert llm.calls == []
        assert msg.content == [{"file_ref": {"name": "inv.png"}}]

    asyncio.run(_drive())


def test_vision_failure_surfaces_recoverable_text() -> None:
    async def _drive() -> None:
        llm = _FakeLlm(boom=True)
        ctx = _FakeCtx(llm)
        msg = await loop_mod._dispatch_tool_call(
            ctx, _call("read_file", {"name": "scan.pdf", "purpose": "x"}),
            None, file_tools_enabled=True, multimodal_preset="vision",
        )
        # a vision failure becomes a result the model can act on, not a crash
        assert "Could not read 'scan.pdf'" in msg.content
        assert "vision upstream down" in msg.content

    asyncio.run(_drive())


def test_last_user_text_trims_and_picks_latest() -> None:
    from bp_sdk import Message

    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="first"),
        Message(role="assistant", content="reply"),
        Message(role="user", content=[{"text": "second"}, {"file_ref": {"name": "x"}}]),
    ]
    assert loop_mod._last_user_text(msgs) == "second"
    assert loop_mod._last_user_text([Message(role="user", content="z" * 5000)], limit=10) == "z" * 10
