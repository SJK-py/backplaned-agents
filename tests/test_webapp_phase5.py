"""Webapp Phase 5 — config + cron structured panes ([webapp.md] §5).

The config form reads and writes the ROUTER's user-scoped state through the
steward store surface, with the SAME validation the config agent uses
(bp_agents.user_prefs); cron pane add/remove reuse bp_agents.cron_manage.
Driven on one loop via httpx.ASGITransport (asyncpg is loop-bound).

The app under test therefore needs a channel core, because that is where the
store handle lives — `_build_app` wires a minimal one over the same
`FakeStore` the fake upstream serves sessions from, so seeding a preference
and asserting on a save both go through one object.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from bp_agents.db import queries
from bp_agents.db.connection import open_pool
from bp_agents.settings import SuiteSettings
from bp_agents.user_prefs import ConfigError, coerce_config_value
from tests.fake_store import FakeChannelStore, UpstreamSessionMixin


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class _Upstream(UpstreamSessionMixin):
    """Stands in for the router. The LLM-preset half models what the real
    endpoints do: the listing is ALREADY filtered to what this caller's tier
    admits (the router applies the gate), and `set_llm_preference` refuses
    anything outside it with the same 403 the router raises."""

    def __init__(self, *, presets=None) -> None:
        self.presets = presets if presets is not None else [
            {"name": "default", "description": "Everyday", "default_for": ["balanced", "lite"]},
            {"name": "claude", "description": "Claude", "default_for": ["pro"]},
        ]
        self.preferences: dict[str, str] = {}

    async def login(self, *, email: str, password: str) -> dict:
        return {
            "access_token": _fake_jwt("usr_a"), "refresh_token": "r",
            "expires_at": "2999-01-01T00:00:00+00:00", "level": "tier1",
        }

    async def list_llm_presets(self, *, access_token, slot=None):
        return list(self.presets)

    async def get_llm_preferences(self, *, access_token):
        return dict(self.preferences)

    async def set_llm_preference(self, *, access_token, slot, preset_name):
        from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

        if preset_name is None:
            self.preferences.pop(slot, None)
        elif preset_name not in {p["name"] for p in self.presets}:
            raise UpstreamError(403, "preset_not_allowed")
        else:
            self.preferences[slot] = preset_name
        return dict(self.preferences)

    async def aclose(self):
        pass


class _StoreOnlyCore:
    """What the config pages need from `app.state.core`: a store. The rest of
    `ChannelCore` (dispatch, leases, delegation) is not on this path."""

    def __init__(self, store) -> None:
        self.store = store


def _build_app(*, pool, upstream=None):
    pytest.importorskip("fastapi")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("jinja2")
    from pydantic import SecretStr  # noqa: PLC0415

    from bp_agents.agents.webapp.app import create_app  # noqa: PLC0415
    from bp_agents.agents.webapp.config import WebappConfig  # noqa: PLC0415

    upstream = upstream or _Upstream()
    cfg = WebappConfig(session_secret=SecretStr("x" * 32), session_cookie_secure=False)
    return create_app(
        cfg, upstream=upstream, pool=pool,
        core=_StoreOnlyCore(FakeChannelStore(upstream.store)),
    )


async def _seed(pool) -> None:
    """Suite-side identity in Postgres; the SETTINGS belong to the store the
    caller built its app with, so a test that asserts on them seeds there."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE TABLE cron_jobs, user_config, "
            "suite_platform_mappings RESTART IDENTITY CASCADE"
        )
        await queries.create_user_config(conn, user_id="usr_a")


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
        # Model choice is not a `user_config` field at all any more — it is a
        # router-side slot preference set through /config/models.
        coerce_config_value("preset_pro", "x")


def test_model_choice_is_not_a_user_config_field() -> None:
    """The four `preset_*` columns are gone. Nothing in the suite's editable
    or displayable surface may name a model — the choice lives router-side
    ([docs/design/router-resolved-preset-slots.md] §8)."""
    from bp_agents.db.models import UserConfigRow  # noqa: PLC0415
    from bp_agents.user_prefs import (  # noqa: PLC0415
        displayable_fields,
        editable_fields,
    )

    assert not [f for f in editable_fields() if f.startswith("preset")]
    assert not [f for f in displayable_fields() if f.startswith("preset")]
    assert not [f for f in UserConfigRow.model_fields if f.startswith("preset")]


