"""bp_router.llm.providers._openai_client — Shared async-openai client
factory + exception classifier.

Used by every adapter that talks to an OpenAI-compatible HTTP API:
``OpenAIAdapter``, ``OpenAIEmbeddingsAdapter``,
``OpenAICompatibleAdapter``, ``OpenAICompatibleEmbeddingsAdapter``.

Before extraction, each adapter carried a near-identical ``_get_client``
that lazy-imported ``AsyncOpenAI``, dropped ``base_url`` from the
constructor kwargs when it was None, and built the client. Bug fixes
(e.g. adding a per-adapter timeout knob) had to land in four places.

The factory keeps the import lazy — ``openai`` isn't a hard
dependency of ``bp_router``; only adapters that get wired through
``LlmService._build_adapter`` actually import it. The error message
on missing-import remains the same single string.

`classify_openai_exception` does the same de-duplication for the
exception → ``ErrorCode`` mapping. The four adapters all use the
same ``openai`` SDK exception classes, so a single classifier
serves them all.
"""

from __future__ import annotations

from typing import Any

from bp_protocol.frames import ErrorCode
from bp_router.llm.retry_classification import RetryHint


def make_async_openai(
    *,
    api_key: str,
    base_url: str | None = None,
) -> Any:
    """Construct an ``AsyncOpenAI`` client.

    ``base_url`` overrides the SDK default when set (Azure proxies,
    LiteLLM, vLLM, LM Studio, etc.); leave None for the official
    endpoint.

    Raises ``RuntimeError`` with the standard install hint if the
    ``openai`` package isn't available — the four call sites used to
    duplicate this string.
    """
    try:
        from openai import AsyncOpenAI  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "openai not installed; `pip install openai`"
        ) from exc
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def classify_openai_exception(exc: BaseException) -> RetryHint:
    """Map an ``openai`` SDK exception to a typed ``RetryHint``.

    Shared by the four OpenAI-SDK adapters
    (``OpenAIAdapter``, ``OpenAIEmbeddingsAdapter``,
    ``OpenAICompatibleAdapter``, ``OpenAICompatibleEmbeddingsAdapter``)
    since they all raise from the same exception hierarchy.

    Class names checked by ``__name__`` rather than ``isinstance`` so
    the classifier doesn't import ``openai`` (deferred per the
    rest of this module). The SDK's class names have been stable
    across recent versions; if a future version renames one we'll
    catch it via the default-hint fallback (still retriable).
    """
    cls_name = type(exc).__name__

    # Rate limits — caller should honour Retry-After.
    if cls_name == "RateLimitError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
            retry_after_seconds=_parse_retry_after(exc),
            upstream_class=cls_name,
        )

    # Timeouts / connection errors — transient.
    if cls_name in ("APITimeoutError", "APIConnectionError"):
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
            upstream_class=cls_name,
        )

    # 5xx — provider unavailable.
    if cls_name == "InternalServerError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
            upstream_class=cls_name,
        )

    # 401 / 403 — operator must rotate the key.
    if cls_name == "AuthenticationError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
            upstream_class=cls_name,
        )
    if cls_name == "PermissionDeniedError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
            upstream_class=cls_name,
        )

    # 400 — bad prompt / oversized / malformed tool spec.
    if cls_name == "BadRequestError":
        # Some servers (vLLM, LM Studio) return 400 on content-policy
        # blocks. We can't disambiguate without inspecting the body;
        # default to the more general invalid_request bucket.
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=cls_name,
        )

    # 422 — unprocessable entity. Treat as invalid_request.
    if cls_name == "UnprocessableEntityError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=cls_name,
        )

    # 409 — conflict (idempotency clash, etc.). Not retriable.
    if cls_name == "ConflictError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=cls_name,
        )

    # 404 — wrong model name / endpoint.
    if cls_name == "NotFoundError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=cls_name,
        )

    # The `parse` / structured-output helpers raise these when the
    # model stops on a finish reason the helper considers an error
    # rather than a normal stop. They're TERMINAL: the response
    # already happened, retrying just burns tokens for the same
    # outcome.
    if cls_name == "LengthFinishReasonError":
        # Model hit `max_tokens` before finishing the structured
        # output. Caller should raise `max_tokens` or shrink the
        # schema, not retry.
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
            upstream_class=cls_name,
        )
    if cls_name == "ContentFilterFinishReasonError":
        # Model halted on a content-policy block. Retry won't help.
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_CONTENT_FILTER,
            upstream_class=cls_name,
        )

    # `client.parse(...)` raises this when the JSON body coming back
    # doesn't match the declared response schema — usually means the
    # provider returned an unexpected shape (proxy injecting fields,
    # rate-limit page rendered as JSON, etc.). Treat as transient:
    # one retry against the official endpoint usually clears proxy
    # weirdness; against a flaky local server it hits the bounded
    # retry cap.
    if cls_name == "APIResponseValidationError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_TIMEOUT,
            upstream_class=cls_name,
        )

    # `OAuthError` is `AuthenticationError`'s subclass for OAuth flows;
    # the sibling check at line 98 already catches it via
    # `cls_name == "AuthenticationError"` only when the SDK reports
    # the parent. Cover the dedicated subclass name here so the
    # telemetry stays accurate.
    if cls_name == "OAuthError":
        return RetryHint(
            code=ErrorCode.LLM_UPSTREAM_AUTH_FAILED,
            upstream_class=cls_name,
        )

    # `APIStatusError` parent class — catches CDN-fronted 429/5xx
    # responses that arrive with a non-typed status code (e.g.
    # Cloudflare returning 530). Bucket on the HTTP status code so
    # we still retry the transient ones.
    if cls_name == "APIStatusError":
        status = getattr(exc, "status_code", None)
        if status == 429:
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_RATE_LIMITED,
                retry_after_seconds=_parse_retry_after(exc),
                upstream_class=cls_name,
            )
        if isinstance(status, int) and 500 <= status < 600:
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_UNAVAILABLE,
                upstream_class=cls_name,
            )
        if isinstance(status, int) and 400 <= status < 500:
            return RetryHint(
                code=ErrorCode.LLM_UPSTREAM_INVALID_REQUEST,
                upstream_class=cls_name,
            )
        # Status missing / nonsense — fall through to default.

    # Unknown OpenAI exception type → fall through to caller's default.
    return RetryHint(
        code=ErrorCode.INTERNAL_ERROR,
        upstream_class=cls_name,
    )


def _parse_retry_after(exc: BaseException) -> float | None:
    """Extract `Retry-After` from an OpenAI `RateLimitError`.

    Delegates to `bp_router.llm.retry_classification.parse_http_retry_after`,
    which handles both delta-seconds and HTTP-date forms (the latter
    matters for WAF-fronted endpoints that ban-list with multi-minute
    waits in the date format).
    """
    from bp_router.llm.retry_classification import parse_http_retry_after  # noqa: PLC0415

    return parse_http_retry_after(exc)
