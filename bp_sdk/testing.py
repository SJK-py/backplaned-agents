"""bp_sdk.testing — TestRouter harness for unit and integration tests.

Spins up an in-process bp_router against a Postgres test database and
exposes either an ASGI client (for fast in-process tests) or a real
WebSocket on a random port (for end-to-end agent tests).

Postgres is required because bp_router queries lean on jsonb,
recursive CTEs, GIN indexes, and `FOR UPDATE`. The harness expects
the schema to already exist — typically via Alembic upgrade head
inside a CI job — and truncates tables between tests rather than
re-running migrations.

Usage:

    async with TestRouter(db_url=os.environ["TEST_DB_URL"]) as router:
        user = await router.create_user(role="user")
        agent_id = await router.register_agent(...)
        result = await router.call(agent_id, MyInput(...), user_id=user.user_id)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.frames import NewTaskFrame, ResultFrame
from bp_protocol.types import AgentInfo, TaskPriority

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# TestRouter
# ---------------------------------------------------------------------------


class TestRouter:
    """Lightweight router harness for tests.

    Construction parameters:
      db_url:            Postgres DSN (must already have the schema applied).
      file_store_dir:    LocalFileStore root (defaults to a tmpdir).
      allow_all_acl:     If True, install a single allow-all ACL rule.
    """

    # pytest naming heuristic — this class is a fixture, not a test class.
    __test__ = False

    def __init__(
        self,
        *,
        db_url: str | None = None,
        file_store_dir: str | None = None,
        allow_all_acl: bool = True,
        port: int | None = None,
    ) -> None:
        self._db_url = db_url or os.environ.get("ROUTER_DB_URL") or os.environ.get(
            "TEST_DB_URL"
        )
        if not self._db_url:
            raise RuntimeError(
                "TestRouter requires db_url (or env ROUTER_DB_URL / TEST_DB_URL)"
            )
        self._file_store_dir = file_store_dir or "./.test_router_files"
        self._allow_all_acl = allow_all_acl
        self._port = port

        self._app: Any = None
        self._server: Any = None
        self._server_task: asyncio.Task | None = None
        self._public_url: str = ""
        # Snapshot of env vars `_start` mutates so `_stop` can
        # restore them. Stored as a dict
        # keyed on var name; missing keys (i.e. unset before the
        # context entered) round-trip via `_DEL_SENTINEL` so the
        # restore deletes them rather than reapplying a stale
        # value.
        self._env_snapshot: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TestRouter:
        await self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        # Configure the router via env so its Settings model picks them up.
        # Every mutation is snapshotted first so `_stop` can restore
        # the original value (or unset, if not present before).
        # Without this restore, a TestRouter context leaks
        # `ROUTER_DB_URL` and friends into the rest of the pytest
        # session, breaking adjacent tests that read those vars.
        self._env_set("ROUTER_DB_URL", self._db_url)
        self._env_setdefault("ROUTER_PUBLIC_URL", "http://localhost:0")
        self._env_setdefault("ROUTER_JWT_SECRET", secrets.token_urlsafe(32))
        self._env_setdefault("ROUTER_FILE_STORE", "local")
        self._env_setdefault(
            "ROUTER_FILE_STORE_OPTIONS",
            f'{{"path": "{self._file_store_dir}"}}',
        )
        self._env_setdefault("ROUTER_DEPLOYMENT_ENV", "dev")
        # The smoke test exercises the JSON API; disable the admin UI
        # mount so the test env doesn't need jinja2 / a session secret.
        self._env_setdefault("ROUTER_SERVE_ADMIN_UI", "false")

        from bp_router.app import create_app  # noqa: PLC0415

        self._app = create_app()

        # Start uvicorn programmatically so we can pick a port and shutdown cleanly.
        import uvicorn  # noqa: PLC0415

        port = self._port or _free_port()
        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server.config.load()
        self._server.lifespan = config.lifespan_class(config)
        self._server_task = asyncio.create_task(self._server.serve())

        # Wait until the server starts accepting connections.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if getattr(self._server, "started", False):
                break
        else:
            raise RuntimeError("uvicorn server failed to start")

        self._public_url = f"http://127.0.0.1:{port}"
        await self._reset_db()
        if self._allow_all_acl:
            await self._install_allow_all_rules()

    async def _stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=10.0)
            except TimeoutError:
                self._server_task.cancel()
        # Restore the env vars `_start` mutated. Vars that didn't
        # exist before the context entered are deleted; vars that
        # had a prior value are reset to it. Without this restore,
        # a TestRouter `async with` block leaks `ROUTER_DB_URL` and
        # friends into the rest of the pytest session.
        self._restore_env()

    # ------------------------------------------------------------------
    # Env-snapshot helpers
    # ------------------------------------------------------------------

    # Sentinel for "this var was unset before we mutated it" — the
    # restore path deletes the var rather than reapplying the
    # sentinel. Used because `None` is a valid `os.environ` lookup
    # default and we want to distinguish "missing" from "literal None".
    _ENV_UNSET = object()

    def _env_set(self, key: str, value: str) -> None:
        """Snapshot then set. Records the prior value (or unset
        sentinel) once; subsequent calls for the same key don't
        overwrite the snapshot."""
        if key not in self._env_snapshot:
            self._env_snapshot[key] = os.environ.get(key, self._ENV_UNSET)
        os.environ[key] = value

    def _env_setdefault(self, key: str, value: str) -> None:
        """Snapshot then `setdefault`. The snapshot still records the
        prior value (or unset) — even if the var was already set and
        we don't write to it, the snapshot guarantees `_restore_env`
        leaves the env identical to its pre-context state."""
        if key not in self._env_snapshot:
            self._env_snapshot[key] = os.environ.get(key, self._ENV_UNSET)
        os.environ.setdefault(key, value)

    def _restore_env(self) -> None:
        for key, prior in self._env_snapshot.items():
            if prior is self._ENV_UNSET:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior  # type: ignore[assignment]
        self._env_snapshot.clear()

    async def _reset_db(self) -> None:
        """Truncate per-test tables between tests; re-seed the bootstrap
        ACL rule so the router starts with a usable rule list. Schema
        is assumed already applied via Alembic."""
        pool = self._app.state.bp.db_pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                TRUNCATE TABLE
                    auth_refresh_tokens, invitations, audit_log,
                    acl_rules, files, task_events, tasks,
                    agents, sessions, users
                RESTART IDENTITY CASCADE
                """
            )
            await conn.execute(
                """
                INSERT INTO acl_rules
                    (rule_id, ord, name, description,
                     effect, user_level, caller_pattern, callee_pattern)
                VALUES
                    ('rule_test_bootstrap', 0, 'test-allow-all',
                     'TestRouter bootstrap rule.',
                     'allow', '*', '*/*', '*/*')
                """
            )

    async def _install_allow_all_rules(self) -> None:
        """Reload the in-memory RuleSet from the freshly-seeded acl_rules
        table so handshake catalogs admit everything during tests."""
        from bp_router.acl import Rule  # noqa: PLC0415
        from bp_router.db import queries  # noqa: PLC0415

        pool = self._app.state.bp.db_pool
        async with pool.acquire() as conn:
            rows = await queries.list_acl_rules(conn)
        self._app.state.bp.rules.replace([
            Rule(
                rule_id=r.rule_id,
                ord=r.ord,
                name=r.name,
                description=r.description,
                effect=r.effect,  # type: ignore[arg-type]
                user_level=r.user_level,
                caller_pattern=r.caller_pattern,
                callee_pattern=r.callee_pattern,
            )
            for r in rows
        ])

    # ------------------------------------------------------------------
    # Test fixtures
    # ------------------------------------------------------------------

    @property
    def public_url(self) -> str:
        return self._public_url

    @property
    def ws_url(self) -> str:
        return self._public_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        ) + "/v1/agent"

    async def create_user(
        self,
        *,
        user_id: str | None = None,
        level: str = "tier0",
        email: str | None = None,
    ):  # type: ignore[no-untyped-def]
        from bp_router.db import queries  # noqa: PLC0415

        pool = self._app.state.bp.db_pool
        async with pool.acquire() as conn:
            return await queries.insert_user(
                conn,
                user_id=user_id,
                email=email,
                level=level,
                auth_kind="api_key",
                auth_secret_hash=None,
            )

    async def register_agent(
        self,
        info: AgentInfo,
        *,
        kind: str = "external",
    ) -> str:
        """Register an agent directly (skipping the invitation flow) and
        return an auth_token usable in HelloFrame."""
        from bp_router.db import queries  # noqa: PLC0415
        from bp_router.security.jwt import issue_agent_token  # noqa: PLC0415

        pool = self._app.state.bp.db_pool
        async with pool.acquire() as conn:
            await queries.insert_agent(
                conn,
                agent_id=info.agent_id,
                kind=kind,
                capabilities=info.capabilities,
                groups=info.groups,
                agent_info=info.model_dump(),
            )

        settings = self._app.state.bp.settings
        token, _exp, _jti = issue_agent_token(
            agent_id=info.agent_id,
            secret=settings.jwt_secret.get_secret_value(),
            ttl_s=settings.agent_token_ttl_s,
            key_version=settings.jwt_key_version,
            protocol_version=PROTOCOL_VERSION,
            algorithm=settings.jwt_algorithm,
        )
        return token

    async def open_session(self, *, user_id: str) -> str:
        from bp_router.db import queries  # noqa: PLC0415

        pool = self._app.state.bp.db_pool
        async with pool.acquire() as conn:
            row = await queries.Scope.user(conn, user_id).open_session()
        return row.session_id

    async def call(
        self,
        agent_id: str,
        payload: BaseModel,
        *,
        user_id: str,
        session_id: str | None = None,
        timeout_s: float = 30.0,
    ) -> ResultFrame:
        """Inject a NewTask via admit_task as if from a synthetic
        'test_caller' agent and wait for the Result.

        The caller agent is created on-demand the first time it's used.
        """
        from bp_protocol.types import AgentInfo as AInfo  # noqa: PLC0415
        from bp_router.db import queries  # noqa: PLC0415
        from bp_router.tasks import admit_task  # noqa: PLC0415

        # Ensure synthetic caller exists.
        pool = self._app.state.bp.db_pool
        caller_id = "test_caller"
        async with pool.acquire() as conn:
            existing = await queries.get_agent(conn, caller_id)
            if existing is None:
                await queries.insert_agent(
                    conn,
                    agent_id=caller_id,
                    kind="external",
                    capabilities=[],
                    groups=["test"],
                    agent_info=AInfo(
                        agent_id=caller_id,
                        description="test caller",
                        groups=["test"],
                    ).model_dump(),
                )

        if session_id is None:
            session_id = await self.open_session(user_id=user_id)

        frame = NewTaskFrame(
            agent_id=caller_id,
            trace_id="0" * 32,
            span_id="0" * 16,
            destination_agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            priority=TaskPriority.NORMAL,
            payload=payload.model_dump(),
        )
        # R9 changed `admit_task` to return `AdmitResult`; `.task_id`
        # is the assigned id. Like the admin "Test Task" path, this
        # harness polls the `tasks` table for terminal state (not
        # the SDK pending_results path), so it ignores
        # `.replay_result` — the poll below observes the terminal
        # row directly regardless.
        task_id = (
            await admit_task(
                self._app.state.bp, frame, caller_agent_id=caller_id
            )
        ).task_id

        # Poll the tasks table until terminal.
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT state, status_code, output, error, parent_task_id
                    FROM tasks WHERE task_id = $1
                    """,
                    task_id,
                )
            if row is not None and row["state"] in (
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "TIMED_OUT",
            ):
                from bp_protocol.types import AgentOutput, TaskStatus  # noqa: PLC0415

                output = (
                    AgentOutput.model_validate(row["output"])
                    if row["output"]
                    else None
                )
                status_map = {
                    "SUCCEEDED": TaskStatus.SUCCEEDED,
                    "FAILED": TaskStatus.FAILED,
                    "CANCELLED": TaskStatus.CANCELLED,
                    "TIMED_OUT": TaskStatus.TIMED_OUT,
                }
                return ResultFrame(
                    agent_id="router",
                    trace_id="0" * 32,
                    span_id="0" * 16,
                    task_id=task_id,
                    parent_task_id=row["parent_task_id"],
                    status=status_map[row["state"]],
                    status_code=row["status_code"] or 0,
                    output=output,
                    error=row["error"],
                )
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"task {task_id} did not complete in {timeout_s}s")
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Capture helpers (skeleton)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def captured_logs() -> AsyncIterator[list[dict[str, Any]]]:
    """Capture structured log records emitted while the block runs."""
    records: list[dict[str, Any]] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            entry = {"level": record.levelname, "logger": record.name}
            entry.update({k: v for k, v in record.__dict__.items() if k.startswith("event") or k.startswith("bp.")})
            records.append(entry)

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)


@asynccontextmanager
async def captured_metrics() -> AsyncIterator[dict[str, float]]:
    """Snapshot Prometheus counter deltas during the block."""
    from bp_router.observability.metrics import REGISTRY  # noqa: PLC0415

    def snapshot() -> dict[str, float]:
        out: dict[str, float] = {}
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                out[f"{sample.name}::{sorted(sample.labels.items())}"] = sample.value
        return out

    before = snapshot()
    deltas: dict[str, float] = {}
    try:
        yield deltas
    finally:
        after = snapshot()
        for k, v in after.items():
            if v != before.get(k, 0):
                deltas[k] = v - before.get(k, 0)
