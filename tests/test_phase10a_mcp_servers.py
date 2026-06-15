"""Tests for Phase 10a: MCP server config schema + admin endpoints + UI.

Source-pin style matching the rest of the admin-UI suite. Covers
migration shape, model fields, query helpers, request-model
validators (the bulk of the safety surface), endpoint wiring, and
the admin UI page handlers + templates.

Phase 10a is config-only — no bridge runtime to test. Live MCP
protocol bridging lands in 10b.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# Migration
# ===========================================================================


def _migration_body() -> str:
    # `mcp_servers` was historically standalone migration 0006; it
    # is now folded into the consolidated `0001_initial_schema`
    # baseline (pre-release migration consolidation).
    return (
        Path(__file__).parent.parent
        / "bp_router" / "db" / "migrations" / "versions"
        / "0001_initial_schema.py"
    ).read_text()


def test_consolidated_migration_creates_mcp_servers() -> None:
    body = _migration_body()
    assert "down_revision = None" in body
    assert "CREATE TABLE mcp_servers" in body


def test_migration_pk_is_server_id_not_agent_id() -> None:
    """D.1 means one row generates N agents — the PK must be
    `server_id`, not any single `agent_id`."""
    body = _migration_body()
    assert "server_id            text         PRIMARY KEY" in body


def test_migration_enforces_server_id_grammar() -> None:
    body = _migration_body()
    assert "server_id ~ '^[a-z][a-z0-9_]+$'" in body


def test_migration_auth_consistency_check_exists() -> None:
    """The CHECK constraint must enforce: auth_kind=none → no
    ref/header; bearer → ref only; header → ref + header. This is
    defense-in-depth alongside the Pydantic validator."""
    body = _migration_body()
    assert "mcp_servers_auth_consistent" in body
    # Three branches.
    assert "auth_kind = 'none'" in body
    assert "auth_kind = 'bearer'" in body
    assert "auth_kind = 'header'" in body


def test_migration_creates_groups_gin_index() -> None:
    body = _migration_body()
    assert "mcp_servers_groups_idx" in body
    assert "USING gin (groups)" in body


# ===========================================================================
# Model
# ===========================================================================


def test_mcp_server_row_has_required_fields() -> None:
    from bp_router.db.models import McpServerRow

    fields = McpServerRow.model_fields
    for name in (
        "server_id", "description", "url", "transport",
        "auth_kind", "auth_value_ref", "auth_header_name",
        "groups", "expose_to_llm", "tools_cache",
        "refresh_requested_at", "created_at", "last_connected_at",
        "created_by",
    ):
        assert name in fields, f"McpServerRow missing field {name!r}"


def test_mcp_server_row_expose_to_llm_defaults_true() -> None:
    """The expose_to_llm column defaults to true (admin opts OUT for
    high-tool-count servers, not in)."""
    from bp_router.db.models import McpServerRow

    assert McpServerRow.model_fields["expose_to_llm"].default is True


# ===========================================================================
# Queries
# ===========================================================================


def test_list_mcp_servers_query_exists() -> None:
    from bp_router.db import queries

    assert hasattr(queries, "list_mcp_servers")
    src = inspect.getsource(queries.list_mcp_servers)
    assert "FROM mcp_servers" in src
    assert "ORDER BY server_id" in src


def test_insert_mcp_server_takes_all_columns() -> None:
    from bp_router.db import queries

    sig = inspect.signature(queries.insert_mcp_server)
    for name in (
        "server_id", "description", "url", "transport",
        "auth_kind", "auth_value_ref", "auth_header_name",
        "groups", "expose_to_llm", "created_by",
    ):
        assert name in sig.parameters


def test_update_mcp_server_only_patches_provided_fields() -> None:
    """Source pin: the dynamic SET-clause builder skips None
    fields. Without this, a PATCH that only updates `groups`
    would null out every other column."""
    from bp_router.db import queries

    src = inspect.getsource(queries.update_mcp_server)
    assert "sets: list[str] = []" in src
    assert "if description is not None:" in src
    assert "if not sets:" in src


def test_update_mcp_server_auth_kind_writes_ref_and_header_together() -> None:
    """Changing auth_kind requires writing the matching ref/header
    in the same UPDATE (the DB CHECK refuses inconsistent rows).
    Source pin so a future refactor that decouples these is
    caught."""
    from bp_router.db import queries

    src = inspect.getsource(queries.update_mcp_server)
    # Inside the `if auth_kind is not None:` block, both ref and
    # header columns are appended to sets.
    assert "if auth_kind is not None:" in src
    branch = src.split("if auth_kind is not None:", 1)[1].split("if groups is not None:", 1)[0]
    assert "auth_value_ref" in branch
    assert "auth_header_name" in branch


def test_mark_refresh_requested_returns_truthy_on_hit() -> None:
    """The bridge polls for `refresh_requested_at IS NOT NULL`;
    this helper just stamps it. Returns True/False so the admin
    endpoint can 404 on missing rows."""
    from bp_router.db import queries

    src = inspect.getsource(queries.mark_mcp_server_refresh_requested)
    assert "SET refresh_requested_at = now()" in src
    assert "result.endswith(\" 1\")" in src


# ===========================================================================
# Request-model validators (the bulk of the safety surface)
# ===========================================================================


def test_create_request_accepts_minimal_payload() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    req = McpServerCreate(
        server_id="filesystem",
        url="https://mcp.example.com/sse",
        transport="sse",
    )
    assert req.auth_kind == "none"
    assert req.expose_to_llm is True
    assert req.capabilities == []
    assert req.disabled_tools == []


def test_create_request_accepts_capabilities_and_disabled_tools() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    req = McpServerCreate(
        server_id="minimax", url="https://x/sse", transport="sse",
        capabilities=["mcp.minimax", "mcp.media"],
        disabled_tools=["play_audio"],
    )
    assert req.capabilities == ["mcp.minimax", "mcp.media"]
    assert req.disabled_tools == ["play_audio"]


def test_create_request_validates_capability_grammar() -> None:
    pytest.importorskip("fastapi")
    import pydantic

    from bp_router.api.admin import McpServerCreate

    with pytest.raises(pydantic.ValidationError):
        McpServerCreate(
            server_id="x", url="https://x/sse", transport="sse",
            capabilities=["Not A Capability"],  # spaces/caps → rejected
        )


def test_create_request_accepts_stdio() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    req = McpServerCreate(
        server_id="minimax", transport="stdio", command="uvx",
        args=["minimax-mcp"], env_refs={"MINIMAX_API_KEY": "env://MINIMAX_API_KEY"},
    )
    req._check_transport_consistency()
    assert req.url is None and req.command == "uvx"
    assert req.args == ["minimax-mcp"]


def test_stdio_transport_consistency_requires_command_and_null_url() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    # stdio needs a command
    with pytest.raises(ValueError, match="command is required"):
        McpServerCreate(
            server_id="srv", transport="stdio",
        )._check_transport_consistency()
    # stdio must not carry a url
    with pytest.raises(ValueError, match="url must be null"):
        McpServerCreate(
            server_id="srv", transport="stdio", command="uvx",
            url="https://x/sse",
        )._check_transport_consistency()
    # url transport must not carry a command
    with pytest.raises(ValueError, match="command/args/env_refs apply only to stdio"):
        McpServerCreate(
            server_id="srv", transport="sse", url="https://x/sse", command="uvx",
        )._check_transport_consistency()


def test_env_refs_accept_refs_and_inline_literals() -> None:
    pytest.importorskip("fastapi")
    import pydantic

    from bp_router.api.admin import McpServerCreate

    # Both a ref and an inline literal are accepted (like a preset's inline
    # api_key vs api_key_ref).
    req = McpServerCreate(
        server_id="srv", transport="stdio", command="uvx",
        env_refs={"REFD": "env://MINIMAX_API_KEY", "INLINE": "raw-secret-value"},
    )
    assert req.env_refs == {
        "REFD": "env://MINIMAX_API_KEY", "INLINE": "raw-secret-value",
    }
    # The env var NAME is still validated; an empty value is rejected.
    with pytest.raises(pydantic.ValidationError):
        McpServerCreate(
            server_id="srv", transport="stdio", command="uvx",
            env_refs={"bad name": "x"},
        )
    with pytest.raises(pydantic.ValidationError):
        McpServerCreate(
            server_id="srv", transport="stdio", command="uvx",
            env_refs={"K": ""},
        )


def test_launcher_allowlist_default_uvx_only() -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.api.admin import _check_mcp_launcher

    _check_mcp_launcher("uvx", None)  # default allowlist accepts uvx
    with pytest.raises(HTTPException) as exc:
        _check_mcp_launcher("bash", None)
    assert exc.value.status_code == 400


def test_create_request_validates_server_id_grammar() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    # Uppercase, leading digit, dash, slash, dot — all rejected.
    for bad in ("Filesystem", "1filesystem", "file-system", "file/system", "file.system"):
        with pytest.raises(ValidationError):
            McpServerCreate(server_id=bad, url="https://x/", transport="sse")


def test_create_request_validates_url_scheme() -> None:
    """XSS guard — http(s):// only, matching the AgentInfo
    `documentation_url` pattern."""
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/plain,..."):
        with pytest.raises(ValidationError):
            McpServerCreate(server_id="x", url=bad, transport="sse")


def test_create_request_rejects_unknown_transport() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    with pytest.raises(ValidationError):
        McpServerCreate(
            server_id="x", url="https://x/", transport="websocket",
        )


def test_create_request_rejects_unknown_auth_kind() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    with pytest.raises(ValidationError):
        McpServerCreate(
            server_id="sample", url="https://x/", transport="sse",
            auth_kind="oauth2",
        )


def test_create_request_refuses_raw_secret_in_auth_value_ref() -> None:
    """Anything not matching `(env|secret)://` is refused. Defends
    against operators pasting plaintext secrets into the form."""
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    with pytest.raises(ValidationError):
        McpServerCreate(
            server_id="sample", url="https://x/", transport="sse",
            auth_kind="bearer", auth_value_ref="sk-abc123",
        )


def test_create_request_accepts_env_and_secret_refs() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    for ref in ("env://MCP_FS_TOKEN", "secret://kv/mcp/filesystem"):
        req = McpServerCreate(
            server_id="sample", url="https://x/", transport="sse",
            auth_kind="bearer", auth_value_ref=ref,
        )
        assert req.auth_value_ref == ref


def test_create_request_auth_consistency_none_requires_no_creds() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    # OK case.
    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="none",
    )
    req._check_auth_consistency()  # does not raise

    # Bad: auth_kind=none but ref set.
    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="none",
        auth_value_ref="env://X",
    )
    with pytest.raises(ValueError, match="must be null"):
        req._check_auth_consistency()


def test_create_request_auth_consistency_bearer_requires_ref() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="bearer",
    )
    with pytest.raises(ValueError, match="auth_value_ref is required"):
        req._check_auth_consistency()

    # header_name must NOT be set with bearer.
    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="bearer",
        auth_value_ref="env://X", auth_header_name="X-API",
    )
    with pytest.raises(ValueError, match="header_name must be null"):
        req._check_auth_consistency()


def test_create_request_auth_consistency_header_requires_both() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api.admin import McpServerCreate

    # Missing ref.
    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="header",
        auth_header_name="X-API-Key",
    )
    with pytest.raises(ValueError, match="auth_value_ref is required"):
        req._check_auth_consistency()

    # Missing header_name.
    req = McpServerCreate(
        server_id="sample", url="https://x/", transport="sse", auth_kind="header",
        auth_value_ref="env://X",
    )
    with pytest.raises(ValueError, match="header_name is required"):
        req._check_auth_consistency()


def test_create_request_groups_must_match_grammar() -> None:
    pytest.importorskip("fastapi")
    from pydantic import ValidationError

    from bp_router.api.admin import McpServerCreate

    # Uppercase rejected by GROUP_NAME_PATTERN.
    with pytest.raises(ValidationError):
        McpServerCreate(
            server_id="sample", url="https://x/", transport="sse",
            groups=["BadGroup"],
        )


# ===========================================================================
# Endpoint wiring
# ===========================================================================


def test_admin_endpoints_registered_on_router() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    paths = {route.path for route in admin.router.routes if hasattr(route, "path")}
    assert "/mcp-servers" in paths
    assert "/mcp-servers/{server_id}" in paths
    assert "/mcp-servers/{server_id}/refresh-tools" in paths


def test_create_endpoint_audits_creation() -> None:
    """The audit trail is the operator's only history of MCP-server
    config changes — pin that creation events are written."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.create_mcp_server)
    assert 'event="mcp_server.created"' in src
    assert '"transport"' in src
    assert '"auth_kind"' in src


