"""Tests for operator-authored Python code agents.

Layered the way the feature's risk is:

  * **Structural** — migration shape, row model, select-cols coverage, admin
    wiring. Cheap, and they catch the "added a column, forgot the SELECT"
    class of bug that silently drops a field.
  * **Validators** — the safety surface: id/entrypoint grammar, typed
    parameters, and the rule that a secret must be a REFERENCE.
  * **The runner** — a real interpreter, really dropped and really killed.
    These are the ones that matter: the isolation claims in
    `docs/design/bridge-python-code-agents.md` §3.5 are only true if the
    subprocess behaves, and nothing about that can be asserted from a mock.

See `docs/design/bridge-python-code-agents.md`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# ===========================================================================
# Migration
# ===========================================================================


def _migration_body() -> str:
    """The consolidated baseline. `code_agents` shipped as its own migration
    and was later folded in; the table's SHAPE is what these tests pin, so
    they follow it to wherever it is declared rather than asserting a
    filename that consolidation is allowed to change."""
    return (
        Path(__file__).parent.parent
        / "bp_router" / "db" / "migrations" / "versions"
        / "0001_initial_schema.py"
    ).read_text()


def test_migration_creates_code_agents() -> None:
    body = _migration_body()
    assert "CREATE TABLE code_agents" in body
    assert "down_revision = None" in body


def test_migration_pins_id_and_entrypoint_grammar() -> None:
    body = _migration_body()
    assert "agent_id ~ '^code_[a-z][a-z0-9_]*$'" in body
    assert "entrypoint ~ '^[a-z_][a-z0-9_]*$'" in body


def test_migration_bounds_timeout_and_memory() -> None:
    body = _migration_body()
    assert "timeout_s BETWEEN 1 AND 300" in body
    assert "memory_mb BETWEEN 64 AND 4096" in body


def test_migration_has_invitation_columns() -> None:
    body = _migration_body()
    assert "pending_invitation_token" in body
    assert "pending_invitation_expires_at" in body


def test_migration_declares_no_network_column() -> None:
    """Per-agent egress control needs CAP_NET_ADMIN, which the bridge does
    not have. A column would read as a guarantee it cannot make (§3.4), so
    its ABSENCE is deliberate and worth pinning.

    Checked against the CREATE TABLE body only — the module docstring
    explains the omission and naturally mentions the word."""
    body = _migration_body()
    ddl = body[body.index("CREATE TABLE code_agents"):body.index("def downgrade")]
    assert "network" not in ddl


# ===========================================================================
# Row model + queries
# ===========================================================================


def test_code_agent_row_fields() -> None:
    from bp_router.db.models import CodeAgentRow

    expected = {
        "agent_id", "description", "code", "entrypoint", "parameters",
        "returns", "secret_refs", "timeout_s", "memory_mb", "groups",
        "capabilities", "expose_to_llm", "output_as_file", "enabled",
        "created_at", "updated_at", "created_by",
        "pending_invitation_token", "pending_invitation_expires_at",
    }
    assert expected <= set(CodeAgentRow.model_fields)


def test_query_helpers_exist() -> None:
    from bp_router.db import queries

    for name in (
        "list_code_agents", "get_code_agent", "insert_code_agent",
        "update_code_agent", "delete_code_agent",
        "record_code_agent_connected", "set_code_agent_pending_invitation",
    ):
        assert hasattr(queries, name), name


def test_select_cols_cover_every_model_field() -> None:
    """The "added a column, forgot the SELECT" guard: a field the model
    declares but the projection omits fails `model_validate` at runtime, in
    production, on the first read."""
    from bp_router.db import queries
    from bp_router.db.models import CodeAgentRow

    cols = {c.strip() for c in queries._CODE_AGENT_SELECT_COLS.split(",")}
    assert set(CodeAgentRow.model_fields) <= cols


# ===========================================================================
# Request-model validators (the safety surface)
# ===========================================================================


def _create(**over):
    from bp_router.api.admin import CodeAgentCreate

    base = {"agent_id": "code_demo", "code": "def run(p, c): return 1\n"}
    base.update(over)
    return CodeAgentCreate(**base)


def test_agent_id_requires_code_prefix() -> None:
    from pydantic import ValidationError

    _create(agent_id="code_demo")  # ok
    for bad in ("demo", "code_", "code_Bad", "custom_demo"):
        with pytest.raises(ValidationError):
            _create(agent_id=bad)


def test_entrypoint_grammar() -> None:
    from pydantic import ValidationError

    _create(entrypoint="handle_it")  # ok
    _create(entrypoint="_private")  # ok — a leading underscore is legal Python
    for bad in ("Run", "2run", "run-it", "run()"):
        with pytest.raises(ValidationError):
            _create(entrypoint=bad)


def test_parameters_are_typed_here_unlike_the_llm_kind() -> None:
    """The string-only rule on `custom_agents` exists for `$`-templating
    safety; this handler passes a dict, so types are real."""
    from pydantic import ValidationError

    created = _create(parameters=[
        {"name": "city", "type": "string"},
        {"name": "days", "type": "integer"},
        {"name": "metric", "type": "boolean"},
    ])
    assert [p.type for p in created.parameters] == ["string", "integer", "boolean"]
    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "x", "type": "date"}])


def test_param_name_grammar_and_uniqueness() -> None:
    from pydantic import ValidationError

    _create(parameters=[{"name": "topic"}])
    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "Topic"}])
    with pytest.raises(ValidationError):
        _create(parameters=[{"name": "x"}, {"name": "x"}])


def test_a_literal_secret_is_refused() -> None:
    """The whole point of `secret_refs`: a secret in a DB column is a secret
    in every backup, replica and admin-API response."""
    from pydantic import ValidationError

    _create(secret_refs={"API_KEY": "env://UPSTREAM_KEY"})  # ok
    with pytest.raises(ValidationError):
        _create(secret_refs={"API_KEY": "sk-live-abc123"})
    with pytest.raises(ValidationError):
        _create(secret_refs={"API_KEY": "env://"})  # empty ref
    with pytest.raises(ValidationError):
        _create(secret_refs={"lowercase": "env://X"})  # bad env name


def test_returns_must_be_a_json_schema() -> None:
    from pydantic import ValidationError

    _create(returns={"type": "object"})  # ok
    _create(returns=None)  # ok — no schema declared
    with pytest.raises(ValidationError):
        _create(returns={"type": "not-a-type"})


def test_timeout_and_memory_bounds() -> None:
    from pydantic import ValidationError

    _create(timeout_s=300, memory_mb=4096)  # ok at the ceiling
    for bad in ({"timeout_s": 0}, {"timeout_s": 301},
                {"memory_mb": 32}, {"memory_mb": 8192}):
        with pytest.raises(ValidationError):
            _create(**bad)


def test_capability_and_group_grammar() -> None:
    from pydantic import ValidationError

    _create(capabilities=["code.weather"], groups=["tools"])
    with pytest.raises(ValidationError):
        _create(capabilities=["nodot"])
    with pytest.raises(ValidationError):
        _create(groups=["Bad Group"])


def test_update_model_reuses_the_same_rules() -> None:
    from pydantic import ValidationError

    from bp_router.api.admin import CodeAgentUpdate

    CodeAgentUpdate(secret_refs={"K": "env://V"})
    CodeAgentUpdate(returns={})  # the CLEAR sentinel must survive validation
    with pytest.raises(ValidationError):
        CodeAgentUpdate(secret_refs={"K": "literal"})
    with pytest.raises(ValidationError):
        CodeAgentUpdate(timeout_s=999)


# ===========================================================================
# Admin view round-trip
# ===========================================================================


def test_row_to_view_round_trip() -> None:
    from datetime import UTC, datetime

    from bp_router.api.admin import _code_agent_row_to_view
    from bp_router.db.models import CodeAgentRow

    now = datetime.now(UTC)
    row = CodeAgentRow(
        agent_id="code_demo", description="d", code="def run(p,c): return 1\n",
        entrypoint="run",
        parameters=[{"name": "n", "type": "integer", "required": True}],
        returns={"type": "object"}, secret_refs={"K": "env://V"},
        timeout_s=45, memory_mb=256, groups=["g"], capabilities=["code.x"],
        expose_to_llm=True, output_as_file=True, enabled=True,
        created_at=now, updated_at=now, created_by="user_1",
        pending_invitation_token="tok", pending_invitation_expires_at=now,
    )
    view = _code_agent_row_to_view(row)
    assert view.agent_id == "code_demo"
    assert view.parameters[0]["type"] == "integer"
    assert view.secret_refs == {"K": "env://V"}
    assert view.timeout_s == 45
    assert view.pending_invitation_token == "tok"


def test_audit_records_a_code_hash_never_the_body() -> None:
    """An append-only hash chain containing operator code is an erasure
    problem (§10) — the code lives in the row, where it can be read, edited
    and deleted normally. Every endpoint that audits a code change must
    record the HASH."""
    from bp_router.api import admin

    for fn in (admin.create_code_agent, admin.update_code_agent,
               admin.delete_code_agent):
        src = inspect.getsource(fn)
        assert "_code_hash(" in src, fn.__name__
        # The body itself must never be handed to the audit payload. Every
        # mention of `.code` inside it has to be wrapped in `_code_hash(...)`
        # or measured as a length — never passed through raw.
        payload = src[src.index("append_audit_event"):]
        for wrapped in ("_code_hash(req.code)", "_code_hash(existing.code)",
                        'len(req.code.encode("utf-8"))'):
            payload = payload.replace(wrapped, "")
        assert ".code" not in payload, fn.__name__


# ===========================================================================
# Agent construction
# ===========================================================================


def _spec(tmp_path, **over):
    from bp_mcp_bridge.code_agent import CodeAgentSpec
    from bp_mcp_bridge.config import StdioPolicy

    base = dict(
        agent_id="code_demo",
        description="A demo",
        code="def run(params, context):\n    return 'hi'\n",
        parameters=[
            {"name": "city", "type": "string", "description": "where", "required": True},
            {"name": "days", "type": "integer", "description": "how long", "required": False},
        ],
        groups=["tools"],
        capabilities=["code.weather"],
        policy=StdioPolicy(work_root=tmp_path / "work"),
        state_dir=tmp_path / "state",
    )
    base.update(over)
    return CodeAgentSpec(**base)


def test_accepts_schema_carries_the_declared_types(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import MODE, _accepts_schema

    schema = _accepts_schema(_spec(tmp_path).parameters)[MODE]
    assert schema["properties"]["city"]["type"] == "string"
    assert schema["properties"]["days"]["type"] == "integer"
    # Only the required one is listed, and undeclared keys are refused.
    assert schema["required"] == ["city"]
    assert schema["additionalProperties"] is False


def test_schema_omits_required_entirely_when_empty(tmp_path) -> None:
    """An empty `required: []` is invalid to some validators; absent is
    unambiguous."""
    from bp_mcp_bridge.agent_common import object_schema

    schema = object_schema([{"name": "x", "required": False}])
    assert "required" not in schema


def test_build_code_agent_info(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import MODE, build_code_agent

    agent = build_code_agent(_spec(tmp_path, returns={"type": "object"}), "tok")
    info = agent.info
    assert info.agent_id == "code_demo"
    assert "code.agent" in info.capabilities  # the coarse marker
    assert "code.weather" in info.capabilities
    assert MODE in info.accepts_schema
    assert info.produces_schema == {"type": "object"}
    assert info.hidden is False


def test_single_mode_so_the_tool_name_stays_bare(tmp_path) -> None:
    """The SDK drops the mode label for a ONE-mode agent, so the model calls
    `call_code_<slug>` rather than `call_code_<slug>__main`. That property is
    "exactly one mode" — pin it, since adding a second would silently rename
    the tool for every existing caller."""
    from bp_mcp_bridge.code_agent import MODE, build_code_agent

    agent = build_code_agent(_spec(tmp_path), "tok")
    assert list(agent.info.accepts_schema) == [MODE]


def test_hidden_when_not_exposed(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import build_code_agent

    agent = build_code_agent(_spec(tmp_path, expose_to_llm=False), "tok")
    assert agent.info.hidden is True


# ===========================================================================
# The runner — a real interpreter, really dropped, really killed
# ===========================================================================


def _run_spec(tmp_path, code: str, **over):
    from bp_mcp_bridge.code_runner import CodeRunSpec
    from bp_mcp_bridge.config import StdioPolicy

    base = dict(
        agent_id="code_t", code=code, timeout_s=15,
        policy=StdioPolicy(work_root=tmp_path / "work"),
    )
    base.update(over)
    return CodeRunSpec(**base)


def _run(tmp_path, code: str, params=None, context=None, **over):
    from bp_mcp_bridge.code_runner import run_code

    return asyncio.run(
        run_code(_run_spec(tmp_path, code, **over), params or {}, context or {})
    )


def test_runner_returns_the_functions_value(tmp_path) -> None:
    out = _run(
        tmp_path,
        "def run(params, context):\n"
        "    return {'sum': params['a'] + params['b'], 'who': context['user_id']}\n",
        params={"a": 2, "b": 40}, context={"user_id": "usr_x"},
    )
    assert out == {"sum": 42, "who": "usr_x"}


def test_operator_print_does_not_corrupt_the_result(tmp_path) -> None:
    """The harness dups fd 1 and points sys.stdout at stderr before importing
    the module. Without that a stray `print()` interleaves with the result
    JSON — the day-one failure this design anticipated."""
    out = _run(
        tmp_path,
        "print('module-level noise')\n"
        "def run(params, context):\n"
        "    print('call-time noise')\n"
        "    return 'clean'\n",
    )
    assert out == "clean"


def test_a_raising_function_is_a_caller_error_with_its_message(tmp_path) -> None:
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError) as excinfo:
        _run(tmp_path, "def run(params, context):\n    raise ValueError('bad city')\n")
    assert excinfo.value.kind == "code"
    assert "ValueError" in str(excinfo.value)
    assert "bad city" in str(excinfo.value)


def test_traceback_is_not_returned_to_the_caller(tmp_path) -> None:
    """Operator locals may hold secrets; the traceback is logged, not
    returned."""
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError) as excinfo:
        _run(
            tmp_path,
            "def run(params, context):\n"
            "    secret_local = 'sk-live-DO-NOT-LEAK'\n"
            "    raise RuntimeError('boom')\n",
        )
    assert "sk-live-DO-NOT-LEAK" not in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


def test_timeout_kills_the_whole_process_group(tmp_path) -> None:
    """A bare kill(pid) would leave the grandchild running. `start_new_session`
    plus killpg is what makes the timeout an actual bound."""
    from bp_mcp_bridge.code_runner import CodeRunError

    marker = tmp_path / "grandchild-still-alive"
    code = (
        "import subprocess, sys, time\n"
        "def run(params, context):\n"
        f"    subprocess.Popen([sys.executable, '-c',\n"
        f"        \"import time; time.sleep(20); open({str(marker)!r}, 'w').write('x')\"])\n"
        "    time.sleep(20)\n"
    )
    with pytest.raises(CodeRunError, match="did not finish within"):
        _run(tmp_path, code, timeout_s=1)
    # Give a surviving grandchild ample time to write its marker.
    asyncio.run(asyncio.sleep(3))
    assert not marker.exists(), "the grandchild outlived the timeout"


def test_a_leaked_background_process_does_not_break_the_call(tmp_path) -> None:
    """A function that returns fine after a bare `Popen(...)` must get its
    result back.

    Regression: waiting on process EXIT rather than on the result line meant
    waiting for every holder of the stdout pipe to let go — so a leaked
    grandchild turned a 40ms success into a full-timeout failure with the
    result discarded. The runner reads the harness's single line instead.
    """
    import time

    started = time.monotonic()
    out = _run(
        tmp_path,
        "import subprocess, sys\n"
        "def run(params, context):\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'])\n"
        "    return 'returned fine'\n",
        timeout_s=8,
    )
    assert out == "returned fine"
    assert time.monotonic() - started < 5, "waited on the leaked child's pipe"


def test_a_leaked_background_process_is_reaped(tmp_path) -> None:
    """The process group is killed once the result is in hand, so a code
    agent cannot leave background work running on the bridge host."""
    marker = tmp_path / "leaked-child-ran-on"
    _run(
        tmp_path,
        "import subprocess, sys\n"
        "def run(params, context):\n"
        f"    subprocess.Popen([sys.executable, '-c',\n"
        f"        \"import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')\"])\n"
        "    return 'done'\n",
        timeout_s=8,
    )
    asyncio.run(asyncio.sleep(5))
    assert not marker.exists(), "the leaked background process outlived the call"


def test_the_child_cannot_see_the_bridge_service_secret(tmp_path) -> None:
    """THE invariant. The bridge's refresh token mints invitations for any
    bridged agent; the child's environment is constructed, not inherited, so
    it is absent by construction rather than by removal."""
    os.environ["BP_MCP_BRIDGE_SERVICE_SECRET"] = "SUPERSECRET-do-not-leak"
    os.environ["SOME_OTHER_DEPLOY_VAR"] = "also-private"
    try:
        seen = _run(
            tmp_path,
            "import os\n"
            "def run(params, context):\n"
            "    return {'keys': sorted(os.environ), 'values': sorted(os.environ.values())}\n",
            secrets={"MY_API_KEY": "k-123"},
        )
    finally:
        os.environ.pop("BP_MCP_BRIDGE_SERVICE_SECRET", None)
        os.environ.pop("SOME_OTHER_DEPLOY_VAR", None)
    assert "BP_MCP_BRIDGE_SERVICE_SECRET" not in seen["keys"]
    assert "SOME_OTHER_DEPLOY_VAR" not in seen["keys"]
    assert "SUPERSECRET-do-not-leak" not in seen["values"]
    # The agent's OWN declared secret is present — that is the feature.
    assert "MY_API_KEY" in seen["keys"]


def test_declared_context_reaches_the_function(tmp_path) -> None:
    out = _run(
        tmp_path,
        "import os\n"
        "def run(params, context):\n"
        "    return [context['task_id'], os.environ['BP_AGENT_ID']]\n",
        context={"task_id": "tsk_9", "user_id": "u", "session_id": "s",
                 "agent_id": "code_t"},
    )
    assert out == ["tsk_9", "code_t"]


def test_missing_entrypoint_is_a_clear_error(tmp_path) -> None:
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError, match="no callable"):
        _run(tmp_path, "def run(p, c): return 1\n", entrypoint="handler")


def test_a_module_that_fails_to_import_is_reported(tmp_path) -> None:
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError, match="failed to import"):
        _run(tmp_path, "this is not python\n")


def test_unserialisable_result_names_the_problem(tmp_path) -> None:
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError, match="not JSON"):
        _run(tmp_path, "def run(params, context):\n    return object()\n")


def test_oversize_result_names_the_fix(tmp_path) -> None:
    """Refused, not truncated: a truncated JSON body is worse than an error,
    because the caller cannot tell it happened."""
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError, match="output_as_file"):
        _run(tmp_path, "def run(params, context):\n    return 'x' * 2_000_000\n")


def test_memory_cap_is_enforced(tmp_path) -> None:
    from bp_mcp_bridge.code_runner import CodeRunError

    with pytest.raises(CodeRunError):
        _run(
            tmp_path,
            "def run(params, context):\n    return len(bytearray(400 * 1024 * 1024))\n",
            memory_mb=64,
        )


def test_the_workdir_is_reaped(tmp_path) -> None:
    """Per-call scratch, cleaned in a `finally` — including on failure, or a
    busy agent slowly fills the bridge's disk."""
    work = tmp_path / "work"
    _run(tmp_path, "def run(params, context):\n    return 1\n")
    with pytest.raises(Exception):
        _run(tmp_path, "def run(params, context):\n    raise RuntimeError('x')\n")
    assert list(work.rglob("call-*")) == []


