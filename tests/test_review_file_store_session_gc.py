"""Router-managed file store — Phase 3: session-close GC.

`docs/design/router-managed-file-store.md` §6. When a session
closes, its ephemeral file stash (`scope = session:{session_id}`) is
reclaimed: every `file_names` directory row under that scope is
deleted in the SAME transaction as the session close, so the close
and the stash teardown commit atomically. `persist/` rows are
user-wide and untouched. Underlying blobs are reclaimed by the
refcount sweep (not inline — an S3 delete storm must not ride the
close request path).

DB round-trip (real session + directory rows) runs in the
integration suite; here we pin the wiring + atomicity + scope.
"""

from __future__ import annotations

import inspect

import pytest


def test_close_session_gcs_session_scope_directory_rows() -> None:
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod._close_session)
    # Deletes the session-scoped directory rows...
    assert "delete_file_names_for_scope(" in src
    # ...keyed by the SESSION scope (not persist).
    assert 'f"session:{session_id}"' in src


def test_close_session_gc_is_in_the_close_transaction() -> None:
    """The GC must commit atomically with the session close — both
    inside the single `async with conn.transaction()` block, so a
    crash can't leave a closed session with a live stash (or an open
    session with a reaped stash)."""
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod._close_session)
    txn_idx = src.index("async with conn.transaction()")
    close_idx = src.index("scope.close_session(session_id)")
    gc_idx = src.index("delete_file_names_for_scope(")
    audit_idx = src.index("append_audit_event")
    # close → gc → audit, all after the transaction opens.
    assert txn_idx < close_idx < gc_idx < audit_idx


def test_close_session_does_not_delete_persist_scope() -> None:
    """Regression guard: the GC targets ONLY `session:{...}`. A
    stray `delete_file_names_for_scope("persist")` would wipe the
    user's persistent stash on every session close."""
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod._close_session)
    # GC keyed by the session scope only; no literal "persist" target.
    assert 'f"session:{session_id}"' in src
    assert 'delete_file_names_for_scope("persist")' not in src


def test_close_session_gc_count_in_audit_payload() -> None:
    """The reclaimed-row count rides the `session.closed` audit
    payload (operator visibility into stash teardown) — only when
    non-zero, to keep the common no-files close payload-free."""
    pytest.importorskip("fastapi")
    from bp_router.api import sessions as sessions_mod

    src = inspect.getsource(sessions_mod._close_session)
    assert '"file_names_gc": gc_count' in src
