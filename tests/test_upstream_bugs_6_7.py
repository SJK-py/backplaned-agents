"""Tests for upstream Bugs 6 & 7 (post-Bug-5 boot regressions).

Bug 6: CSRF middleware had the same mount-prefix bug Bug 5 fixed
in the auth middleware — `EXEMPT_PATHS = {"/login"}` compared
against `request.url.path` directly, which is `/admin/login` in
mounted mode. Result: every POST /admin/login was rejected with
403 csrf_validation_failed and nobody could sign in.

The audit also caught two siblings the reviewer's report didn't
flag explicitly: `_safe_next` and `login_form`'s "already
authenticated → redirect home" both hard-coded `/admin/`.

Bug 7: no documented or supported path to create the FIRST admin
user. `POST /v1/admin/users` requires an existing admin
(chicken-and-egg). The fix adds optional
`ROUTER_BOOTSTRAP_ADMIN_EMAIL` + `ROUTER_BOOTSTRAP_ADMIN_PASSWORD`
env vars; when both are set, the lifespan creates the user on
first boot. Idempotent.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Bug 6: CSRF middleware mount-aware
# ===========================================================================


def _make_mounted_app():
    """Mounted-mode admin app fixture (mirrors
    `test_admin_mounted_smoke._make_mounted_app`)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from fastapi import FastAPI
    from pydantic import SecretStr

    from bp_admin.app import create_app
    from bp_admin.config import AdminConfig
    from bp_admin.upstream import UpstreamClient

    cfg = AdminConfig(
        router_url="http://127.0.0.1:0",
        session_secret=SecretStr("x" * 32),
    )
    admin_app = create_app(cfg)
    admin_app.state.upstream = UpstreamClient(
        cfg.router_url, timeout_s=cfg.upstream_timeout_s
    )
    parent = FastAPI()
    parent.mount("/admin", admin_app)
    return parent, admin_app


def test_bug6_post_login_under_mount_does_not_403_on_csrf() -> None:
    """The exact upstream-bug #6 reproduction. `POST /admin/login`
    must NOT be rejected with 403 csrf_validation_failed just
    because `/admin/login` doesn't match the unprefixed
    `EXEMPT_PATHS = {"/login"}`.

    Mock the upstream client so the test doesn't need a real
    router connection — we only care about the middleware-stack
    behaviour up to (and including) reaching the handler."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from bp_admin.upstream import UpstreamError

    parent, admin_app = _make_mounted_app()
    # Stub the upstream so we surface a 401 from the handler
    # (not a real connection error from the test ip:0).
    admin_app.state.upstream.login = AsyncMock(  # type: ignore[attr-defined]
        side_effect=UpstreamError(401, "invalid credentials")
    )

    with TestClient(parent) as client:
        # Establish a session cookie (needed for SessionMiddleware
        # to populate request.session before the auth middleware
        # touches it).
        client.get("/admin/login")
        r = client.post(
            "/admin/login",
            data={"email": "admin@local.test", "password": "wrong"},
            follow_redirects=False,
        )

    assert r.status_code != 403, (
        f"upstream-bug #6 regression: POST /admin/login returned 403. "
        f"If the response says 'csrf_validation_failed', the CSRF "
        f"middleware needs strip_root_path applied — same fix as "
        f"the auth middleware in Bug 5. Body excerpt: "
        f"{r.text[:300]!r}"
    )
    # Should reach the upstream-call path and surface a 401 from
    # the stubbed `UpstreamError`.
    assert r.status_code == 401


def test_bug6_csrf_middleware_uses_strip_root_path() -> None:
    """Source pin: the CSRF middleware MUST call `strip_root_path`
    before checking `EXEMPT_PATHS`. Pin so a future regression
    that reverts to `path = request.url.path` is caught."""
    pytest.importorskip("fastapi")
    from bp_admin import csrf as csrf_module

    src = inspect.getsource(csrf_module.make_csrf_middleware)
    assert "strip_root_path(request)" in src, (
        "upstream-bug #6 regression: CSRF middleware no longer "
        "strips the mount prefix before checking EXEMPT_PATHS"
    )


def test_bug6_strip_root_path_promoted_to_shared_module() -> None:
    """The reviewer's recommended structural fix: promote
    `_strip_root_path` from a private helper in `bp_admin/auth.py`
    to a shared `bp_admin/asgi_utils` module so every middleware
    + handler can use the same implementation. Pin so a future
    refactor doesn't reintroduce a private duplicate."""
    pytest.importorskip("fastapi")
    from bp_admin import asgi_utils

    assert hasattr(asgi_utils, "strip_root_path")
    assert hasattr(asgi_utils, "root_path")

    # Both auth.py and csrf.py must import from asgi_utils, NOT
    # carry a private duplicate.
    from bp_admin import auth as auth_module
    from bp_admin import csrf as csrf_module

    auth_src = inspect.getsource(auth_module)
    csrf_src = inspect.getsource(csrf_module)
    # Neither file should define its own _strip_root_path.
    assert "def _strip_root_path" not in auth_src, (
        "upstream-bug #6: auth.py reintroduced a private "
        "_strip_root_path duplicate; should import from asgi_utils"
    )
    assert "def _strip_root_path" not in csrf_src, (
        "upstream-bug #6: csrf.py defined a private "
        "_strip_root_path duplicate; should import from asgi_utils"
    )


