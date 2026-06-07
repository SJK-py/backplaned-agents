"""Pins for R-MEDIUM batch items #2, #3, #13.

  #2  SDK `ValidationError` / `PermissionError` shadowed pydantic's
      `ValidationError` and the builtin `PermissionError`. Renamed to
      `InputValidationError` / `PermissionDeniedError` (clean rename —
      pre-release, no compat shim per project principle P9).
  #3  `delegate(..., handoff_note=...)` was dead legacy surface (a
      router-ignored payload key). Fully removed.
  #13 The deadline sweep's `find_expired_tasks` had no supporting
      index. A partial index keyed on `deadline` was folded into the
      consolidated `0001` baseline (the history stays single-rooted —
      see test_migrations_consolidated).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# ===========================================================================
# #2 — SDK error rename (non-shadowing)
# ===========================================================================


def test_renamed_sdk_errors_present_and_mapped() -> None:
    pytest.importorskip("fastapi")
    import bp_sdk
    from bp_sdk import HandlerError, InputValidationError, PermissionDeniedError

    assert issubclass(InputValidationError, HandlerError)
    assert InputValidationError.status_code == 400
    assert issubclass(PermissionDeniedError, HandlerError)
    assert PermissionDeniedError.status_code == 403

    assert "InputValidationError" in bp_sdk.__all__
    assert "PermissionDeniedError" in bp_sdk.__all__


def test_old_shadowing_names_are_gone() -> None:
    """The whole point: the SDK no longer re-exports names that
    collide with pydantic's `ValidationError` / the builtin
    `PermissionError`."""
    pytest.importorskip("fastapi")
    import bp_sdk
    from bp_sdk import errors

    assert not hasattr(errors, "ValidationError")
    assert not hasattr(errors, "PermissionError")
    assert "ValidationError" not in bp_sdk.__all__
    assert "PermissionError" not in bp_sdk.__all__
    assert not hasattr(bp_sdk, "ValidationError")


def test_core_doc_uses_renamed_errors() -> None:
    doc = (
        Path(__file__).parent.parent / "docs" / "backplaned" / "sdk" / "core.md"
    ).read_text()
    assert "class InputValidationError(HandlerError): status_code = 400" in doc
    assert "class PermissionDeniedError(HandlerError): status_code = 403" in doc
    assert "class ValidationError(HandlerError)" not in doc
    assert "class PermissionError(HandlerError)" not in doc


# ===========================================================================
# #3 — handoff_note fully removed
# ===========================================================================


def test_delegate_signature_has_no_handoff_note() -> None:
    pytest.importorskip("fastapi")
    from bp_sdk.peers import PeerClient

    sig = inspect.signature(PeerClient.delegate)
    assert "handoff_note" not in sig.parameters
    src = inspect.getsource(PeerClient.delegate)
    assert "handoff_note" not in src


def test_handoff_note_absent_from_sdk_and_docs() -> None:
    """No residual `handoff_note` anywhere in the SDK peers module or
    the SDK core doc — it was a no-op legacy key, fully excised."""
    root = Path(__file__).parent.parent
    peers_src = (root / "bp_sdk" / "peers.py").read_text()
    assert "handoff_note" not in peers_src
    core_doc = (root / "docs" / "backplaned" / "sdk" / "core.md").read_text()
    assert "handoff_note" not in core_doc


# ===========================================================================
# #13 — deadline-sweep partial index folded into the 0001 baseline
# ===========================================================================


_V0001 = (
    Path(__file__).parent.parent
    / "bp_router" / "db" / "migrations" / "versions"
    / "0001_initial_schema.py"
)


def test_0001_declares_deadline_sweep_partial_index() -> None:
    body = _V0001.read_text()
    assert "CREATE INDEX tasks_deadline_sweep_idx ON tasks(deadline)" in body
    # Partial on exactly the slice the sweep scans.
    idx = body.index("tasks_deadline_sweep_idx")
    region = body[idx:idx + 400]
    assert "WHERE deadline IS NOT NULL" in region
    assert "state IN ('QUEUED','RUNNING','WAITING_CHILDREN')" in region


def test_index_predicate_covers_find_expired_tasks_query() -> None:
    """The index WHERE must cover the query's WHERE (else Postgres
    can't use the partial index). Pin both sides so a future change
    to one without the other is caught."""
    pytest.importorskip("asyncpg")
    from bp_router.db import queries

    qsrc = inspect.getsource(queries.find_expired_tasks)
    assert "deadline IS NOT NULL" in qsrc
    assert "deadline < $1" in qsrc
    assert "state IN ('QUEUED', 'RUNNING', 'WAITING_CHILDREN')" in qsrc
    assert "ORDER BY deadline ASC" in qsrc
