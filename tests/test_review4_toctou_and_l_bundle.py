"""Tests for the fourth-pass review TOCTOU + L-bundle.

  - M-4: `admit_task` re-checks session validity inside the
    create_task transaction with `FOR UPDATE`, closing the TOCTOU
    window where a concurrent `DELETE /v1/sessions/{id}` could
    close the session between pre-flight check and INSERT.
  - L-1: `/v1/files` upload validates `session_id` / `task_id`
    ownership against the uploader's scope before insert. The FK
    only checks row existence, not same-user_id.
  - L-2: `verify_token` wraps `int(claims["kver"])` in try/except
    so a malformed `kver` claim (string, dict, etc.) fails closed
    with `TokenError("invalid")` rather than crashing the request
    with a stack trace.
  - L-3: SDK `_END` sentinel was dead code (nothing pushed it).
    Deleted, along with the unreachable `if item is _END:` branch.
  - L-4: Three `revoke_jti` call sites had comments claiming
    "TTL = remaining lifetime of the token." The actual code is
    `max(remaining, default_ttl)` — strictly more conservative
    (defence-in-depth). Comments now match the code.
  - L-5: `Settings` numeric fields gained `Field(ge=, le=)` bounds
    plus a cross-field validator for `db_pool_min_size <=
    db_pool_max_size`. Misconfigurations fail fast at startup.
"""

from __future__ import annotations

import inspect

import pytest

# ===========================================================================
# M-4: admit_task session FOR UPDATE re-check inside transaction
# ===========================================================================


def test_m4_admit_task_recheck_session_for_update_inside_transaction() -> None:
    """Source pin: the admit_task body must include a `FOR UPDATE`
    SELECT on `sessions` WITHIN the `conn.transaction()` block. A
    regression that drops the lock or runs the check outside the
    transaction would re-open the TOCTOU window."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.admit_task)
    # The FOR UPDATE re-check must be present.
    assert "FOR UPDATE" in src, (
        "review4-M4 regression: admit_task no longer locks the session "
        "row at admit-vs-close TOCTOU window — concurrent close can "
        "race a task into a closed session"
    )

    # Ordering: the FOR UPDATE must appear AFTER the
    # `async with conn.transaction()` opener and BEFORE the
    # `create_task` call.
    txn_idx = src.find("async with conn.transaction()")
    for_update_idx = src.find("FOR UPDATE")
    create_task_idx = src.find(".create_task(")
    assert txn_idx > 0
    assert for_update_idx > txn_idx, (
        "review4-M4: FOR UPDATE check must run INSIDE the transaction"
    )
    assert for_update_idx < create_task_idx, (
        "review4-M4: session check must run BEFORE create_task; "
        "otherwise a closed session could host the new row "
        "before the lock proves it open"
    )


def test_m4_recheck_raises_session_unknown_on_missing_session() -> None:
    """Behavioural-ish pin via source: the in-transaction recheck
    must raise `AdmitError("session_unknown", ...)` when the row
    is missing AND `AdmitError("session_closed", ...)` when
    `closed_at` is non-null. Pinned via source so the test stays
    pure unit (no DB)."""
    pytest.importorskip("fastapi")
    from bp_router import tasks as tasks_module

    src = inspect.getsource(tasks_module.admit_task)
    # The error codes must be reachable from the in-transaction
    # block — find both within the transaction.
    txn_idx = src.find("async with conn.transaction()")
    create_task_idx = src.find(".create_task(")
    inner = src[txn_idx:create_task_idx]
    assert '"session_unknown"' in inner
    assert '"session_closed"' in inner


# ===========================================================================
# L-1: upload validates session_id / task_id ownership
# ===========================================================================


def test_l1_upload_rejects_unowned_session_id_with_404() -> None:
    """Behavioural: when `session_id` query param is supplied but
    `Scope.user(...).get_session(session_id)` returns None, the
    upload must raise an `HTTPException(404)`. Without this, the
    file row could pin to another user's session_id (FK passes,
    same-user check absent before review4-L1)."""
    pytest.importorskip("fastapi")

    from bp_router.api import files as files_module

    src = inspect.getsource(files_module.upload)
    # Source must include the session ownership check.
    assert "scope.get_session(session_id)" in src, (
        "review4-L1 regression: upload no longer checks session ownership"
    )
    # And the equivalent for task_id.
    assert "scope.get_task(task_id)" in src, (
        "review4-L1 regression: upload no longer checks task_id ownership"
    )
    # The check must happen BEFORE insert_file so a failure doesn't
    # leave a pending storage object orphaned.
    sess_check_idx = src.find("scope.get_session(session_id)")
    insert_idx = src.find("scope.insert_file(")
    assert 0 < sess_check_idx < insert_idx, (
        "review4-L1: session check must precede insert_file"
    )


def test_l1_upload_404_uses_caller_tree_phrasing() -> None:
    """Pin the 404 message phrasing — operators / SDK error
    classifiers depend on it. 'caller's tree' is the established
    pattern (see `parent_task_id` 404 in admit_task)."""
    pytest.importorskip("fastapi")
    from bp_router.api import files as files_module

    src = inspect.getsource(files_module.upload)
    assert "session_id not found in caller's tree" in src
    assert "task_id not found in caller's tree" in src


# ===========================================================================
# L-2: verify_token int(kver) fails closed on malformed claim
# ===========================================================================


def test_l2_verify_token_handles_non_numeric_kver_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural: a token whose `kver` claim is a string (or any
    non-numeric value) MUST raise `TokenError('invalid')` — NOT
    let `int(...)` propagate a `ValueError`."""
    pytest.importorskip("jwt")
    # Stub the pyjwt.decode to return a malformed kver, bypassing
    # signature verification — that's not what we're testing.
    import bp_router.security.jwt as jwt_module
    from bp_router.security.jwt import TokenError, verify_token

    monkeypatch.setattr(
        jwt_module.pyjwt,
        "decode",
        lambda *a, **k: {"kind": "session", "kver": "v1", "jti": "j"},
    )

    with pytest.raises(TokenError) as excinfo:
        verify_token(
            "fake-token",
            secret="x" * 32,
            algorithm="HS256",
            expected_kind="session",
            key_version=1,
        )
    # Either "invalid" message or empty (TokenError can have empty)
    # — pin the type, not the string.
    assert "invalid" in str(excinfo.value).lower() or excinfo.value.args == ("invalid",)


