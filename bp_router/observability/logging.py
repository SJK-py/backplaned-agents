"""bp_router.observability.logging — Structured JSON logging.

See `docs/observability.md` §3.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bp_router.settings import Settings


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line to stdout.

    Required fields per `observability.md` §3.2: ts, level, logger,
    event, plus any keyword extras the caller passed via `logger.info(
    event=..., trace_id=..., ...)`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        # Include free-form message only when no `event` is set;
        # `event` is the canonical signal.
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        if record.msg:
            payload["message"] = record.getMessage()

        # Attach trace context if present (OTel instrumentation injects these
        # via a LogRecordFactory; here we just include whatever is on the record).
        for key in ("trace_id", "span_id"):
            v = getattr(record, key, None)
            if v is not None:
                payload[key] = v

        # Attach any extra fields the caller bound (filtered to bp.* + a few standard).
        for key, value in record.__dict__.items():
            if key.startswith(("_", "args", "msg", "exc_", "stack_")):
                continue
            if key in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "event",
                "trace_id", "span_id", "message", "taskName",
            }:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _AccessLogQuietFilter(logging.Filter):
    """Drop `uvicorn.access` lines for *successful* GET requests to noisy
    poll/health paths (configurable via `access_log_quiet_paths`).

    `uvicorn.access` logs with `record.args = (client_addr, method,
    full_path, http_version, status_code)`. We read that tuple rather than
    re-parsing the formatted message. Fails **open** — any record whose
    shape isn't the expected access tuple is kept, so a uvicorn change can
    never silently swallow logs."""

    def __init__(self, quiet_prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self._prefixes = quiet_prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, full_path, status = args[1], args[2], args[4]
        try:
            status_code = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return True
        if method != "GET" or status_code >= 400:
            return True
        path = str(full_path).split("?", 1)[0]
        return not any(path.startswith(p) for p in self._prefixes)


def configure_logging(settings: Settings) -> None:
    """Replace any pre-existing handlers with a JSON-formatted stdout handler."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Quiet routine poll/health access lines (kept out of the JSON stream).
    quiet = getattr(settings, "access_log_quiet_paths", None)
    if quiet:
        logging.getLogger("uvicorn.access").addFilter(
            _AccessLogQuietFilter(tuple(quiet))
        )
