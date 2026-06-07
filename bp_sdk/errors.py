"""bp_sdk.errors — Typed exceptions agent code may raise.

Each error maps to a specific status_code on the Result frame. See
`docs/backplaned/sdk/core.md` §10.
"""

from __future__ import annotations


class HandlerError(Exception):
    """Base class. Maps to status_code=500 unless subclass overrides."""

    status_code: int = 500


class InputValidationError(HandlerError):
    """Input failed schema validation. Maps to 400.

    Named to NOT shadow pydantic's `ValidationError` (the SDK and
    agent code both import that one for model construction).
    """

    status_code = 400


class PermissionDeniedError(HandlerError):
    """Caller is not authorised for the requested operation. Maps to 403.

    Named to NOT shadow the builtin `PermissionError` (an OSError).
    """

    status_code = 403


class NotFoundError(HandlerError):
    status_code = 404


class CancellationError(HandlerError):
    """Raised by `ctx.cancel_token` when a task is cancelled. Maps to 499."""

    status_code = 499


class UpstreamError(HandlerError):
    """Wraps an error from a downstream call (LLM, peer, storage). Maps to 502."""

    status_code = 502


class TransportError(Exception):
    """Socket-level failure (disconnect, ack timeout). Not a handler error;
    handled by the SDK loop, not by user code."""


class TransportPermanentlyFailed(Exception):
    """The recv loop gave up after `recv_consecutive_failures_max`
    consecutive failures — auth permanently rejected, a dead
    transport supervisor, or a decode bug. TERMINAL: the agent
    cannot recover, so this escapes `_recv_loop` → `run_until` →
    `run_async`. `Agent.run()` translates it to a non-zero process
    exit.

    Deliberately NOT a `TransportError` (which the SDK loop treats
    as transient/handled): the old code `return`ed silently here, so
    a permanently-dead agent exited 0 and a fleet on
    `systemd Restart=on-failure` (or any exit-code orchestrator)
    never restarted it. This must be loud and must escape."""


class FrameTooLargeError(ValueError):
    """A frame's serialized size exceeds the router's negotiated
    `max_payload_bytes` cap.

    A usage error, not a transport hiccup: the router would close
    the socket (1009) on receipt and the SDK would re-queue + retry
    the same oversize frame forever. Raised synchronously from
    `transport.send()` (so it surfaces on the agent author's
    `peers.spawn()` / `peers.delegate()` call) BEFORE the frame is
    queued. The almost-always cause is inlining large
    `image_part()` / `document_part()` bytes into a task payload —
    base64 is ~33% larger than the raw bytes — when that media
    should travel out-of-band via `ctx.files.put()` attachments.
    `ValueError` (not `TransportError`) so it is loud and not
    swallowed by the SDK loop's transport-error handling."""


class ProtocolError(Exception):
    """Frame-level invariant violation (unexpected frame, version mismatch)."""