# ---------------------------------------------------------------------------
# Bug 6 siblings: _safe_next + login_form redirect targets
# ---------------------------------------------------------------------------


def test_bug6_safe_next_uses_root_path_not_hardcoded_admin() -> None:
    """Source pin: `_safe_next` must build allowed-prefix and
    safe-default from `request.scope["root_path"]`, not a
    hard-coded `/admin/`. The hard-coded form broke standalone
    `bp-admin` deployments — open-redirect rejected even
    legitimate `/dashboard` redirects."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import auth_pages as auth_pages_module

    src = inspect.getsource(auth_pages_module._safe_next)
    # Must reference root_path from the scope.
    assert "root_path" in src
    # The hard-coded `/admin/` literal must NOT appear in active
    # code (comments are fine).
    code_only = "\n".join(
        line for line in src.split("\n")
        if not line.lstrip().startswith("#")
    )
    assert '"/admin/"' not in code_only, (
        "upstream-bug #6 audit: _safe_next still hard-codes "
        "/admin/ — breaks standalone bp-admin deployment"
    )


def test_bug6_safe_next_standalone_mode() -> None:
    """Behavioural: in standalone mode (`root_path=""`),
    `_safe_next` must default to `/` and accept relative paths."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.pages.auth_pages import _safe_next

    fake_request = SimpleNamespace(scope={"root_path": ""})
    # Empty raw → safe default = "/" in standalone mode.
    assert _safe_next(fake_request, None) == "/"
    assert _safe_next(fake_request, "") == "/"
    # Absolute URL → rejected.
    assert _safe_next(fake_request, "https://evil.example/") == "/"
    # Login self-loop → rejected.
    assert _safe_next(fake_request, "/login") == "/"
    # Legitimate relative path under standalone root → accepted.
    assert _safe_next(fake_request, "/dashboard") == "/dashboard"


def test_bug6_safe_next_mounted_mode() -> None:
    """Behavioural: under `/admin` mount, the safe default and
    accepted prefix are both `/admin/`-rooted."""
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from bp_admin.pages.auth_pages import _safe_next

    fake_request = SimpleNamespace(scope={"root_path": "/admin"})
    assert _safe_next(fake_request, None) == "/admin/"
    assert _safe_next(fake_request, "/admin/dashboard") == "/admin/dashboard"
    # Login self-loop under mount → rejected.
    assert _safe_next(fake_request, "/admin/login") == "/admin/"
    # Outside the mount → rejected (open-redirect protection).
    assert _safe_next(fake_request, "/other/path") == "/admin/"
    # Absolute URL → rejected.
    assert _safe_next(fake_request, "https://evil.example/admin/x") == "/admin/"


def test_bug6_login_form_redirects_authenticated_user_with_root_path() -> None:
    """Source pin: `login_form`'s already-authenticated redirect
    target uses `_root_path(request)`, not a hard-coded
    `/admin/`."""
    pytest.importorskip("fastapi")
    from bp_admin.pages import auth_pages as auth_pages_module

    src = inspect.getsource(auth_pages_module.login_form)
    # Reference to root_path must be present.
    assert "_root_path(request)" in src or "root_path(request)" in src
    # Active code must not hard-code /admin/.
    code_only = "\n".join(
        line for line in src.split("\n")
        if not line.lstrip().startswith("#")
    )
    assert 'url="/admin/"' not in code_only


# ---------------------------------------------------------------------------
# Path-audit pin — every middleware that compares request paths must
# strip the mount prefix.
# ---------------------------------------------------------------------------