def test_concurrent_calls_do_not_share_a_workdir(tmp_path) -> None:
    """Two calls to the SAME agent must not see each other's scratch files."""
    code = (
        "import os, time\n"
        "def run(params, context):\n"
        "    open(params['name'], 'w').write('x')\n"
        "    time.sleep(0.3)\n"
        "    return sorted(os.listdir('.'))\n"
    )

    async def _both():
        from bp_mcp_bridge.code_runner import run_code

        spec = _run_spec(tmp_path, code)
        return await asyncio.gather(
            run_code(spec, {"name": "a.txt"}, {}),
            run_code(spec, {"name": "b.txt"}, {}),
        )

    first, second = asyncio.run(_both())
    assert "a.txt" in first and "b.txt" not in first
    assert "b.txt" in second and "a.txt" not in second


def test_uid_is_deterministic_and_inside_the_policy_range() -> None:
    from bp_mcp_bridge.code_runner import agent_uid
    from bp_mcp_bridge.config import StdioPolicy

    pol = StdioPolicy(uid_base=30000, uid_max=39999)
    uid = agent_uid("code_demo", pol)
    assert uid == agent_uid("code_demo", pol), "must be stable across restarts"
    assert 30000 <= uid <= 39999
    # No range configured (rootless dev) → no drop.
    assert agent_uid("code_demo", StdioPolicy()) is None