def test_model_slots_are_built_from_the_router_listing() -> None:
    """The menu is whatever the router says this caller may run — the suite
    contributes the slot taxonomy and nothing else. A slot the user has never
    touched reports `current=None` so the form can select "Default"."""
    from types import SimpleNamespace  # noqa: PLC0415

    from bp_agents.agents.webapp.pages.config import _model_slots  # noqa: PLC0415

    upstream = _Upstream()
    upstream.preferences["pro"] = "claude"
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(upstream=upstream)),
        session={"access_token": "tok"},
    )
    rows = {r["slot"]: r for r in asyncio.run(_model_slots(request))}

    assert list(rows) == ["pro", "balanced", "lite"]
    assert rows["pro"]["current"] == "claude"
    assert rows["balanced"]["current"] is None
    # `default_for` from the router marks which option is the operator default.
    assert rows["pro"]["default_name"] == "claude"
    assert rows["balanced"]["default_name"] == "default"
    # Every admitted preset is offered for every slot — the router already
    # filtered by entitlement, and slots have no per-slot allow-list.
    assert {c["name"] for c in rows["lite"]["choices"]} == {"default", "claude"}


def test_model_slots_degrade_when_the_router_is_unreachable() -> None:
    """A settings page that 500s because the router blipped is worse than one
    that hides the model pane for a refresh."""
    from types import SimpleNamespace  # noqa: PLC0415

    from bp_agents.agents.webapp.pages.config import _model_slots  # noqa: PLC0415
    from bp_agents.agents.webapp.upstream import UpstreamError  # noqa: PLC0415

    class _Broken(_Upstream):
        async def list_llm_presets(self, *, access_token, slot=None):
            raise UpstreamError(503, "down")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(upstream=_Broken())),
        session={"access_token": "tok"},
    )
    assert asyncio.run(_model_slots(request)) == []


def test_config_agent_uses_shared_editable_fields() -> None:
    """The config agent must source its editable set + coercion from
    user_prefs, so the NL path and the form can't drift."""
    import importlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from bp_agents.user_prefs import PREF_TYPES  # noqa: PLC0415

    # importlib returns the real module (the package __init__ rebinds the
    # `agent` attribute to the Agent instance, shadowing the submodule).
    src = inspect.getsource(importlib.import_module("bp_agents.agents.config.agent"))
    assert "coerce_config_value" in src and "editable_fields" in src
    assert "max_context_token_limit" in PREF_TYPES


# ---------------------------------------------------------------------------
# Config pane
# ---------------------------------------------------------------------------


