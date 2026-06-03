"""bp_agents.common.urlsafe — SSRF guard for agent web fetches."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from bp_agents.common.urlsafe import (
    UnsafeUrlError,
    ensure_fetchable_url,
    safe_stream_get,
)


def _mock_client(handler):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _check(url: str) -> None:
    asyncio.run(ensure_fetchable_url(url))


def test_blocks_loopback_private_linklocal_metadata() -> None:
    for bad in (
        "http://127.0.0.1/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://metadata.google.internal/",
    ):
        with pytest.raises(UnsafeUrlError):
            _check(bad)


def test_blocks_non_http_scheme() -> None:
    for bad in ("ftp://example.com/", "file:///etc/passwd", "gopher://x/", "data:,hi"):
        with pytest.raises(UnsafeUrlError):
            _check(bad)


def test_blocks_missing_host() -> None:
    with pytest.raises(UnsafeUrlError):
        _check("http:///nohost")


def test_allows_public_ip_literal() -> None:
    _check("https://8.8.8.8/")  # public — must not raise


def test_hostname_resolving_private_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))],
    )
    with pytest.raises(UnsafeUrlError):
        _check("http://intranet.example/")


def test_hostname_resolving_public_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    _check("http://example.com/")  # public — must not raise


# --- safe_stream_get: redirect following with per-hop SSRF re-check ---------


def test_safe_stream_get_follows_redirect() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})  # relative
        return httpx.Response(200, content=b"final body")

    async def _drive() -> bytes:
        async with _mock_client(handler) as c:
            return await safe_stream_get(c, "http://8.8.8.8/a", cap=1000)

    assert asyncio.run(_drive()) == b"final body"


def test_safe_stream_get_revalidates_each_redirect_hop() -> None:
    """A public page that 302s to the cloud-metadata endpoint is blocked BEFORE
    the metadata request is made (the hop is re-validated)."""
    requested: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested.append(req.url.host)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async def _drive() -> None:
        async with _mock_client(handler) as c:
            await safe_stream_get(c, "http://8.8.8.8/x", cap=1000)

    with pytest.raises(UnsafeUrlError):
        asyncio.run(_drive())
    assert requested == ["8.8.8.8"]  # the metadata host was never contacted


def test_safe_stream_get_caps_redirects() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/loop"})

    async def _drive() -> None:
        async with _mock_client(handler) as c:
            await safe_stream_get(c, "http://8.8.8.8/loop", cap=1000, max_redirects=2)

    with pytest.raises(UnsafeUrlError):
        asyncio.run(_drive())


def test_safe_stream_get_enforces_byte_cap() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    async def _drive() -> None:
        async with _mock_client(handler) as c:
            await safe_stream_get(c, "http://8.8.8.8/big", cap=10)

    with pytest.raises(ValueError):
        asyncio.run(_drive())