# ===========================================================================
# Handler
# ===========================================================================


class _FakeFiles:
    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []

    async def write(self, name: str, text: str) -> str:
        self.written.append((name, text))
        return name


def _ctx(files=None):
    import logging

    return SimpleNamespace(
        task_id="tsk_1", user_id="usr_1", session_id="ses_1",
        log=logging.getLogger("test"), files=files,
    )


def test_handler_returns_a_string_verbatim(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import make_code_handler

    handler = make_code_handler(
        _spec(tmp_path, code="def run(params, context):\n    return 'plain text'\n")
    )
    out = asyncio.run(handler(_ctx(), {}))
    assert out.content == "plain text"


def test_handler_json_encodes_a_structured_return(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import make_code_handler

    handler = make_code_handler(
        _spec(tmp_path, code="def run(params, context):\n    return {'a': [1, 2]}\n")
    )
    out = asyncio.run(handler(_ctx(), {}))
    assert json.loads(out.content) == {"a": [1, 2]}


def test_handler_writes_a_file_when_output_as_file(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import make_code_handler

    files = _FakeFiles()
    handler = make_code_handler(
        _spec(
            tmp_path, output_as_file=True,
            code="def run(params, context):\n    return 'a big report'\n",
        )
    )
    out = asyncio.run(handler(_ctx(files), {}))
    assert files.written == [("output.txt", "a big report")]
    assert out.files == ["output.txt"]
    assert "output.txt" in out.content


def test_handler_reports_a_code_failure_as_a_caller_error(tmp_path) -> None:
    from bp_mcp_bridge.code_agent import make_code_handler
    from bp_sdk import InputValidationError

    handler = make_code_handler(
        _spec(tmp_path, code="def run(params, context):\n    raise ValueError('nope')\n")
    )
    with pytest.raises(InputValidationError, match="nope"):
        asyncio.run(handler(_ctx(), {}))


def test_handler_reports_a_bridge_fault_as_an_internal_error(tmp_path) -> None:
    """A spawn/chown failure is the BRIDGE's fault. Reporting it as a caller
    error sends the calling model off rewriting a payload that was fine."""
    from bp_mcp_bridge import code_agent
    from bp_mcp_bridge.code_runner import CodeRunError

    async def _boom(*_a, **_k):
        raise CodeRunError("could not start the code subprocess", kind="internal")

    from bp_sdk import InputValidationError

    handler = code_agent.make_code_handler(_spec(tmp_path))
    original = code_agent.run_code
    code_agent.run_code = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(handler(_ctx(), {}))
    finally:
        code_agent.run_code = original  # type: ignore[assignment]
    assert not isinstance(excinfo.value, InputValidationError)


# ===========================================================================
# Bridge row + supervisor wiring
# ===========================================================================


def _row(**over):
    from bp_mcp_bridge.code_agent_bridge import CodeAgentBridgeRow

    base = {
        "agent_id": "code_demo", "description": "d",
        "code": "def run(p, c): return 1\n", "entrypoint": "run",
        "parameters": [{"name": "x", "type": "string"}],
        "returns": None, "secret_refs": {"K": "env://V"},
        "timeout_s": 30, "memory_mb": 512,
        "groups": [], "capabilities": [], "expose_to_llm": True,
        "output_as_file": False, "enabled": True,
    }
    base.update(over)
    return CodeAgentBridgeRow.from_admin_dict(base)


def test_bridge_row_from_admin_dict() -> None:
    row = _row()
    assert row.agent_id == "code_demo"
    assert row.secret_refs == {"K": "env://V"}
    assert row.timeout_s == 30


def test_config_signature_excludes_the_invitation() -> None:
    """A freshly minted invitation must not restart a healthy bridge."""
    assert _row().config_signature() == _row(
        pending_invitation_token="tok"
    ).config_signature()


def test_config_signature_tracks_every_runtime_field() -> None:
    base = _row().config_signature()
    for change in (
        {"code": "def run(p, c): return 2\n"},
        {"entrypoint": "handler"},
        {"parameters": [{"name": "x", "type": "integer"}]},
        {"returns": {"type": "object"}},
        {"secret_refs": {"K": "env://OTHER"}},
        {"timeout_s": 60},
        {"memory_mb": 1024},
        {"description": "other"},
        {"expose_to_llm": False},
        {"output_as_file": True},
    ):
        assert _row(**change).config_signature() != base, change


def test_config_signature_hashes_the_code_rather_than_carrying_it() -> None:
    """The signature is compared on every poll; carrying a whole module body
    through that comparison is waste."""
    big = "x = 1\n" * 5000
    sig = _row(code=big).config_signature()
    assert not any(isinstance(part, str) and len(part) > 200 for part in sig)


def test_supervisor_reconciles_the_code_kind() -> None:
    from bp_mcp_bridge.agent_common import KIND_CODE
    from bp_mcp_bridge.supervisor import Supervisor

    sup = Supervisor(
        admin_client=SimpleNamespace(  # type: ignore[arg-type]
            list_custom_agents=None, list_code_agents=None,
        ),
        router_url="ws://r/v1/agent",
        state_dir=Path("/tmp/state"),  # noqa: S108
    )
    assert KIND_CODE in {k.name for k in sup._kinds()}
    assert "_reconcile_agents_once" in inspect.getsource(Supervisor.run)


def test_secret_refs_resolve_from_the_bridge_env(tmp_path) -> None:
    from bp_mcp_bridge.code_agent_bridge import CodeAgentBridge

    os.environ["BRIDGE_SIDE_KEY"] = "resolved-value"
    try:
        bridge = CodeAgentBridge(
            _row(secret_refs={"API_KEY": "env://BRIDGE_SIDE_KEY"}),
            admin_client=SimpleNamespace(),  # type: ignore[arg-type]
            router_url="ws://r/v1/agent",
            state_dir=tmp_path,
        )
        assert bridge._resolved_secrets() == {"API_KEY": "resolved-value"}
    finally:
        os.environ.pop("BRIDGE_SIDE_KEY", None)


def test_an_unresolvable_secret_does_not_stop_the_agent(tmp_path) -> None:
    """Skipped and logged by NAME, not fatal: the function then sees the
    variable missing and can say so, which beats an agent that never comes up
    because one of five refs has a typo."""
    from bp_mcp_bridge.code_agent_bridge import CodeAgentBridge

    bridge = CodeAgentBridge(
        _row(secret_refs={"A": "env://DEFINITELY_NOT_SET", "B": "not-a-ref"}),
        admin_client=SimpleNamespace(),  # type: ignore[arg-type]
        router_url="ws://r/v1/agent",
        state_dir=tmp_path,
    )
    assert bridge._resolved_secrets() == {}


# ===========================================================================
# Admin UI wiring
# ===========================================================================


def test_admin_page_and_nav_registered() -> None:
    from bp_admin import app as admin_app

    src = inspect.getsource(admin_app)
    assert "code_agents.router" in src
    assert '"/code-agents"' in src
    nav = (
        Path(__file__).parent.parent / "bp_admin" / "templates" / "base.html"
    ).read_text()
    assert "/admin/code-agents" in nav


def test_code_agent_templates_compile() -> None:
    from jinja2 import Environment, FileSystemLoader

    root = Path(__file__).parent.parent / "bp_admin" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)), autoescape=True)
    for name in ("code_agents/list.html", "code_agents/form.html"):
        env.get_template(name)