def test_update_endpoint_runs_cross_field_consistency_before_write() -> None:
    """PATCH must merge req + existing row, then run the auth
    consistency check, BEFORE issuing the UPDATE. Without this, a
    PATCH that changes auth_kind without updating ref/header lands
    a row that fails the DB CHECK."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.update_mcp_server)
    assert "_check_auth_consistency()" in src
    # Synthetic merged record built from existing + req.
    assert "synthetic = McpServerCreate(" in src


def test_delete_endpoint_404s_when_missing() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.delete_mcp_server)
    assert "if existing is None:" in src
    assert "HTTPException(404" in src


def test_refresh_tools_endpoint_stamps_timestamp() -> None:
    """The endpoint doesn't talk to the MCP server directly — it
    flips a timestamp the bridge picks up on poll."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    src = inspect.getsource(admin.refresh_mcp_server_tools)
    assert "mark_mcp_server_refresh_requested" in src
    assert "refresh_requested" in src


# ===========================================================================
# Admin UI — wiring + handler shape
# ===========================================================================


def test_admin_app_mounts_mcp_servers_router() -> None:
    pytest.importorskip("fastapi")
    from bp_admin import app as app_module

    src = inspect.getsource(app_module)
    # Imported from the pages package.
    pages_import = src.split("from bp_admin.pages import", 1)[1].split(")", 1)[0]
    assert "mcp_servers," in pages_import
    # Mounted under /mcp-servers.
    assert 'mcp_servers.router, prefix="/mcp-servers"' in src