def test_config_view_prefills_current_values(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            upstream.store.set_pref("full_name", "Ada")
            app = _build_app(pool=pool, upstream=upstream)
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
            upstream = _Upstream()
            app = _build_app(pool=pool, upstream=upstream)
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
            return upstream.store
        finally:
            await pool.close()

    store = asyncio.run(_drive())
    # Every field landed in the USER scope — cross-session, and not in this
    # session's state, which is what a carrier session must not become.
    assert store.pref("full_name") == "Grace"
    assert store.pref("timezone") == "Europe/London"
    assert store.pref("max_context_token_limit") == "9000"
    assert store.pref("verbose_default") == "true"
    assert store.pref("custom_note") == "be concise"
    assert store.session_state == {}


def test_config_model_pane_renders_and_persists(suite_db_url: str) -> None:
    """The Models pane offers exactly what the router says the caller may run,
    and each slot saves on its own through the router — never into
    `user_config`. A refusal comes back as a message on the page, which is the
    whole point of moving the gate to selection time."""
    pytest.importorskip("fastapi")

    upstream = _Upstream()

    async def _drive() -> object:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            app = _build_app(pool=pool, upstream=upstream)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                page = await client.get("/config")
                # One <select> per slot, plus the "leave it to the operator"
                # option naming the default.
                for slot in ("pro", "balanced", "lite"):
                    assert 'name="preset_name"' in page.text
                    assert f'value="{slot}"' in page.text
                assert "Deep reasoning" in page.text
                assert "Default (claude)" in page.text
                # No model field may ride the user_config form any more.
                assert 'name="preset_balanced"' not in page.text

                token = await _csrf(client, "/config")
                ok = await client.post(
                    "/config/models",
                    data={"csrf_token": token, "slot": "pro", "preset_name": "claude"},
                    follow_redirects=False,
                )
                assert ok.status_code == 303, ok.text[:300]
                assert upstream.preferences == {"pro": "claude"}

                # A preset the router refuses → the page says so; nothing saved.
                token = await _csrf(client, "/config")
                bad = await client.post(
                    "/config/models",
                    data={"csrf_token": token, "slot": "pro", "preset_name": "gpt"},
                    follow_redirects=False,
                )
                assert bad.status_code == 303
                assert "model_error=not_allowed" in bad.headers["location"]
                assert upstream.preferences == {"pro": "claude"}

                page = await client.get("/config?model_error=not_allowed")
                assert "isn’t available on your plan" in page.text

                # Empty value clears the preference back to the default.
                token = await _csrf(client, "/config")
                cleared = await client.post(
                    "/config/models",
                    data={"csrf_token": token, "slot": "pro", "preset_name": ""},
                    follow_redirects=False,
                )
                assert cleared.status_code == 303
                assert upstream.preferences == {}

                # An unknown slot is a 404, not a pass-through to the router.
                token = await _csrf(client, "/config")
                unknown = await client.post(
                    "/config/models",
                    data={"csrf_token": token, "slot": "nope", "preset_name": "claude"},
                    follow_redirects=False,
                )
                assert unknown.status_code == 404

                async with pool.acquire() as conn:
                    cfg = await queries.get_user_config(conn, "usr_a")
            return cfg
        finally:
            await pool.close()

    cfg = asyncio.run(_drive())
    # The suite row is untouched by model selection — and holds no settings
    # at all any more.
    assert not [f for f in type(cfg).model_fields if f.startswith("preset")]


def test_config_save_unchecked_checkbox_is_false(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> str | None:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            upstream.store.set_pref("verbose_default", "true")
            app = _build_app(pool=pool, upstream=upstream)
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
            return upstream.store.pref("verbose_default")
        finally:
            await pool.close()

    assert asyncio.run(_drive()) == "false"


def test_config_works_with_only_closed_sessions(suite_db_url: str) -> None:
    """The carrier session is a CARRIER, not a scope.

    The rows the settings form touches have no session at all; the session in
    the ops path is only what the endpoint checks ownership against, and that
    check does not look at `closed_at`. So a user whose every conversation is
    closed can still read and change their settings — which is the whole
    reason this shipped without a new router endpoint."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, object]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            for row in upstream.sessions.values():
                row["closed_at"] = "2026-01-02T00:00:00+00:00"
            app = _build_app(pool=pool, upstream=upstream)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/config")
                r = await client.post(
                    "/config",
                    data={"csrf_token": token, "full_name": "Hopper"},
                    follow_redirects=False,
                )
            return r.status_code, upstream.store
        finally:
            await pool.close()

    status, store = asyncio.run(_drive())
    assert status == 303
    assert store.pref("full_name") == "Hopper"


def test_config_save_without_any_session_reports_rather_than_lying(
    suite_db_url: str,
) -> None:
    """No session at all means no route into the user scope. Redirecting to
    "?saved=1" would tell the user their settings were stored when nothing
    was written."""
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str, object]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            upstream.sessions.clear()
            app = _build_app(pool=pool, upstream=upstream)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await _login(client)
                token = await _csrf(client, "/config")
                r = await client.post(
                    "/config",
                    data={"csrf_token": token, "full_name": "Hopper"},
                    follow_redirects=False,
                )
            return r.status_code, r.text, upstream.store
        finally:
            await pool.close()

    status, text, store = asyncio.run(_drive())
    assert status == 400
    assert "no session to write through" in text
    assert store.pref("full_name") is None


def test_config_save_invalid_int_re_renders_error(suite_db_url: str) -> None:
    pytest.importorskip("fastapi")

    async def _drive() -> tuple[int, str, object]:
        pool = await open_pool(SuiteSettings(database_url=suite_db_url))
        try:
            await _seed(pool)
            upstream = _Upstream()
            upstream.store.set_pref("full_name", "Ada")
            app = _build_app(pool=pool, upstream=upstream)
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
            return r.status_code, r.text, upstream.store
        finally:
            await pool.close()

    status, text, store = asyncio.run(_drive())
    assert status == 400
    assert "Invalid value" in text
    assert store.pref("full_name") == "Ada"  # unchanged — nothing committed


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
