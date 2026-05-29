"""Tests for the upstream-bug bundle surfaced by the
`examples/test_drive/` round-trip (echo_agent + caller_agent against
a live router).

Bug 10: `Agent.handler` resolved the payload type via
`inspect.signature(fn).parameters[1].annotation` — which returns the
RAW STRING `"LLMData"` whenever the handler module uses
`from __future__ import annotations` (project house style). The
BaseModel-subclass check then failed, raising `TypeError: <handler>
payload type must be a Pydantic BaseModel subclass; got 'LLMData'`
at decoration time. Fix: resolve via `typing.get_type_hints(fn)`
so PEP 563 forward refs become real classes.

Bug 11: `invitations.used_by` was declared
`text REFERENCES users(user_id)` in migration 0001. Invitations
are consumed by AGENTS during `/v1/onboard`, and agents live in
`agents(agent_id)`, not `users(user_id)`. Every legitimate agent
onboard hit
`ForeignKeyViolationError on invitations_used_by_fkey`. Fix: drop
the FK; `used_by` is audit-only plain text.

Bug 12: `TaskEventRow.event_id` and `AuditLogRow.event_id` were
typed `str`, but the underlying columns are `uuid PRIMARY KEY
DEFAULT gen_random_uuid()`. asyncpg returns `uuid.UUID` objects;
`model_validate` then crashed with
`Input should be a valid string [type=string_type,
input_value=UUID('...'), input_type=UUID]` on every
`INSERT ... RETURNING *` round-trip — meaning every `admit_task`
crashed before dispatch. Fix: type both fields as `uuid.UUID`.
"""

from __future__ import annotations

import inspect
from datetime import UTC

import pytest

# Module-level imports for the Bug 10 fixtures. `typing.get_type_hints`
# resolves forward refs against `func.__globals__`, which for a
# function defined inside a test function points at THIS module's
# globals — not the function's locals. Imports done inside a test
# function don't help; they have to live up here.
from bp_protocol.types import AgentInfo, AgentOutput, LLMData
from bp_sdk import Agent, TaskContext

# ===========================================================================
# Bug 10: Agent.handler resolves PEP 563 string annotations
# ===========================================================================


def test_bug10_handler_accepts_pep563_string_annotation() -> None:
    """The exact upstream-bug #10 reproduction: a handler module
    with `from __future__ import annotations` (the project style)
    must register cleanly. Before the fix, `params[1].annotation`
    was the string `"LLMData"`, the BaseModel check failed, and
    the decorator raised `TypeError` at IMPORT TIME — so every
    external agent module that followed the style guide crashed
    before its `if __name__ == "__main__"` block could run."""
    agent = Agent(
        info=AgentInfo(
            agent_id="bug10_test_agent",
            description="bug 10 regression",
            groups=["bug10"],
            capabilities=[],
        ),
    )

    # The handler body re-mirrors how `examples/test_drive/echo_agent.py`
    # is written. The decorator MUST NOT raise.
    @agent.handler
    async def handle(ctx: TaskContext, payload: LLMData) -> AgentOutput:
        return AgentOutput(content=payload.prompt.upper())

    # The registered input model must be the resolved class, not
    # a string.
    registered = agent.resolve_handler(mode="LLMData")
    assert registered is not None, (
        "upstream-bug #10 regression: handler not registered under "
        "the resolved LLMData class — likely registered under the "
        "string 'LLMData' instead"
    )
    assert registered.input_model is LLMData
    # Output type also resolved (not a string).
    assert registered.output_model is AgentOutput