def test_admin_nav_has_mcp_servers_section() -> None:
    """Without the nav entry the page is reachable by URL but
    invisible from the sidebar — same pin as Phase 9a."""
    base = (
        Path(__file__).parent.parent
        / "bp_admin" / "templates" / "base.html"
    ).read_text()
    assert '("mcp_servers"' in base
    assert "/admin/mcp-servers" in base


def test_admin_routes_registered_at_boot() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr

    from bp_admin.app import create_app
    from bp_admin.config import AdminConfig
    from bp_admin.upstream import UpstreamClient

    cfg = AdminConfig(
        router_url="http://127.0.0.1:0",
        session_secret=SecretStr("x" * 32),
    )
    app = create_app(cfg)
    app.state.upstream = UpstreamClient(
        cfg.router_url, timeout_s=cfg.upstream_timeout_s
    )
    # fastapi >=0.137 lazily wraps included routers in app.routes
    # (_IncludedRouter), so individual routes are no longer flattened there.
    # openapi()["paths"] reflects every registered path on both old and new
    # fastapi — the version-stable way to assert route registration.
    paths = set(app.openapi()["paths"])
    assert "/mcp-servers" in paths
    assert "/mcp-servers/new" in paths
    assert "/mcp-servers/{server_id}/edit" in paths
    assert "/mcp-servers/{server_id}/delete" in paths
    assert "/mcp-servers/{server_id}/refresh-tools" in paths