def test_bug6_audit_all_path_comparisons_use_strip_root_path() -> None:
    """Audit every `bp_admin/*.py` for `request.url.path` reads
    that aren't immediately followed by a `strip_root_path` /
    `root_path` use. Catches a future middleware or handler
    that introduces a fresh mount-prefix assumption.

    Today the only legitimate non-stripped read is INSIDE
    `bp_admin.asgi_utils.strip_root_path` itself (where the
    helper consumes `request.url.path` to do the stripping).
    Everything else should be calling the helper."""
    from pathlib import Path

    bp_admin = Path(__file__).parent.parent / "bp_admin"
    offenders: list[tuple[str, int, str]] = []
    for py in bp_admin.rglob("*.py"):
        if py.name == "asgi_utils.py":
            # The helper itself reads request.url.path — that's
            # what it's there to read FROM.
            continue
        text = py.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # Skip comments / docstrings.
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if "request.url.path" in stripped:
                offenders.append((
                    str(py.relative_to(bp_admin)), line_no, stripped
                ))

    assert not offenders, (
        f"upstream-bug #6 audit: {len(offenders)} active-code site(s) "
        f"read `request.url.path` directly. Use "
        f"`bp_admin.asgi_utils.strip_root_path(request)` so the "
        f"comparison works in both standalone and mounted-under-prefix "
        f"deployments. Offenders:\n"
        + "\n".join(f"  {p}:{ln}  {s}" for p, ln, s in offenders)
    )


# ===========================================================================
# Bug 7: bootstrap admin env-var auto-creation
# ===========================================================================


def test_bug7_settings_expose_bootstrap_admin_fields() -> None:
    """`Settings` exposes `bootstrap_admin_email` and
    `bootstrap_admin_password` Optional fields. Both default
    to None — bootstrap is opt-in."""
    pytest.importorskip("pydantic_settings")
    from bp_router.settings import Settings

    fields = Settings.model_fields
    assert "bootstrap_admin_email" in fields
    assert "bootstrap_admin_password" in fields
    # Defaults: both None (opt-in).
    assert fields["bootstrap_admin_email"].default is None
    assert fields["bootstrap_admin_password"].default is None


def test_bug7_settings_pair_validator_rejects_half_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Cross-field validator rejects the half-set case: setting
    only the email, or only the password, is almost always a
    misconfigured deployment. Fail fast at startup rather than
    silently skipping the bootstrap step at lifespan time.

    `chdir(tmp_path)` isolates from the project-root `.env` —
    pydantic-settings auto-discovers it and would otherwise
    inherit `bootstrap_admin_password` from the dev scaffold's
    .env, defeating the half-set test."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    from pydantic import SecretStr

    from bp_router.settings import Settings

    base = dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
    )
    # Email without password → rejected by the pair validator.
    # Use a valid (non-special-use) domain so EmailStr accepts the
    # value and the cross-field "must be set together" check fires.
    with pytest.raises(Exception) as excinfo:
        Settings(  # type: ignore[arg-type]
            **base,
            bootstrap_admin_email="admin@example.com",
        )
    assert "ROUTER_BOOTSTRAP_ADMIN" in str(excinfo.value).upper() or (
        "together" in str(excinfo.value).lower()
    )
    # Password without email → also rejected.
    with pytest.raises(Exception):
        Settings(  # type: ignore[arg-type]
            **base,
            bootstrap_admin_password=SecretStr("x" * 16),
        )


def test_bug7_settings_both_unset_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Both unset is the default — no bootstrap, no error.

    Isolates from the project-root `.env` (which the dev-scaffold
    writes out and which pydantic-settings auto-discovers) by
    `chdir`-ing to a tmp directory. Without the chdir, this test
    would inherit `bootstrap_admin_email=admin@example.com` from
    `.env` and fail."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
    )
    assert cfg.bootstrap_admin_email is None
    assert cfg.bootstrap_admin_password is None


