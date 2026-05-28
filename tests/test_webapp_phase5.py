"""Webapp Phase 5 — config + cron structured panes ([webapp.md] §5).

Config form writes via the SAME validation the config agent uses
(bp_agents.config_edit); cron pane add/remove reuse bp_agents.cron_manage.
Driven on one loop via httpx.ASGITransport (asyncpg is loop-bound).
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from bp_agents.config_edit import ConfigError, coerce_config_value
from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream:
    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt("usr_a"), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def aclose(self):
        pass


def _build_app(*, pool):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    cfg = WebappConfig(session_secret=SecretStr("x" * 32), session_cookie_secure=False)
    return create_app(cfg, upstream=_Upstream(), pool=pool, core=None)


async def _seed(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE cron_jobs, session_info, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await queries.create_user_config(
            conn, user_id="usr_a", full_name="Ada", timezone="UTC",
        )
        await queries.create_session_info(
            conn, session_id="ses_1", user_id="usr_a", channel="webapp",
        )


async def _login(client) -> None:
    await client.post("/login", data={"email": "a@b.c", "password": "x", "next": "/"})


async def _csrf(client, path: str) -> str:
    import re  # noqa: PLC0415

    m = re.search(r'name="csrf-token" content="([^"]+)"', (await client.get(path)).text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Shared validation agreement (unit)
# ---------------------------------------------------------------------------


def test_config_edit_coercion_and_rejection() -> None:
    assert coerce_config_value("verbose_default", "on") is True
    assert coerce_config_value("verbose_default", "false") is False
    assert coerce_config_value("max_context_token_limit", "8000") == 8000
    assert coerce_config_value("full_name", "Ada") == "Ada"
    with pytest.raises(ConfigError):
        coerce_config_value("max_context_token_limit", "not-an-int")
    with pytest.raises(ConfigError):
        coerce_config_value("preset_pro", "x")  # not user-editable


def test_config_agent_uses_shared_editable_fields() -> None:
    """The config agent must source its editable set + coercion from
    config_edit, so the NL path and the form can't drift."""
    import importlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from bp_agents.config_edit import EDITABLE_FIELDS  # noqa: PLC0415

    # importlib returns the real module (the package __init__ rebinds the
    # `agent` attribute to the Agent instance, shadowing the submodule).
    src = inspect.getsource(importlib.import_module("bp_agents.agents.config.agent"))
    assert "coerce_config_value" in src and "EDITABLE_FIELDS" in src
    assert "max_context_token_limit" in EDITABLE_FIELDS


# ---------------------------------------------------------------------------
# Config pane
# ---------------------------------------------------------------------------


def test_config_view_prefills_current_values(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/config")
            assert r.status_code == 200, r.text[:300]
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert 'value="Ada"' in html
    assert 'name="timezone"' in html


def test_config_save_persists_via_shared_validation(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> object:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/config")
                r = await client.post(
                    "/config",
                    data={
                        "csrf_token": token,
                        "full_name": "Grace",
                        "timezone": "Europe/London",
                        "language": "en",
                        "max_context_token_limit": "9000",
                        "custom_note": "be concise",
                        "verbose_default": "true",  # checkbox present → True
                    },
                    follow_redirects=False,
                )
                assert r.status_code == 303, r.text[:300]
                async with pool.acquire() as conn:
                    cfg = await queries.get_user_config(conn, "usr_a")
            return cfg
        finally:
            await pool.close()

    cfg = asyncio.run(_drive())
    assert cfg.full_name == "Grace"
    assert cfg.timezone == "Europe/London"
    assert cfg.max_context_token_limit == 9000
    assert cfg.verbose_default is True
    assert cfg.custom_note == "be concise"


def test_config_save_unchecked_checkbox_is_false(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> bool:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                await queries.update_user_config(conn, "usr_a", verbose_default=True)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/config")
                # No verbose_default key → checkbox unchecked → must store False.
                await client.post(
                    "/config",
                    data={"csrf_token": token, "max_context_token_limit": "120000"},
                    follow_redirects=False,
                )
                async with pool.acquire() as conn:
                    cfg = await queries.get_user_config(conn, "usr_a")
            return cfg.verbose_default
        finally:
            await pool.close()

    assert asyncio.run(_drive()) is False


def test_config_save_invalid_int_re_renders_error(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str, object]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/config")
                r = await client.post(
                    "/config",
                    data={"csrf_token": token, "max_context_token_limit": "abc"},
                    follow_redirects=False,
                )
                async with pool.acquire() as conn:
                    cfg = await queries.get_user_config(conn, "usr_a")
            return r.status_code, r.text, cfg
        finally:
            await pool.close()

    status, text, cfg = asyncio.run(_drive())
    assert status == 400
    assert "Invalid value" in text
    assert cfg.full_name == "Ada"  # unchanged — nothing committed


# ---------------------------------------------------------------------------
# Cron pane
# ---------------------------------------------------------------------------


def test_cron_view_shows_deferred_delivery_note(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                r = await client.get("/cron/ses_1")
            assert r.status_code == 200, r.text[:300]
            return r.text
        finally:
            await pool.close()

    html = asyncio.run(_drive())
    assert "Delivery" in html  # the §6 deferred-delivery note
    assert 'name="cron_expression"' in html


def test_cron_add_creates_validated_job(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> list:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/cron/ses_1")
                r = await client.post(
                    "/cron/ses_1",
                    data={
                        "csrf_token": token, "cron_expression": "0 8 * * *",
                        "cron_message": "stand up", "timezone": "UTC", "report": "always",
                    },
                    follow_redirects=False,
                )
                assert r.status_code == 303, r.text[:300]
                async with pool.acquire() as conn:
                    jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            return jobs
        finally:
            await pool.close()

    jobs = asyncio.run(_drive())
    assert len(jobs) == 1
    assert jobs[0].cron_expression == "0 8 * * *"
    assert jobs[0].session_id == "ses_1"
    assert jobs[0].cron_message == "stand up"


def test_cron_add_invalid_expression_re_renders_error(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str, int]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/cron/ses_1")
                r = await client.post(
                    "/cron/ses_1",
                    data={
                        "csrf_token": token, "cron_expression": "not a cron",
                        "cron_message": "x",
                    },
                    follow_redirects=False,
                )
                async with pool.acquire() as conn:
                    jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            return r.status_code, r.text, len(jobs)
        finally:
            await pool.close()

    status, text, n = asyncio.run(_drive())
    assert status == 400
    assert "Invalid cron expression" in text
    assert n == 0  # nothing created


def test_cron_remove_deletes_owned_job(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> int:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            async with pool.acquire() as conn:
                job = await queries.create_cron_job(
                    conn, cron_id="cron_x", user_id="usr_a", session_id="ses_1",
                    cron_expression="0 8 * * *", cron_message="hi",
                )
            app = _build_app(pool=pool)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/cron/ses_1")
                await client.post(
                    "/cron/ses_1/remove",
                    data={"csrf_token": token, "cron_id": job.cron_id},
                    follow_redirects=False,
                )
                async with pool.acquire() as conn:
                    jobs = await queries.list_cron_jobs(conn, user_id="usr_a")
            return len(jobs)
        finally:
            await pool.close()

    assert asyncio.run(_drive()) == 0