def test_parse_parameters_json_normalises_types() -> None:
    from bp_admin.pages.code_agents import _parse_parameters_json

    rows = _parse_parameters_json(json.dumps([
        {"name": "a", "type": "integer", "required": False},
        {"name": "b", "type": "bogus"},          # unknown type → string
        {"name": "", "type": "string"},          # blank name → dropped
        "not-a-dict",
    ]))
    assert rows == [
        {"name": "a", "type": "integer", "description": "", "required": False},
        {"name": "b", "type": "string", "description": "", "required": True},
    ]
    assert _parse_parameters_json("not json") == []


def test_parse_secret_refs_normalises_a_bare_name() -> None:
    """An operator typing `MY_VAR` means the env var, not a literal secret.
    Normalising keeps the router's refusal for things that look pasted."""
    from bp_admin.pages.code_agents import _parse_secret_refs_json

    refs = _parse_secret_refs_json(json.dumps([
        {"name": "A", "ref": "env://X"},
        {"name": "B", "ref": "PLAIN_VAR"},
        {"name": "C", "ref": ""},        # incomplete row → dropped
    ]))
    assert refs == {"A": "env://X", "B": "env://PLAIN_VAR"}


def test_form_echo_preserves_the_code_body() -> None:
    """A rejected create must not make the operator retype their function."""
    from bp_admin.pages.code_agents import _create_payload, _form_echo

    payload = _create_payload(
        "demo", "desc", "def run(p, c):\n    return 1\n", "run",
        "[]", "", "[]", "30", "512", "", "", True, False, True,
    )
    echoed = _form_echo(payload, "demo")
    assert echoed["code"] == "def run(p, c):\n    return 1\n"
    assert echoed["agent_id"] == "demo"