def test_bug7_settings_both_set_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Sanity-pin the happy path: both set → Settings accepts.

    Uses RFC-2606 `example.com`, NOT `.test` — the test-drive
    finding (Bug 8) showed `.test` is rejected by `EmailStr` as
    a special-use TLD. The bootstrap field uses the same
    validator as the auth login endpoint so a row that bootstraps
    can also log in.

    `chdir(tmp_path)` isolates from the project-root `.env`."""
    pytest.importorskip("pydantic_settings")
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    from pydantic import SecretStr

    from bp_router.settings import Settings

    cfg = Settings(  # type: ignore[arg-type]
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password=SecretStr("seed-password-1"),
    )
    assert cfg.bootstrap_admin_email == "admin@example.com"
    assert cfg.bootstrap_admin_password is not None


def test_bug8_bootstrap_admin_email_validates_as_emailstr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Test-drive finding (Bug 8): `bootstrap_admin_email` MUST
    use `EmailStr` validation, not plain `str`. Otherwise the
    bootstrap accepts emails the auth login endpoint
    (`LoginRequest.email: EmailStr`) later rejects, leaving an
    admin user in the DB who can never sign in.

    `email-validator` rejects RFC 6761 special-use TLDs (at
    minimum `.test` and `.invalid`). Pin so a future refactor
    that loosens the field type is caught.

    `chdir(tmp_path)` isolates from the project-root `.env` —
    otherwise `bootstrap_admin_email` would be inherited from
    the dev-scaffold value and the kwarg-with-bad-email test
    arm would never see the validation error."""
    pytest.importorskip("pydantic_settings")
    pytest.importorskip("email_validator")
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ROUTER_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    from pydantic import SecretStr

    from bp_router.settings import Settings

    base = dict(
        db_url="postgres://test/test",
        public_url="https://router.test",
        jwt_secret="x" * 64,
        serve_admin_ui=False,
        bootstrap_admin_password=SecretStr("p" * 16),
    )
    # Special-use TLDs that `email-validator` rejects. The
    # `.test` case is the one that bit the test drive — a
    # README example using `admin@local.test` would silently
    # bootstrap a row the auth login can't validate against.
    # `.invalid` is the other RFC 6761 special-use that
    # email-validator currently rejects. (The library
    # historically accepts `.example` and `.localhost`; we
    # don't pin those to avoid coupling to a specific
    # email-validator version.)
    for bad_email in ("admin@local.test", "admin@local.invalid"):
        with pytest.raises(Exception) as excinfo:
            Settings(  # type: ignore[arg-type]
                **base, bootstrap_admin_email=bad_email,
            )
        msg = str(excinfo.value).lower()
        assert "special-use" in msg or "reserved" in msg, (
            f"Bug 8 regression: {bad_email!r} accepted by Settings "
            f"but would be rejected by the auth login endpoint. "
            f"Validation error: {msg[:200]}"
        )

    # Plain bad-syntax email — should also fail.
    with pytest.raises(Exception):
        Settings(  # type: ignore[arg-type]
            **base, bootstrap_admin_email="not-an-email",
        )

    # RFC-2606 example.com is the recommended dev fixture.
    cfg = Settings(  # type: ignore[arg-type]
        **base, bootstrap_admin_email="admin@example.com",
    )
    assert cfg.bootstrap_admin_email == "admin@example.com"


def test_dev_scaffold_script_exists_and_is_executable() -> None:
    """`scripts/dev-up.sh` is the codified end-to-end dev-stack
    bring-up — Postgres+Redis, .env generation, alembic upgrade.
    Pin its existence + executable bit so a future cleanup
    doesn't accidentally remove the entry point that
    `DEVELOPMENT.md` documents."""
    import os
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "dev-up.sh"
    assert script.exists(), (
        "scripts/dev-up.sh is the documented dev-stack bring-up; "
        "its absence breaks the README quickstart"
    )
    assert os.access(script, os.X_OK), (
        "scripts/dev-up.sh is not executable; "
        "`scripts/dev-up.sh` won't work without `bash` prefix"
    )
    body = script.read_text()
    # Pin the steps so a refactor that drops one is caught.
    assert "alembic upgrade head" in body
    assert "openssl rand" in body
    assert "BOOTSTRAP_ADMIN" in body
    assert "example.com" in body, (
        "dev-up.sh should default to admin@example.com per the "
        "Bug 8 EmailStr validation note"
    )


def test_development_md_exists() -> None:
    """`DEVELOPMENT.md` documents the contributor-facing dev
    quickstart, smoke-test commands, and known footguns.
    Pin so it can't silently drift away."""
    from pathlib import Path

    doc = Path(__file__).parent.parent / "DEVELOPMENT.md"
    assert doc.exists()
    body = doc.read_text()
    # Pin a few of the headline sections so a future "shorten
    # the doc" pass doesn't lose the test-drive findings.
    assert "Quick start" in body
    assert "scripts/dev-up.sh" in body
    assert "Known dev-mode footguns" in body


def test_bug9_env_example_documents_admin_session_secret_and_bootstrap() -> None:
    """Test-drive finding (Bug 9): `.env.example` must list every
    env var the dev quickstart needs. A fresh `cp .env.example
    .env` should yield a misconfigured-but-discoverable starting
    point — operators have to see ROUTER_ADMIN_SESSION_SECRET and
    ROUTER_BOOTSTRAP_ADMIN_* mentioned somewhere or they don't
    know they exist."""
    from pathlib import Path

    env_example = (Path(__file__).parent.parent / ".env.example").read_text()
    for var in (
        "ROUTER_DB_URL",
        "ROUTER_JWT_SECRET",
        "ROUTER_ADMIN_SESSION_SECRET",
        "ROUTER_BOOTSTRAP_ADMIN_EMAIL",
        "ROUTER_BOOTSTRAP_ADMIN_PASSWORD",
    ):
        assert var in env_example, (
            f"Bug 9 regression: {var} not mentioned in .env.example"
        )


