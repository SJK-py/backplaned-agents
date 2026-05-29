"""MCP server `url` is SSRF-validated at admin save (create + patch).

Pre-release blocker: the MCP server `url` was only scheme-checked (http/https),
unlike the LLM preset `base_url` which runs through `validate_base_url`. The
bridge connects to this URL and presents the configured `auth_value_ref`
credential, so a URL pointing at the cloud-metadata endpoint or an internal
service is a credential-exfil / SSRF vector.

Fix: `create_mcp_server` / `update_mcp_server` now call `_check_mcp_url_ssrf`,
which runs `validate_base_url(provider="mcp", ...)`. MCP is classified
local-class (loopback / private allowed — internal MCP servers are the norm),
but link-local (169.254.169.254), cloud-metadata hostnames, and
multicast/reserved are blocked, and operators may allowlist hosts via
ROUTER_BASE_URL_ALLOWED_HOSTS.
"""

from __future__ import annotations

import inspect

import pytest


def test_mcp_is_local_class_in_url_validation() -> None:
    """`mcp` is treated as a local provider (loopback/private allowed) but is
    NOT a hosted provider (http permitted — internal MCP is commonly http)."""
    from bp_router.url_validation import _HOSTED_PROVIDERS, _is_local_provider

    assert _is_local_provider("mcp") is True
    assert "mcp" not in _HOSTED_PROVIDERS


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com/sse",   # public TLS
        "http://127.0.0.1:9000/sse",     # loopback (local MCP)
        "http://10.1.2.3/sse",           # RFC1918 private (internal MCP)
    ],
)
def test_validate_base_url_mcp_allows_internal_and_public(url: str) -> None:
    from bp_router.url_validation import validate_base_url

    validate_base_url(provider="mcp", base_url=url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud-metadata IP
        "http://metadata.google.internal/",          # metadata hostname
        "https://metadata.azure.com/",
    ],
)
def test_validate_base_url_mcp_blocks_metadata(url: str) -> None:
    from bp_router.url_validation import BaseUrlValidationError, validate_base_url

    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="mcp", base_url=url)


def test_mcp_ssrf_allowlist_override() -> None:
    """An operator-approved host passes even in a blocked range."""
    from bp_router.url_validation import validate_base_url

    validate_base_url(
        provider="mcp",
        base_url="http://169.254.169.254/x",
        allowed_hosts=frozenset({"169.254.169.254"}),
    )


def test_check_mcp_url_ssrf_raises_http_400_on_metadata() -> None:
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from bp_router.api.admin import _check_mcp_url_ssrf

    # request=None → empty allowlist; a metadata URL must 400.
    with pytest.raises(HTTPException) as ei:
        _check_mcp_url_ssrf("http://169.254.169.254/latest/", None)
    assert ei.value.status_code == 400

    # A normal internal/public URL passes.
    _check_mcp_url_ssrf("https://mcp.example.com/sse", None)


def test_create_and_update_handlers_run_the_ssrf_check() -> None:
    """Source pin: both the create and patch handlers call the SSRF guard."""
    pytest.importorskip("fastapi")
    from bp_router.api import admin

    assert "_check_mcp_url_ssrf(req.url, request)" in inspect.getsource(
        admin.create_mcp_server
    )
    assert "_check_mcp_url_ssrf(synthetic.url, request)" in inspect.getsource(
        admin.update_mcp_server
    )