# ===========================================================================
# End-to-end: a real router, a real onboarded agent, a real subprocess
# ===========================================================================


async def _wait_connected(agent) -> None:  # noqa: ANN001
    for _ in range(100):
        await asyncio.sleep(0.05)
        if agent._dispatcher and agent._dispatcher.transport.is_connected:
            return
    raise AssertionError(f"{agent.info.agent_id} never connected")


def test_e2e_code_agent_round_trip(test_db_url: str, tmp_path) -> None:
    """The whole path: build the agent from a spec, onboard it to a live
    router, call it through `admit_task`, and get the subprocess's value back.

    This is what step 4 of the design's sequence asks for — the assumption the
    rest of the feature rests on is that a bridge-hosted agent can run a
    dropped subprocess and return its result through the normal task path.
    Unit tests cover the runner and the handler separately; only this one
    proves they compose under the router's admit + schema validation.
    """
    from pydantic import BaseModel

    from bp_mcp_bridge.code_agent import build_code_agent
    from bp_sdk.testing import TestRouter

    class _Payload(BaseModel):
        city: str
        days: int

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            spec = _spec(
                tmp_path,
                code=(
                    "def run(params, context):\n"
                    "    return {'forecast': params['city'].upper(),\n"
                    "            'days': params['days'] * 2,\n"
                    "            'called_by': context['user_id']}\n"
                ),
                returns={"type": "object"},
            )
            agent = build_code_agent(spec, "")
            token = await router.register_agent(agent.info)
            agent.config.router_url = router.ws_url
            agent.config.auth_token = token
            agent.config.embedded = False
            agent.config.state_dir = tmp_path / "state"

            run_task = asyncio.create_task(agent.run_async())
            try:
                await _wait_connected(agent)
                user = await router.create_user(level="tier0")
                session_id = await router.open_session(user_id=user.user_id)

                result = await router.call(
                    "code_demo", _Payload(city="lisbon", days=3),
                    user_id=user.user_id, session_id=session_id,
                    timeout_s=30.0,
                )
                assert result.status.value == "succeeded", result.error
                body = json.loads(result.output.content)
                assert body["forecast"] == "LISBON"
                assert body["days"] == 6
                assert body["called_by"] == user.user_id
            finally:
                await agent.aclose()
                await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(_drive())