def test_bug7_lifespan_calls_bootstrap_helper() -> None:
    """Source pin: the lifespan invokes `_bootstrap_admin_user`
    after `_ensure_admin_console_agent`. Pin the ordering so a
    refactor that drops or moves the call fails immediately."""
    pytest.importorskip("fastapi")
    from bp_router import app as app_module

    src = inspect.getsource(app_module.lifespan)
    assert "_bootstrap_admin_user(state)" in src, (
        "upstream-bug #7 regression: lifespan no longer calls "
        "_bootstrap_admin_user — fresh deployments will boot "
        "without a way to create the first admin"
    )
    # Must come after _ensure_admin_console_agent so the synthetic
    # admin_console agent (which the bootstrap admin's tasks reference)
    # is in place first.
    bootstrap_idx = src.find("_bootstrap_admin_user")
    console_idx = src.find("_ensure_admin_console_agent")
    assert console_idx < bootstrap_idx, (
        "upstream-bug #7: _bootstrap_admin_user must run AFTER "
        "_ensure_admin_console_agent"
    )


def test_bug7_bootstrap_helper_skips_when_unset() -> None:
    """Behavioural: when both env vars are unset, the helper is
    a clean no-op — no DB call, no log line."""
    pytest.importorskip("fastapi")
    import asyncio

    from bp_router import app as app_module

    state = MagicMock()
    state.settings.bootstrap_admin_email = None
    state.settings.bootstrap_admin_password = None
    # If the helper tried to acquire a connection, this would
    # fail — None.acquire() raises AttributeError.
    state.db_pool = None

    asyncio.run(app_module._bootstrap_admin_user(state))  # must not raise


def test_bug7_bootstrap_helper_creates_user_when_absent() -> None:
    """Behavioural: with both env vars set AND no existing user
    with that email, the helper inserts a new admin row."""
    pytest.importorskip("fastapi")
    import asyncio

    from pydantic import SecretStr

    from bp_router import app as app_module

    state = MagicMock()
    state.settings.bootstrap_admin_email = "admin@local.test"
    state.settings.bootstrap_admin_password = SecretStr("super-secret")

    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool

    # Mock queries: get_user_by_email returns None; insert_user
    # returns a fake user row.
    fake_user = MagicMock(user_id="usr_test")
    from unittest.mock import patch

    import bp_router.db.queries as queries_module

    with patch.object(
        queries_module, "get_user_by_email", AsyncMock(return_value=None)
    ), patch.object(
        queries_module, "insert_user", AsyncMock(return_value=fake_user)
    ) as insert_mock:
        asyncio.run(app_module._bootstrap_admin_user(state))

    insert_mock.assert_awaited_once()
    kwargs = insert_mock.call_args.kwargs
    assert kwargs["email"] == "admin@local.test"
    assert kwargs["level"] == "admin"
    assert kwargs["auth_kind"] == "password"
    # The hash, not the plaintext, must be stored.
    assert "auth_secret_hash" in kwargs
    assert kwargs["auth_secret_hash"] != "super-secret"
    assert len(kwargs["auth_secret_hash"]) > 20


def test_bug7_bootstrap_helper_skips_when_user_exists() -> None:
    """Idempotency: when a user with the configured email already
    exists, the helper does NOT call `insert_user` again. Safe to
    leave the env vars set across restarts."""
    pytest.importorskip("fastapi")
    import asyncio

    from pydantic import SecretStr

    from bp_router import app as app_module

    state = MagicMock()
    state.settings.bootstrap_admin_email = "admin@local.test"
    state.settings.bootstrap_admin_password = SecretStr("password")

    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    state.db_pool = pool

    existing_user = MagicMock(user_id="usr_existing")
    from unittest.mock import patch

    import bp_router.db.queries as queries_module

    with patch.object(
        queries_module,
        "get_user_by_email",
        AsyncMock(return_value=existing_user),
    ), patch.object(
        queries_module, "insert_user", AsyncMock()
    ) as insert_mock:
        asyncio.run(app_module._bootstrap_admin_user(state))

    insert_mock.assert_not_awaited()
