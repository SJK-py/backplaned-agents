"""The jsonb/json asyncpg encoder strips NUL bytes.

Postgres jsonb (and text) cannot store a NUL byte (\\x00): a plain
`json.dumps` emits it as the escape `\\u0000`, and binding that to a jsonb
column fails with

    asyncpg.exceptions.UntranslatableCharacterError:
        unsupported Unicode escape sequence

NULs legitimately reach the router in task output — e.g. an agent doing a
direct `read_file` on a binary file (a .pdf decoded to a str) into
`AgentOutput.content`, which is persisted to `tasks.output` (jsonb). The
encoder registered on every connection (`_json_dumps_no_nul`) drops the escape
so the write succeeds instead of crashing `complete_task`.
"""

from __future__ import annotations

import json

from bp_router.db.connection import _json_dumps_no_nul


def test_strips_nul_from_string_value() -> None:
    out = _json_dumps_no_nul({"content": "Hello\x00World"})
    assert "\\u0000" not in out
    # Round-trips to clean text (NUL gone, rest intact).
    assert json.loads(out) == {"content": "HelloWorld"}


def test_strips_nul_nested_and_in_keys() -> None:
    payload = {"a": ["x\x00y", {"k\x00": "v\x00"}], "b": "plain"}
    out = _json_dumps_no_nul(payload)
    assert "\\u0000" not in out
    assert json.loads(out) == {"a": ["xy", {"k": "v"}], "b": "plain"}


def test_noop_when_no_nul_present() -> None:
    # Fast path: identical to json.dumps when there's nothing to strip.
    payload = {"content": "ordinary text", "n": 3, "nested": {"ok": True}}
    assert _json_dumps_no_nul(payload) == json.dumps(payload)


def test_preserves_other_unicode_escapes() -> None:
    # Only the NUL escape is removed; legitimate escapes survive.
    out = _json_dumps_no_nul({"emoji": "\U0001f600", "tab": "a\tb", "q": 'a"b'})
    assert json.loads(out) == {"emoji": "\U0001f600", "tab": "a\tb", "q": 'a"b'}


def test_registered_as_the_jsonb_encoder() -> None:
    # Source pin: the pool init wires _json_dumps_no_nul as the jsonb/json
    # encoder (not a bare json.dumps), so the strip applies to every write.
    import inspect

    from bp_router.db import connection

    src = inspect.getsource(connection.open_pool)
    assert "encoder=_json_dumps_no_nul" in src
    assert src.count("encoder=_json_dumps_no_nul") >= 2  # jsonb AND json