def test_parse_groups_helper_dedupes_and_preserves_order() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _parse_groups

    assert _parse_groups("a, b, c") == ["a", "b", "c"]
    assert _parse_groups("a, b, a, c") == ["a", "b", "c"]  # dedupe
    assert _parse_groups(" a , b , ") == ["a", "b"]
    assert _parse_groups("") == []


def test_parse_capabilities_helper_splits_comma_and_space() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _parse_capabilities

    assert _parse_capabilities("mcp.minimax, mcp.media") == ["mcp.minimax", "mcp.media"]
    assert _parse_capabilities("mcp.a mcp.b mcp.a") == ["mcp.a", "mcp.b"]  # dedupe
    assert _parse_capabilities("") == []


def test_parse_args_and_env_refs_helpers() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _parse_args, _parse_env_refs

    assert _parse_args("minimax-mcp\n-t\nstdio\n") == ["minimax-mcp", "-t", "stdio"]
    assert _parse_args("  \n\n") == []
    assert _parse_env_refs(
        "MINIMAX_API_KEY=env://MINIMAX_API_KEY\nHOST=secret://kv/host\nbad-line"
    ) == {"MINIMAX_API_KEY": "env://MINIMAX_API_KEY", "HOST": "secret://kv/host"}


def test_build_payload_shapes_stdio_vs_url() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _build_payload

    stdio = _build_payload(
        "minimax", "", "", "stdio", "none", "", "", "", "", True,
        "uvx", "minimax-mcp", "K=env://K",
    )
    assert stdio["url"] is None and stdio["command"] == "uvx"
    assert stdio["args"] == ["minimax-mcp"] and stdio["env_refs"] == {"K": "env://K"}

    url = _build_payload(
        "fs", "", "https://x/sse", "sse", "none", "", "", "", "", True,
        "uvx", "ignored", "K=env://K",  # command/args/env_refs nulled for url
    )
    assert url["url"] == "https://x/sse" and url["command"] is None
    assert url["args"] == [] and url["env_refs"] == {}