def test_e2e_router_rejects_a_payload_the_schema_forbids(
    test_db_url: str, tmp_path
) -> None:
    """`additionalProperties: false` is not decoration: the router refuses an
    undeclared key at admit, so the function never sees it and the operator
    never has to defend against it."""
    from pydantic import BaseModel

    from bp_mcp_bridge.code_agent import build_code_agent
    from bp_sdk.testing import TestRouter

    class _Extra(BaseModel):
        city: str
        smuggled: str

    async def _drive() -> None:
        async with TestRouter(db_url=test_db_url) as router:
            spec = _spec(
                tmp_path,
                parameters=[{"name": "city", "type": "string", "required": True}],
                code="def run(params, context):\n    return sorted(params)\n",
            )
            agent = build_code_agent(spec, "")
            token = await router.register_agent(agent.info)
            agent.config.router_url = router.ws_url
            agent.config.auth_token = token
            agent.config.embedded = False
            agent.config.state_dir = tmp_path / "state2"

            run_task = asyncio.create_task(agent.run_async())
            try:
                await _wait_connected(agent)
                user = await router.create_user(level="tier0")
                session_id = await router.open_session(user_id=user.user_id)

                with pytest.raises(Exception) as excinfo:
                    await router.call(
                        "code_demo", _Extra(city="lisbon", smuggled="x"),
                        user_id=user.user_id, session_id=session_id,
                        timeout_s=15.0,
                    )
                assert "schema" in str(excinfo.value).lower()
            finally:
                await agent.aclose()
                await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(_drive())