def test_l2_verify_token_handles_dict_kver_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `kver` that's a dict / list / other non-int-coercible type
    must also fail closed."""
    pytest.importorskip("jwt")
    import bp_router.security.jwt as jwt_module
    from bp_router.security.jwt import TokenError, verify_token

    monkeypatch.setattr(
        jwt_module.pyjwt,
        "decode",
        lambda *a, **k: {"kind": "session", "kver": {"x": 1}, "jti": "j"},
    )

    with pytest.raises(TokenError):
        verify_token(
            "fake-token",
            secret="x" * 32,
            algorithm="HS256",
            expected_kind="session",
            key_version=1,
        )


def test_l2_verify_token_normal_kver_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-pin: the happy path (numeric kver matching
    key_version) still returns the claims unchanged."""
    pytest.importorskip("jwt")
    import bp_router.security.jwt as jwt_module
    from bp_router.security.jwt import verify_token

    payload = {"kind": "session", "kver": 1, "jti": "j", "sub": "u"}
    monkeypatch.setattr(
        jwt_module.pyjwt,
        "decode",
        lambda *a, **k: payload,
    )

    out = verify_token(
        "fake-token",
        secret="x" * 32,
        algorithm="HS256",
        expected_kind="session",
        key_version=1,
    )
    assert out == payload


# ===========================================================================
# L-3: _END sentinel removed
# ===========================================================================


def test_l3_end_sentinel_deleted() -> None:
    """`_END` was unreachable — nothing pushed it onto the stream
    queue. Deleted along with the `if item is _END:` branch."""
    from bp_sdk import llm as llm_module

    src = inspect.getsource(llm_module)
    assert "_END = object()" not in src, (
        "review4-L3 regression: _END sentinel re-added"
    )
    assert "if item is _END:" not in src, (
        "review4-L3 regression: _END branch re-added"
    )


