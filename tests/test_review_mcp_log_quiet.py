"""Regression: the bridge quiets httpx's per-request INFO logging.

The supervisor polls /v1/admin/mcp-servers every poll_interval_s (30s), and
httpx logs an INFO line per request, flooding the log with routine 200s. The
bridge raises the httpx logger to WARNING (overridable) so real errors still
surface.
"""

from __future__ import annotations

import logging

import pytest

from bp_mcp_bridge.__main__ import _configure_logging


@pytest.fixture(autouse=True)
def _restore_httpx_level():  # type: ignore[no-untyped-def]
    logger = logging.getLogger("httpx")
    prev = logger.level
    yield
    logger.setLevel(prev)


def test_httpx_logger_quieted_to_warning_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("BP_MCP_BRIDGE_HTTPX_LOG_LEVEL", raising=False)
    logging.getLogger("httpx").setLevel(logging.INFO)
    _configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpx_logger_level_overridable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BP_MCP_BRIDGE_HTTPX_LOG_LEVEL", "DEBUG")
    _configure_logging()
    assert logging.getLogger("httpx").level == logging.DEBUG