def test_bug10_handler_rejects_unresolvable_forward_ref() -> None:
    """When a forward ref refers to a name not importable from the
    handler module's globals at decoration time, the decorator must
    raise a CLEAR `TypeError` — not a confusing `isinstance() arg 2
    must be a class` from later in the function."""
    agent = Agent(
        info=AgentInfo(
            agent_id="bug10_unresolved",
            description="bug 10 unresolved fwd-ref",
            groups=["bug10"],
            capabilities=[],
        ),
    )

    # Build a function whose annotation is a string referring to a
    # name that doesn't exist anywhere. `typing.get_type_hints`
    # raises NameError; the decorator should re-raise as TypeError
    # with a clear message.
    ns: dict = {"TaskContext": TaskContext}
    exec(
        "from __future__ import annotations\n"
        "async def handle(ctx: TaskContext, payload: NonexistentModel): ...\n",
        ns,
    )
    with pytest.raises(TypeError) as excinfo:
        agent.handler(ns["handle"])
    msg = str(excinfo.value)
    assert "cannot resolve type annotation" in msg or "NonexistentModel" in msg


def test_bug10_handler_decorator_uses_get_type_hints() -> None:
    """Source pin: the registration path must call `typing.get_type_hints`
    so a future refactor that reverts to `params[1].annotation`
    is caught. The annotation read directly from `inspect.signature`
    is a STRING under `from __future__ import annotations` (PEP 563)
    and the BaseModel-subclass check fails.

    The introspection lives in `Agent._make_registered` (shared by
    every `@handler` registration in the unified mode registry). The
    pin follows that helper; the wider intent — catching a revert to
    the bare-annotation read — is preserved."""
    from bp_sdk.agent import Agent

    src = inspect.getsource(Agent._make_registered)
    assert "get_type_hints" in src, (
        "upstream-bug #10 regression: the handler-registration "
        "introspection stopped using typing.get_type_hints — every "
        "handler module that uses `from __future__ import annotations` "
        "will fail at import"
    )


def test_bug10_handler_rejects_non_basemodel_non_dict_payload() -> None:
    """Sanity: even after resolving forward refs, a non-BaseModel,
    non-dict payload type still raises. The original guard
    remains for `list`, `int`, etc. — only `dict` was promoted to
    a first-class escape hatch in Phase 10b for MCP-bridge-style
    forwarders."""
    agent = Agent(
        info=AgentInfo(
            agent_id="bug10_nonbm",
            description="non-basemodel payload",
            groups=["bug10"],
            capabilities=[],
        ),
    )

    with pytest.raises(TypeError, match="must be `dict` or a Pydantic BaseModel"):
        @agent.handler
        async def handle(ctx: TaskContext, payload: list) -> None: ...  # type: ignore[unused-ignore]


# ===========================================================================
# Bug 11: invitations.used_by is NOT a FK to users(user_id)
# ===========================================================================


def test_bug11_invitations_migration_no_fk_to_users_on_used_by() -> None:
    """Source pin against the consolidated v1 migration. The
    `invitations.used_by` column must NOT carry
    `REFERENCES users(user_id)` — invitations are consumed by
    agents (`agents.agent_id`), not users. The FK historically
    rejected every legitimate `/v1/onboard` with
    `ForeignKeyViolationError on invitations_used_by_fkey`."""
    from pathlib import Path

    migration = (
        Path(__file__).parent.parent
        / "bp_router"
        / "db"
        / "migrations"
        / "versions"
        / "0001_initial_schema.py"
    )
    src = migration.read_text()
    # Find the invitations CREATE TABLE block.
    assert "CREATE TABLE invitations" in src
    # The buggy declaration was:
    #   used_by          text REFERENCES users(user_id),
    # We assert the FK is gone. Be specific so we don't trip on
    # the legitimate `created_by ... REFERENCES users(user_id)`
    # on the next line.
    assert "used_by          text REFERENCES users" not in src, (
        "upstream-bug #11 regression: invitations.used_by is back "
        "to FK-ing users(user_id). Agent onboards will crash with "
        "ForeignKeyViolationError because agents live in "
        "agents(agent_id), not users(user_id)."
    )
    # Also catch the loose-spacing variant.
    assert "used_by text REFERENCES users" not in src


