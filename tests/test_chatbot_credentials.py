"""chatbot credentials — token-refresh serialization.

The router rotates a refresh token on use (single-use), so two concurrent
refreshes with the same token race — one wins, the other 401s. The chatbot
runs concurrent callers (the Telegram + KakaoTalk approval loops, per-user
ops), so a refresh must be exclusive; a second caller reuses the freshly
cached token instead of refreshing again. These tests assert exactly one
refresh happens under concurrent callers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bp_agents.agents.chatbot.credentials as cred

_FAR_FUTURE = "2999-01-01T00:00:00+00:00"


def _client(monkeypatch: pytest.MonkeyPatch) -> cred.HttpChannelCredentials:
    monkeypatch.setattr(cred, "persist_service_token", lambda *a, **k: None)
    return cred.HttpChannelCredentials(
        http_url="http://router:8000",
        config=SimpleNamespace(service_refresh_token="T0"),
    )


def test_service_token_refresh_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch)
    calls = {"n": 0}

    async def _fake_refresh(refresh_token: str) -> dict:
        calls["n"] += 1
        await asyncio.sleep(0.05)  # widen the overlap window
        return {
            "access_token": "acc",
            "refresh_token": f"T{calls['n']}",
            "expires_at": _FAR_FUTURE,
        }

    monkeypatch.setattr(c, "_refresh", _fake_refresh)

    async def _drive() -> list[str]:
        toks = await asyncio.gather(
            c._service_token(), c._service_token(), c._service_token()
        )
        await c.aclose()
        return toks

    toks = asyncio.run(_drive())
    assert calls["n"] == 1          # one refresh despite 3 concurrent callers
    assert set(toks) == {"acc"}     # all callers got the same token


def test_user_token_refresh_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch)
    calls = {"refresh": 0, "mint": 0}

    async def _fake_refresh(refresh_token: str) -> dict:
        calls["refresh"] += 1
        await asyncio.sleep(0.05)
        return {
            "access_token": "uacc",
            "refresh_token": "R1",
            "expires_at": _FAR_FUTURE,
        }

    async def _fake_mint(user_id: str) -> str:
        calls["mint"] += 1
        return "minted"

    monkeypatch.setattr(c, "_refresh", _fake_refresh)
    monkeypatch.setattr(c, "_mint_user_refresh", _fake_mint)

    async def _drive() -> list[str]:
        toks = await asyncio.gather(
            c._user_token("u1"), c._user_token("u1"), c._user_token("u1")
        )
        await c.aclose()
        return toks

    toks = asyncio.run(_drive())
    assert (calls["mint"], calls["refresh"]) == (1, 1)  # minted + refreshed once
    assert set(toks) == {"uacc"}
