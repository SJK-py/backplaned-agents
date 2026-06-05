"""bp_router.observability.logging — the access-log quiet filter that drops
routine poll/health access lines (e.g. the channel's serviced-sessions
approval poll) so they don't flood `uvicorn.access`."""

from __future__ import annotations

import logging

from bp_router.observability.logging import _AccessLogQuietFilter

_PREFIXES = ("/healthz", "/metrics", "/v1/admin/serviced-sessions")


def _rec(method: str, path: str, status: int) -> logging.LogRecord:
    # Mirror uvicorn.access: args = (client, method, full_path, http_ver, status)
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %s', args=("127.0.0.1:1", method, path, "1.1", status),
        exc_info=None,
    )
    return rec


def test_drops_successful_get_to_quiet_path() -> None:
    f = _AccessLogQuietFilter(_PREFIXES)
    # Exact path and path-with-query both dropped.
    assert f.filter(_rec("GET", "/healthz", 200)) is False
    assert f.filter(_rec(
        "GET", "/v1/admin/serviced-sessions?channel=chatbot_telegram&since=x", 200
    )) is False


def test_keeps_errors_and_non_get_and_other_paths() -> None:
    f = _AccessLogQuietFilter(_PREFIXES)
    assert f.filter(_rec("GET", "/healthz", 503)) is True          # error still logs
    assert f.filter(_rec("POST", "/v1/admin/serviced-sessions", 200)) is True  # non-GET
    assert f.filter(_rec("GET", "/v1/tasks", 200)) is True         # real API call


def test_default_quiet_paths_cover_known_pollers() -> None:
    """The shipped default must quiet the known 30s pollers so they don't
    spam uvicorn.access — the channel's serviced-sessions poll and the admin
    UI's mcp-servers poll. A regression here brings the log flood back."""
    from bp_router.settings import Settings

    defaults = Settings.model_fields["access_log_quiet_paths"].default
    assert "/v1/admin/serviced-sessions" in defaults
    assert "/v1/admin/mcp-servers" in defaults
    # And the filter built from the default actually drops the spammy line
    # (incl. the prefix-matched per-server detail GET), while keeping errors.
    f = _AccessLogQuietFilter(tuple(defaults))
    assert f.filter(_rec("GET", "/v1/admin/mcp-servers", 200)) is False
    assert f.filter(_rec("GET", "/v1/admin/mcp-servers/srv_123", 200)) is False
    assert f.filter(_rec("GET", "/v1/admin/mcp-servers", 503)) is True


def test_fails_open_on_unexpected_record_shape() -> None:
    f = _AccessLogQuietFilter(_PREFIXES)
    # A non-access record (plain message, no args tuple) must be kept.
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="something happened", args=None, exc_info=None,
    )
    assert f.filter(rec) is True
    # A short/foreign args tuple is also kept.
    rec2 = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%s", args=("only-one",), exc_info=None,
    )
    assert f.filter(rec2) is True