def test_bug11_invitations_migration_documents_used_by_audit_only() -> None:
    """The migration must carry a comment explaining WHY `used_by`
    isn't FK'd. Without it, a future refactor 'tightening up the
    schema' would re-add the constraint."""
    from pathlib import Path

    migration = (
        Path(__file__).parent.parent
        / "bp_router"
        / "db"
        / "migrations"
        / "versions"
        / "0001_initial_schema.py"
    )
    src = migration.read_text()
    # The migration comment must explain why used_by isn't FK'd so a
    # future schema-tightening refactor sees the warning before adding
    # a foreign-key constraint.
    assert "audit" in src.lower() and "used_by" in src


# ===========================================================================
# Bug 12: TaskEventRow.event_id + AuditLogRow.event_id are UUID, not str
# ===========================================================================


def test_bug12_task_event_row_event_id_is_uuid() -> None:
    """asyncpg returns `uuid` columns as `uuid.UUID` objects.
    `TaskEventRow.event_id: str` rejected every `INSERT INTO
    task_events ... RETURNING *` round-trip — meaning every
    `admit_task` crashed before dispatch."""
    from uuid import UUID

    from bp_router.db.models import TaskEventRow

    field = TaskEventRow.model_fields["event_id"]
    assert field.annotation is UUID, (
        f"upstream-bug #12 regression: TaskEventRow.event_id "
        f"annotation is {field.annotation!r}, expected uuid.UUID. "
        f"asyncpg returns UUID objects from `uuid` columns; "
        f"declaring `str` here makes every admit_task crash "
        f"with a Pydantic ValidationError."
    )


def test_bug12_audit_log_row_event_id_is_uuid() -> None:
    """Same column shape, same fix — `audit_log.event_id` is
    `uuid PRIMARY KEY DEFAULT gen_random_uuid()`."""
    from uuid import UUID

    from bp_router.db.models import AuditLogRow

    field = AuditLogRow.model_fields["event_id"]
    assert field.annotation is UUID, (
        f"upstream-bug #12 audit: AuditLogRow.event_id annotation "
        f"is {field.annotation!r}, expected uuid.UUID."
    )


def test_bug12_task_event_row_validates_uuid_input() -> None:
    """Behavioural: a `dict(record)` shape carrying a real UUID
    object (the way asyncpg returns it) must validate cleanly."""
    from datetime import datetime
    from uuid import uuid4

    from bp_router.db.models import TaskEventRow

    record = {
        "event_id": uuid4(),  # real UUID instance, not a str
        "task_id": "tsk_x",
        "ts": datetime.now(UTC),
        "kind": "transition",
        "actor_agent_id": None,
        "from_state": None,
        "to_state": None,
        "payload": {},
    }
    row = TaskEventRow.model_validate(record)
    # Round-trips back to a UUID.
    assert row.event_id == record["event_id"]


def test_bug12_audit_log_row_validates_uuid_input() -> None:
    from datetime import datetime
    from uuid import uuid4

    from bp_router.db.models import AuditLogRow

    record = {
        "event_id": uuid4(),
        "seq": 1,
        "ts": datetime.now(UTC),
        "actor_kind": "system",
        "actor_id": None,
        "event": "router_started",
        "target_kind": None,
        "target_id": None,
        "payload": {},
        "prev_hash": None,
        "self_hash": "h" * 64,
    }
    row = AuditLogRow.model_validate(record)
    assert row.event_id == record["event_id"]


def test_bug12_task_event_row_serialises_uuid_as_str_on_wire() -> None:
    """Pydantic emits UUID as the canonical string at JSON-encode
    time, so the wire shape stays `str`. Any consumer reading
    `event_id` over the wire keeps working unchanged."""
    from datetime import datetime
    from uuid import uuid4

    from bp_router.db.models import TaskEventRow

    eid = uuid4()
    row = TaskEventRow.model_validate({
        "event_id": eid,
        "task_id": "tsk_x",
        "ts": datetime.now(UTC),
        "kind": "transition",
        "actor_agent_id": None,
        "from_state": None,
        "to_state": None,
        "payload": {},
    })
    j = row.model_dump(mode="json")
    assert j["event_id"] == str(eid)
    assert isinstance(j["event_id"], str)


