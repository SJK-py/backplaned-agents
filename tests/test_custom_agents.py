"""Tests for operator-defined custom LLM agents.

Source-pin + functional style matching the MCP-bridge suite. Covers the
migration shape, the row model, query helpers, the request-model
validators (the safety surface), the admin view round-trip, the bridge
agent builder + handler, and the supervisor wiring.

See `docs/design/mcp-bridge-custom-llm-agents.md`.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Migration
# ===========================================================================


def _migration_body() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_router" / "db" / "migrations" / "versions"
        / "0008_custom_agents.py"
    ).read_text()


def test_migration_creates_custom_agents() -> None:
    body = _migration_body()
    assert "CREATE TABLE custom_agents" in body
    assert 'down_revision = "0007_user_oidc_identities"' in body


def test_migration_pk_grammar_and_preset_fk() -> None:
    body = _migration_body()
    assert "agent_id ~ '^custom_[a-z][a-z0-9_]*$'" in body
    assert "preset_name   text NOT NULL REFERENCES llm_presets(name)" in body


def test_migration_has_invitation_columns() -> None:
    body = _migration_body()
    assert "pending_invitation_token" in body
    assert "pending_invitation_expires_at" in body


# ===========================================================================
# Row model
# ===========================================================================


def test_custom_agent_row_fields() -> None:
    from bp_router.db.models import CustomAgentRow

    fields = set(CustomAgentRow.model_fields)
    expected = {
        "agent_id", "description", "preset_name", "system_prompt",
        "user_prompt", "parameters", "groups", "capabilities",
        "expose_to_llm", "output_as_file", "enabled", "created_at",
        "updated_at", "created_by", "pending_invitation_token",
        "pending_invitation_expires_at",
    }
    assert expected <= fields


# ===========================================================================
# Query helpers
# ===========================================================================


def test_query_helpers_exist() -> None:
    from bp_router.db import queries

    for name in (
        "list_custom_agents", "get_custom_agent", "insert_custom_agent",
        "update_custom_agent", "delete_custom_agent",
        "record_custom_agent_connected", "set_custom_agent_pending_invitation",
    ):
        assert hasattr(queries, name), name


def test_select_cols_cover_all_columns() -> None:
    from bp_router.db import queries

    cols = queries._CUSTOM_AGENT_SELECT_COLS
    for c in (
        "agent_id", "preset_name", "system_prompt", "user_prompt",
        "parameters", "groups", "capabilities", "expose_to_llm",
        "output_as_file", "enabled", "pending_invitation_token",
    ):
        assert c in cols


# ===========================================================================
# Request-model validators (the safety surface)
# ===========================================================================


def _create(**over):
    from bp_router.api.admin import CustomAgentCreate

    base = {
        "agent_id": "custom_demo",
        "preset_name": "default",
        "system_prompt": "",
        "user_prompt": "",
        "parameters": [],
    }
    base.update(over)
    return CustomAgentCreate(**base)


def test_agent_id_requires_custom_prefix() -> None:
    from pydantic import ValidationError

    _create(agent_id="custom_demo")  # ok
    with pytest.raises(ValidationError):
        _create(agent_id="demo")  # missing prefix
    with pytest.raises(ValidationError):
        _create(agent_id="custom_")  # prefix only, no slug
    with pytest.raises(ValidationError):
        _create(agent_id="custom_Bad")  # uppercase


def test_param_name_grammar() -> None:
    from pydantic import ValidationError

    _create(parameters=[{"name": "topic"}])  # ok
    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "Topic"}])  # uppercase
    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "1topic"}])  # leading digit


def test_param_names_must_be_unique() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "x"}, {"name": "x"}])


def test_prompt_placeholder_must_be_declared() -> None:
    from pydantic import ValidationError

    # Declared → ok.
    _create(
        user_prompt="Write about $topic",
        parameters=[{"name": "topic"}],
    )
    # Undeclared → rejected.
    with pytest.raises(ValidationError):
        _create(user_prompt="Write about $topic", parameters=[])
    # System prompt placeholders are checked too.
    with pytest.raises(ValidationError):
        _create(system_prompt="Use $tone", parameters=[])


def test_dollar_escape_is_not_a_placeholder() -> None:
    # `$$` is a literal `$` for string.Template — not an undeclared param.
    _create(user_prompt="Costs $$5 for $item", parameters=[{"name": "item"}])


def test_capability_and_group_grammar() -> None:
    from pydantic import ValidationError

    _create(capabilities=["custom.search"], groups=["research"])  # ok
    with pytest.raises(ValidationError):
        _create(capabilities=["nodot"])  # capability needs a dot
    with pytest.raises(ValidationError):
        _create(groups=["Bad Group"])  # space / uppercase


# ===========================================================================
# Admin view round-trip
# ===========================================================================


def test_row_to_view_round_trip() -> None:
    from datetime import UTC, datetime

    from bp_router.api.admin import _custom_agent_row_to_view
    from bp_router.db.models import CustomAgentRow

    now = datetime.now(UTC)
    row = CustomAgentRow(
        agent_id="custom_demo", description="d", preset_name="default",
        system_prompt="s", user_prompt="u",
        parameters=[{"name": "topic", "description": "", "required": True}],
        groups=["g"], capabilities=["custom.x"], expose_to_llm=True,
        output_as_file=True, enabled=True, created_at=now, updated_at=now,
        created_by="user_1", pending_invitation_token="tok",
        pending_invitation_expires_at=now,
    )
    view = _custom_agent_row_to_view(row)
    assert view.agent_id == "custom_demo"
    assert view.output_as_file is True
    assert view.parameters[0]["name"] == "topic"
    assert view.pending_invitation_token == "tok"


# ===========================================================================
# Bridge: agent builder
# ===========================================================================


def _spec(tmp_path, **over):
    from bp_mcp_bridge.custom_agent import CustomAgentSpec

    base = dict(
        agent_id="custom_demo",
        description="A demo agent",
        preset_name="default",
        system_prompt="You are helpful.",
        user_prompt="Write about $topic",
        parameters=[{"name": "topic", "description": "the subject", "required": True}],
        groups=["research"],
        capabilities=["custom.write"],
        expose_to_llm=True,
        output_as_file=False,
        router_url="ws://localhost:8000/v1/agent",
        state_dir=tmp_path,
    )
    base.update(over)
    return CustomAgentSpec(**base)


def test_accepts_schema_maps_params_to_string_props() -> None:
    from bp_mcp_bridge.custom_agent import MODE, _accepts_schema

    schema = _accepts_schema(
        [{"name": "topic", "description": "subj", "required": True},
         {"name": "tone", "description": "", "required": False}]
    )
    mode_schema = schema[MODE]
    assert mode_schema["properties"]["topic"] == {"type": "string", "description": "subj"}
    assert mode_schema["properties"]["tone"] == {"type": "string"}
    assert mode_schema["required"] == ["topic"]  # only required ones
    assert mode_schema["additionalProperties"] is False


def test_render_substitutes_and_is_safe() -> None:
    from bp_mcp_bridge.custom_agent import _render

    assert _render("Write about $topic", {"topic": "otters"}) == "Write about otters"
    # Missing key is left literal (safe_substitute never raises).
    assert _render("Hi $name", {}) == "Hi $name"


def test_build_custom_agent_info(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import MODE, build_custom_agent

    agent = build_custom_agent(_spec(tmp_path), "invite-token")
    info = agent.info
    assert info.agent_id == "custom_demo"
    assert "custom.agent" in info.capabilities
    assert "custom.write" in info.capabilities
    assert MODE in info.accepts_schema
    assert info.hidden is False


def test_build_custom_agent_hidden_when_not_exposed(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import build_custom_agent

    agent = build_custom_agent(_spec(tmp_path, expose_to_llm=False), "t")
    assert agent.info.hidden is True


def test_single_mode_external_tool_name(tmp_path) -> None:
    """The mode label must NOT leak into the tool name — a one-mode agent
    surfaces as call_<agent_id>."""
    from bp_mcp_bridge.custom_agent import build_custom_agent
    from bp_sdk.tools import build_tools

    agent = build_custom_agent(_spec(tmp_path), "t")
    destinations = {
        agent.info.agent_id: {
            "description": agent.info.description,
            "accepts_schema": agent.info.accepts_schema,
        }
    }
    tools = build_tools(destinations, provider="anthropic")
    assert [t["name"] for t in tools] == ["call_custom_demo"]
    assert tools[0]["input_schema"]["properties"]["topic"]["type"] == "string"


# ===========================================================================
# Bridge: handler behaviour (inline vs file output)
# ===========================================================================


class _FakeLog:
    def info(self, *a, **k) -> None:  # noqa: D401
        pass


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeLlm:
    def __init__(self) -> None:
        self.calls: list = []

    async def generate(self, messages, *, preset=None, **kw):
        self.calls.append({"messages": messages, "preset": preset})
        return _FakeResp("the answer")


class _FakeStat:
    def __init__(self, byte_size: int, mime_type: str | None = None) -> None:
        self.byte_size = byte_size
        self.mime_type = mime_type


class _FakeFiles:
    def __init__(self, store: dict[str, bytes] | None = None) -> None:
        self.written: list = []
        self.store = store or {}  # name -> bytes, for file_ref reads

    async def write(self, name, text, **kw):
        self.written.append((name, text))
        return name

    async def stat(self, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        return _FakeStat(len(self.store[name]))

    async def read_bytes(self, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        return self.store[name]


class _FakeCtx:
    def __init__(self, store: dict[str, bytes] | None = None) -> None:
        self.log = _FakeLog()
        self.llm = _FakeLlm()
        self.files = _FakeFiles(store)


@pytest.mark.asyncio
async def test_handler_inline_output(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler

    handler = make_custom_handler(_spec(tmp_path, output_as_file=False))
    ctx = _FakeCtx()
    out = await handler(ctx, {"topic": "otters"})
    assert out.content == "the answer"
    assert out.files == []
    # Preset threaded; user prompt rendered; system message present.
    assert ctx.llm.calls[0]["preset"] == "default"
    roles = [m.role for m in ctx.llm.calls[0]["messages"]]
    assert roles == ["system", "user"]
    assert ctx.llm.calls[0]["messages"][1].content == "Write about otters"


@pytest.mark.asyncio
async def test_handler_file_output(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler

    handler = make_custom_handler(_spec(tmp_path, output_as_file=True))
    ctx = _FakeCtx()
    out = await handler(ctx, {"topic": "otters"})
    assert out.files == ["output.md"]
    assert "output.md" in out.content
    assert ctx.files.written == [("output.md", "the answer")]


@pytest.mark.asyncio
async def test_handler_omits_empty_system_prompt(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler

    handler = make_custom_handler(_spec(tmp_path, system_prompt="   "))
    ctx = _FakeCtx()
    await handler(ctx, {"topic": "x"})
    roles = [m.role for m in ctx.llm.calls[0]["messages"]]
    assert roles == ["user"]


# ===========================================================================
# file_ref parameters
# ===========================================================================


def _fileref_spec(tmp_path, **over):
    return _spec(
        tmp_path,
        user_prompt="Summarize: $doc",
        parameters=[{"name": "doc", "description": "the document",
                     "required": True, "file_ref": True}],
        **over,
    )


def test_param_model_accepts_file_ref() -> None:
    p = _create(parameters=[{"name": "doc", "file_ref": True}])
    assert p.parameters[0].file_ref is True
    # Defaults to False when omitted.
    assert _create(parameters=[{"name": "x"}]).parameters[0].file_ref is False


def test_accepts_schema_file_ref_stays_string_with_hint() -> None:
    from bp_mcp_bridge.custom_agent import MODE, _accepts_schema

    schema = _accepts_schema(
        [{"name": "doc", "description": "the doc", "required": True, "file_ref": True}]
    )
    prop = schema[MODE]["properties"]["doc"]
    assert prop["type"] == "string"  # schema type unchanged
    assert "the doc" in prop["description"]
    assert "file" in prop["description"].lower()  # hint appended


@pytest.mark.asyncio
async def test_handler_file_ref_substitutes_text_content(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler

    handler = make_custom_handler(_fileref_spec(tmp_path))
    ctx = _FakeCtx(store={"notes.txt": b"the file body"})
    await handler(ctx, {"doc": "notes.txt"})
    user_msg = ctx.llm.calls[0]["messages"][-1].content
    assert user_msg == "Summarize: the file body"


@pytest.mark.asyncio
async def test_handler_file_ref_rejects_missing_file(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler
    from bp_sdk import InputValidationError

    handler = make_custom_handler(_fileref_spec(tmp_path))
    ctx = _FakeCtx(store={})
    with pytest.raises(InputValidationError):
        await handler(ctx, {"doc": "nope.txt"})


@pytest.mark.asyncio
async def test_handler_file_ref_rejects_non_utf8(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import make_custom_handler
    from bp_sdk import InputValidationError

    handler = make_custom_handler(_fileref_spec(tmp_path))
    ctx = _FakeCtx(store={"bin": b"\xff\xfe\x00\x01"})
    with pytest.raises(InputValidationError):
        await handler(ctx, {"doc": "bin"})


@pytest.mark.asyncio
async def test_handler_file_ref_rejects_oversize(tmp_path) -> None:
    from bp_mcp_bridge.custom_agent import _FILE_REF_MAX_BYTES, make_custom_handler
    from bp_sdk import InputValidationError

    handler = make_custom_handler(_fileref_spec(tmp_path))
    ctx = _FakeCtx(store={"big": b"x" * (_FILE_REF_MAX_BYTES + 1)})
    with pytest.raises(InputValidationError):
        await handler(ctx, {"doc": "big"})


@pytest.mark.asyncio
async def test_handler_non_file_ref_passes_value_through(tmp_path) -> None:
    """A plain (non-file_ref) param keeps substituting the raw value even
    when its value happens to look like a filename."""
    from bp_mcp_bridge.custom_agent import make_custom_handler

    spec = _spec(
        tmp_path, user_prompt="Echo: $topic",
        parameters=[{"name": "topic", "required": True, "file_ref": False}],
    )
    handler = make_custom_handler(spec)
    ctx = _FakeCtx(store={"topic.txt": b"SHOULD NOT BE READ"})
    await handler(ctx, {"topic": "topic.txt"})
    assert ctx.llm.calls[0]["messages"][-1].content == "Echo: topic.txt"


def test_config_signature_sensitive_to_file_ref() -> None:
    from bp_mcp_bridge.custom_agent_bridge import CustomAgentBridgeRow

    plain = CustomAgentBridgeRow.from_admin_dict(
        _admin_dict(parameters=[{"name": "doc", "required": True, "file_ref": False}])
    )
    as_ref = CustomAgentBridgeRow.from_admin_dict(
        _admin_dict(parameters=[{"name": "doc", "required": True, "file_ref": True}])
    )
    assert plain.config_signature() != as_ref.config_signature()


def test_parse_parameters_json() -> None:
    from bp_admin.pages.custom_agents import _parse_parameters_json

    out = _parse_parameters_json(
        '[{"name":"a","description":"d","required":false,"file_ref":true},'
        '{"name":"  ","description":"blank dropped"},'
        '{"name":"b"}]'
    )
    assert out == [
        {"name": "a", "description": "d", "required": False, "file_ref": True},
        {"name": "b", "description": "", "required": True, "file_ref": False},
    ]
    # Malformed JSON degrades to empty list (router does real validation).
    assert _parse_parameters_json("not json") == []
    assert _parse_parameters_json("") == []


# ===========================================================================
# Bridge: row adaptation + config signature
# ===========================================================================


def _admin_dict(**over):
    base = {
        "agent_id": "custom_demo",
        "description": "d",
        "preset_name": "default",
        "system_prompt": "s",
        "user_prompt": "u $topic",
        "parameters": [{"name": "topic", "description": "", "required": True}],
        "groups": ["research"],
        "capabilities": ["custom.write"],
        "expose_to_llm": True,
        "output_as_file": False,
        "enabled": True,
        "pending_invitation_token": "tok",
    }
    base.update(over)
    return base


def test_bridge_row_from_admin_dict() -> None:
    from bp_mcp_bridge.custom_agent_bridge import CustomAgentBridgeRow

    row = CustomAgentBridgeRow.from_admin_dict(_admin_dict())
    assert row.agent_id == "custom_demo"
    assert row.preset_name == "default"
    assert row.pending_invitation_token == "tok"
    assert row.enabled is True


def test_config_signature_excludes_invitation() -> None:
    from bp_mcp_bridge.custom_agent_bridge import CustomAgentBridgeRow

    a = CustomAgentBridgeRow.from_admin_dict(_admin_dict(pending_invitation_token="t1"))
    b = CustomAgentBridgeRow.from_admin_dict(_admin_dict(pending_invitation_token="t2"))
    # A new invitation must NOT restart a healthy bridge.
    assert a.config_signature() == b.config_signature()


def test_config_signature_sensitive_to_prompt_and_preset() -> None:
    from bp_mcp_bridge.custom_agent_bridge import CustomAgentBridgeRow

    base = CustomAgentBridgeRow.from_admin_dict(_admin_dict())
    assert base.config_signature() != (
        CustomAgentBridgeRow.from_admin_dict(
            _admin_dict(system_prompt="changed")
        ).config_signature()
    )
    assert base.config_signature() != (
        CustomAgentBridgeRow.from_admin_dict(
            _admin_dict(preset_name="other")
        ).config_signature()
    )


# ===========================================================================
# Supervisor wiring
# ===========================================================================


def test_supervisor_has_custom_reconcile() -> None:
    from bp_mcp_bridge.supervisor import Supervisor

    for name in ("_reconcile_custom_once", "_start_custom", "_stop_custom"):
        assert hasattr(Supervisor, name), name
    src = inspect.getsource(Supervisor.run)
    assert "_reconcile_custom_once" in src


# ===========================================================================
# Admin app + nav wiring
# ===========================================================================


def test_admin_page_and_nav_registered() -> None:
    import bp_admin.app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert 'custom_agents.router, prefix="/custom-agents"' in src
    nav = (
        Path(__file__).parent.parent / "bp_admin" / "templates" / "base.html"
    ).read_text()
    assert "/admin/custom-agents" in nav


def test_custom_agent_templates_compile() -> None:
    """The form + list templates parse without a TemplateSyntaxError — the
    Alpine parameter editor introduces non-trivial markup, so guard it."""
    pytest.importorskip("jinja2")
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tdir = Path(__file__).parent.parent / "bp_admin" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tdir)),
        autoescape=select_autoescape(["html"]),
    )
    for name in ("custom_agents/form.html", "custom_agents/list.html"):
        env.get_template(name)  # compiles → raises on bad syntax


def test_parameters_tojson_is_attribute_safe() -> None:
    """The exact pipeline the form uses to seed the Alpine editor's x-data
    must produce HTML-attribute-safe JSON (quotes entity-escaped)."""
    pytest.importorskip("jinja2")
    from jinja2 import Environment

    env = Environment(autoescape=True)
    tmpl = env.from_string("{{ params | tojson | forceescape }}")
    out = tmpl.render(
        params=[{"name": "doc", "description": "", "required": True, "file_ref": True}]
    )
    assert "&#34;name&#34;" in out  # double-quotes escaped for the attribute
    assert "doc" in out
    assert "<" not in out and ">" not in out  # no raw angle brackets