def test_tool_names_helper_reads_tools_cache() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _tool_names

    server = {"tools_cache": {"tools": [
        {"name": "text_to_audio"}, {"name": "play_audio"}, {"no_name": 1},
    ]}}
    assert _tool_names(server) == ["text_to_audio", "play_audio"]
    assert _tool_names({}) == []  # not yet connected


def test_tools_count_helper_handles_missing_cache() -> None:
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _tools_count

    assert _tools_count({}) is None
    assert _tools_count({"tools_cache": None}) is None
    assert _tools_count({"tools_cache": {}}) is None
    assert _tools_count({"tools_cache": {"tools": []}}) == 0
    assert _tools_count(
        {"tools_cache": {"tools": [{"name": "a"}, {"name": "b"}]}}
    ) == 2


def test_create_handler_sends_null_credentials_when_empty() -> None:
    """When auth_kind=none and the form fields are empty, the
    upstream POST must send `null` (not ""); otherwise the
    Pydantic regex validator on auth_value_ref refuses."""
    pytest.importorskip("fastapi")
    from bp_admin.pages.mcp_servers import _build_payload

    payload = _build_payload(
        server_id="x", description="", url="https://x/",
        transport="sse", auth_kind="none",
        auth_value_ref="", auth_header_name="",
        groups="", capabilities="", expose_to_llm=True,
    )
    assert payload["auth_value_ref"] is None
    assert payload["auth_header_name"] is None


# ===========================================================================
# Admin UI — templates
# ===========================================================================


def _list_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin" / "templates" / "mcp_servers" / "list.html"
    ).read_text()


def _form_html() -> str:
    return (
        Path(__file__).parent.parent
        / "bp_admin" / "templates" / "mcp_servers" / "form.html"
    ).read_text()


def test_list_template_columns_cover_key_state() -> None:
    body = _list_html()
    for col in ("Server ID", "Transport", "Auth", "LLM-callable", "Tools", "Last connected"):
        assert col in body, f"list template missing column {col!r}"


def test_list_template_renders_tools_not_yet_refreshed_state() -> None:
    """When tools_cache is null (Phase 10a — no bridge yet, OR
    new row pre-first-connect), the tools column shows a clear
    'not yet refreshed' note instead of '0'."""
    body = _list_html()
    assert "not yet refreshed" in body


def test_list_template_delete_uses_confirm() -> None:
    """Destructive action — must require a browser confirm() so
    misclicks don't tear down all derived agents."""
    body = _list_html()
    assert "onsubmit=\"return confirm" in body
    assert "tear down its agent" in body


def test_form_template_auth_fields_show_only_when_kind_not_none() -> None:
    """Alpine x-show on `authKind !== 'none'` collapses the
    credential fields when not needed. Without this, the form
    would render confusing inputs for the 'no auth' case."""
    body = _form_html()
    assert "authKind: " in body  # Alpine state init
    assert "authKind !== 'none'" in body


def test_form_template_header_name_only_shown_for_header_kind() -> None:
    """Bearer doesn't need a custom header name (it's implicit
    Authorization: Bearer). Only auth_kind=header surfaces the
    field."""
    body = _form_html()
    assert "authKind === 'header'" in body


def test_form_template_uses_url_safe_patterns_for_html5_validation() -> None:
    body = _form_html()
    # Server ID grammar.
    assert 'pattern="[a-z][a-z0-9_]+"' in body
    # URL scheme.
    assert 'pattern="https?://.+"' in body
    # Auth value ref scheme.
    assert 'pattern="(env|secret)://.+"' in body


def test_form_template_server_id_readonly_on_edit() -> None:
    """server_id is the PK and the basis for derived agent_ids —
    changing it would break the bridge's tool-agent identity. UI
    locks the field in edit mode."""
    body = _form_html()
    assert "{% if mode == \"edit\" %}readonly{% endif %}" in body


def test_form_template_expose_to_llm_checkbox_defaults_checked_for_new() -> None:
    """Empty form (mode=new) defaults expose_to_llm=True so most
    servers are LLM-callable by default."""
    body = _form_html()
    # The checkbox is checked when form.expose_to_llm is truthy;
    # the empty form helper returns True. Pin both ends:
    assert "{% if form.expose_to_llm %}checked{% endif %}" in body

    from bp_admin.pages.mcp_servers import _empty_form

    assert _empty_form()["expose_to_llm"] is True