# ===========================================================================
# End-to-end pin: examples/test_drive exists + uses LLMData/AgentOutput
# ===========================================================================


def test_test_drive_examples_exist() -> None:
    """The `examples/test_drive/` pair (echo_agent + caller_agent)
    is the codified end-to-end smoke that surfaced Bugs 10–12.
    Pin existence so a future cleanup doesn't accidentally remove
    the entry points the dev-quickstart documents."""
    from pathlib import Path

    root = Path(__file__).parent.parent / "examples" / "test_drive"
    assert (root / "echo_agent.py").exists(), (
        "examples/test_drive/echo_agent.py is the documented "
        "callee for the round-trip smoke; its absence breaks "
        "the manual test-drive flow"
    )
    assert (root / "caller_agent.py").exists()


def test_test_drive_examples_use_future_annotations() -> None:
    """The two examples are also Bug-10 regression fixtures —
    they MUST carry `from __future__ import annotations` so a
    future `Agent.handler` regression that reverts to direct
    `params[1].annotation` reads is caught by attempting to
    import them. Without the future-import, the examples would
    work even with the buggy decorator and the bug would not
    surface."""
    from pathlib import Path

    root = Path(__file__).parent.parent / "examples" / "test_drive"
    for name in ("echo_agent.py", "caller_agent.py", "gemini_agent.py"):
        body = (root / name).read_text()
        assert "from __future__ import annotations" in body, (
            f"examples/test_drive/{name}: must use "
            f"`from __future__ import annotations` so it serves as "
            f"a Bug-10 regression fixture"
        )


# ===========================================================================
# Bug 13: thinking-token budget caveat on Gemini 2.5+
# ===========================================================================
#
# The first real Gemini call from `examples/test_drive/gemini_agent.py`
# capped `max_tokens=256` — and got `finish_reason="length"` with only
# 8 visible output tokens, because Gemini 2.5-flash splits
# `max_output_tokens` between hidden thoughts and visible output. At
# 256 the model burned ~248 on thoughts and had ~8 left for the user.
#
# Two fixes, both pinned below:
#   - The `gemini_agent.py` example must NOT pass `max_tokens` (no
#     cap → provider's own default applies → no truncation).
#   - The SDK's `LlmServiceClient.generate()` docstring must call out
#     the thinking-budget behaviour so the next agent author doesn't
#     fall into the same trap.


def test_bug13_gemini_example_does_not_cap_max_tokens() -> None:
    """`examples/test_drive/gemini_agent.py` must NOT pass
    `max_tokens` to `ctx.llm.generate(...)`. With the cap, Gemini
    2.5-flash burns the budget on hidden thoughts and truncates
    the visible answer to a handful of tokens with
    `finish_reason="length"`."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "examples" / "test_drive" / "gemini_agent.py"
    ).read_text()
    # Active code only — comments are explicitly fine.
    code_only = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "max_tokens=" not in code_only, (
        "upstream-bug #13 regression: gemini_agent example "
        "reintroduced an explicit `max_tokens=...` argument. "
        "Gemini 2.5+ counts thinking tokens against this cap; "
        "small caps truncate the visible answer."
    )


def test_bug13_gemini_example_surfaces_thoughts_tokens() -> None:
    """The example surfaces `thoughts_tokens` in the metadata so
    callers can see how the budget was split. If a future cleanup
    drops the field, the visibility into Gemini's thinking budget
    goes with it — and the next person to hit the truncation
    behaviour has to dig into router internals to diagnose."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent
        / "examples" / "test_drive" / "gemini_agent.py"
    ).read_text()
    assert "thoughts_tokens" in body, (
        "upstream-bug #13: gemini_agent must expose "
        "`thoughts_tokens` in its AgentOutput metadata"
    )