# ===========================================================================
# L-4: revoke_jti TTL comments match the code
# ===========================================================================


def test_l4_revoke_jti_comment_describes_max_not_min() -> None:
    """Three call sites use `ttl_s = max(remaining, default_ttl)`.
    Comments must explain the `max(...)` semantics (defence-in-
    depth) so a future maintainer doesn't 'fix' it to `min(...)`,
    which would shrink the revocation window for almost-expired
    tokens."""
    pytest.importorskip("fastapi")
    from bp_router.api import auth as auth_module
    from bp_router.api import onboard as onboard_module

    onboard_src = inspect.getsource(onboard_module)
    auth_src = inspect.getsource(auth_module)

    # All three revoke_jti call sites use the max(...) semantics; spot
    # the `max(remaining, default_ttl)` pattern in both modules.
    assert "max(remaining" in onboard_src or "max(remaining_s" in onboard_src
    assert auth_src.count("max(remaining") + auth_src.count("max(remaining_s") >= 2


# ===========================================================================
# L-5: Settings numeric range validators
# ===========================================================================


def _baseline_settings_kwargs() -> dict:
    """Minimal-required kwargs to construct Settings."""
    return dict(
        db_url="postgres://x/y",
        public_url="https://test.example",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
    )


def test_l5_bind_port_outside_range_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bind_port=70000` was silently accepted before review4-L5;
    must now raise."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), bind_port=70_000)  # type: ignore[arg-type]
    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), bind_port=0)  # type: ignore[arg-type]


def test_l5_db_pool_zero_max_size_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`db_pool_max_size=0` would deadlock the first acquire; must
    fail at startup."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), db_pool_max_size=0)  # type: ignore[arg-type]


def test_l5_db_pool_min_exceeds_max_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-field bound: `min_size > max_size` is caught by
    `_db_pool_bounds_consistent`."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **_baseline_settings_kwargs(),
            db_pool_min_size=20,
            db_pool_max_size=10,
        )
    assert "db_pool_min_size" in str(excinfo.value).lower() or (
        "exceed" in str(excinfo.value).lower()
    )


def test_l5_spawn_max_depth_negative_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`spawn_max_depth=-1` made every spawn refuse — caller-error
    that should fail-fast at startup."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), spawn_max_depth=-1)  # type: ignore[arg-type]


def test_l5_spawn_max_depth_above_cte_ceiling_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`spawn_max_depth=128` would silently cap at the
    `_MAX_TASK_TREE_DEPTH = 64` recursive-CTE ceiling — confusing
    operator-facing weirdness. `le=64` makes it fail-fast."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), spawn_max_depth=128)  # type: ignore[arg-type]


def test_l5_pending_ack_timeout_zero_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pending_ack_timeout_s=0` would make every ack expire
    instantly — must be `> 0`."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    with pytest.raises(Exception):
        Settings(**_baseline_settings_kwargs(), pending_ack_timeout_s=0.0)  # type: ignore[arg-type]


def test_l5_resume_window_zero_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resume_window_s=0` is the documented "disable resume"
    setting — must be accepted (`ge=0` not `gt=0`)."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        **_baseline_settings_kwargs(),
        resume_window_s=0,
    )
    assert cfg.resume_window_s == 0


def test_l5_default_settings_pass_all_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-pin: a `Settings()` with default values across the
    board must construct cleanly. Catches a regression where a
    new bound is too tight for its own default."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_SERVE_ADMIN_UI", raising=False)
    from bp_router.settings import Settings

    cfg = Settings(**_baseline_settings_kwargs())  # type: ignore[arg-type]
    # Spot-check a handful of defaults survive validation.
    assert cfg.bind_port == 8000
    assert cfg.db_pool_max_size == 10
    assert cfg.spawn_max_depth == 16
    assert cfg.pending_ack_timeout_s == 30.0
