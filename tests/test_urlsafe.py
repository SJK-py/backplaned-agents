"""bp_agents.common.urlsafe — SSRF guard for agent web fetches."""

from __future__ import annotations

import asyncio
import socket

import pytest

from bp_agents.common.urlsafe import UnsafeUrlError, ensure_fetchable_url


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