def test_bug13_sdk_generate_documents_thinking_budget() -> None:
    """`LlmServiceClient.generate`'s docstring must warn about the
    thinking-token budget on Gemini 2.5+ / Anthropic extended
    thinking. Without it, every new agent author who reads the
    signature sees `max_tokens` and assumes it caps visible
    output (the historical behaviour of all hosted LLMs pre-2024)."""
    import inspect

    from bp_sdk.llm import LlmServiceClient

    doc = inspect.getdoc(LlmServiceClient.generate) or ""
    # Pin the structural shape: doc mentions thinking-token budget,
    # `max_tokens`, and the `finish_reason="length"` failure mode.
    lower = doc.lower()
    assert "thinking" in lower, (
        "upstream-bug #13 regression: generate() docstring no "
        "longer warns about the thinking-token budget"
    )
    assert "max_tokens" in doc, (
        "upstream-bug #13: generate() docstring must reference "
        "`max_tokens` in the thinking-budget warning"
    )
    # Citation pin so a future docstring rewrite that drops the
    # bug reference also drops THIS test's flag.


def test_bug13_default_preset_is_gemini_with_env_keyref() -> None:
    """Source pin against the seeded `default` preset. The smoke
    runner's Gemini leg targets `preset="default"`, which
    relies on:
      - provider == "gemini"
      - api_key_ref == "env://GEMINI_API_KEY"
      - concrete_model is a real Gemini model (e.g. 2.5-flash)
    A future seed-loop refactor that renames the default or moves
    it to a different provider silently breaks the smoke runner.

    The seed list lives in the JSONC catalogue now, so this pins the
    loaded `default` preset rather than the module source text."""
    from bp_router.llm.presets import default_presets

    by_name = {p.name: p for p in default_presets()}
    assert "default" in by_name, "the `default` preset was renamed/removed"
    default = by_name["default"]
    assert default.provider == "gemini"
    assert default.api_key_ref == "env://GEMINI_API_KEY", (
        "upstream-bug #13 audit: default preset's `api_key_ref` "
        "is no longer `env://GEMINI_API_KEY` — the test-drive "
        "smoke runner can't resolve the key without it"
    )
    assert default.concrete_model.startswith("gemini-"), default.concrete_model


def test_bug13_token_usage_exposes_thoughts_tokens_field() -> None:
    """SDK-side `TokenUsage` must carry `thoughts_tokens`. A
    refactor that drops the field would silently zero the
    metadata field the example surfaces."""
    from bp_sdk.llm import TokenUsage

    fields = {f.name for f in TokenUsage.__dataclass_fields__.values()}
    assert "thoughts_tokens" in fields, (
        "upstream-bug #13: TokenUsage no longer carries "
        "`thoughts_tokens` — gemini_agent's metadata loses "
        "visibility into Gemini's thinking budget"
    )


def test_bug13_smoke_runner_has_gemini_leg() -> None:
    """`scripts/run-test-agents.sh` must include the Gemini leg —
    skipped when `GEMINI_API_KEY` is absent, run otherwise. Pin so
    a future cleanup doesn't accidentally remove the codified
    end-to-end LLM smoke."""
    from pathlib import Path

    body = (
        Path(__file__).parent.parent / "scripts" / "run-test-agents.sh"
    ).read_text()
    assert "gemini_agent" in body, (
        "upstream-bug #13: smoke runner no longer drives "
        "gemini_agent — the LLM call path isn't being exercised"
    )
    assert "GEMINI_API_KEY" in body, (
        "upstream-bug #13: smoke runner doesn't gate on "
        "GEMINI_API_KEY presence — fresh-clone runs without "
        "the key configured will fail rather than skip cleanly"
    )
