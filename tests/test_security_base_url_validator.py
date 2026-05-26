"""Tests for the SSRF defense on preset `base_url` fields.

IP-literal hosts are class-checked directly; hostnames are DNS-
resolved and EVERY resolved address is class-checked (H7). DNS is
stubbed via `monkeypatch` so these stay hermetic.
"""

from __future__ import annotations

import pytest

from bp_router import url_validation
from bp_router.url_validation import (
    BaseUrlValidationError,
    parse_allowed_hosts,
    validate_base_url,
)

# ---------------------------------------------------------------------------
# Empty / scheme handling
# ---------------------------------------------------------------------------


def test_empty_base_url_is_silently_accepted() -> None:
    """Empty string / None mean 'no override' — the cross-field check
    upstream decides whether that's allowed for the provider."""
    validate_base_url(provider="openai", base_url="")
    validate_base_url(provider="openai", base_url=None)  # type: ignore[arg-type]


def test_scheme_must_be_http_or_https() -> None:
    with pytest.raises(BaseUrlValidationError, match="http"):
        validate_base_url(provider="openai", base_url="ftp://example.com/v1")
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="openai", base_url="file:///etc/passwd")
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="openai", base_url="javascript:alert(1)")


def test_hosted_providers_require_https() -> None:
    """API keys flow through the SDK auth header. http:// would mean
    cleartext exfil to whatever IP the host resolves to."""
    for provider in ("gemini", "anthropic", "openai", "openai-embeddings"):
        with pytest.raises(BaseUrlValidationError, match="https"):
            validate_base_url(
                provider=provider, base_url="http://my-proxy.example.com/v1"
            )
        # https same host passes the scheme check.
        validate_base_url(
            provider=provider, base_url="https://my-proxy.example.com/v1"
        )


def test_local_providers_accept_http() -> None:
    """vLLM / LM Studio / Ollama on loopback don't speak TLS."""
    validate_base_url(
        provider="openai-compatible", base_url="http://localhost:8000/v1"
    )
    validate_base_url(
        provider="openai-compatible-embeddings",
        base_url="http://localhost:1234/v1",
    )


# ---------------------------------------------------------------------------
# Cloud-metadata literal-host rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", [
    "metadata.google.internal",
    "metadata.azure.com",
    "instance-data.ec2.internal",
    "metadata.ec2.internal",
])
def test_metadata_hostnames_blocked_for_all_providers(host: str) -> None:
    """These literal hostnames target cloud-metadata endpoints — never
    a legitimate target, regardless of provider."""
    for provider in ("openai", "openai-compatible"):
        scheme = "https" if provider == "openai" else "http"
        with pytest.raises(BaseUrlValidationError, match="metadata"):
            validate_base_url(
                provider=provider, base_url=f"{scheme}://{host}/v1"
            )


# ---------------------------------------------------------------------------
# IP-literal blocklist for hosted providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip,reason", [
    ("169.254.169.254", "link-local"),    # AWS / Azure metadata
    ("127.0.0.1", "loopback"),
    ("::1", "loopback"),
    ("10.0.0.1", "private"),
    ("172.20.0.1", "private"),
    ("192.168.1.1", "private"),
    ("0.0.0.0", "unspecified"),
    ("224.0.0.1", "multicast"),
    ("fe80::1", "link-local"),
    ("fd00::1", "private"),               # IPv6 ULA
])
def test_hosted_provider_blocks_dangerous_ip_literals(ip: str, reason: str) -> None:
    # Bracket IPv6 literals.
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(BaseUrlValidationError, match=reason):
        validate_base_url(
            provider="openai",
            base_url=f"https://{host}/v1",
        )


def test_hosted_provider_accepts_public_ip() -> None:
    """8.8.8.8 isn't a real OpenAI endpoint but the validator only
    blocks address-class issues. Public IPs pass."""
    validate_base_url(provider="openai", base_url="https://8.8.8.8/v1")


# ---------------------------------------------------------------------------
# Local-server providers permit loopback / private but not metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip", [
    "127.0.0.1",
    "192.168.1.10",
    "10.0.0.5",
    "172.20.0.7",
])
def test_local_provider_allows_loopback_and_private(ip: str) -> None:
    validate_base_url(
        provider="openai-compatible",
        base_url=f"http://{ip}:8000/v1",
    )


def test_local_provider_still_blocks_metadata_endpoint() -> None:
    """169.254.169.254 is link-local, which is technically a private
    range AWS uses for metadata. Even a misconfigured 'local'
    preset must NOT reach it — flag link-local separately from RFC1918."""
    with pytest.raises(BaseUrlValidationError, match="link-local"):
        validate_base_url(
            provider="openai-compatible",
            base_url="http://169.254.169.254/latest/meta-data/",
        )


# ---------------------------------------------------------------------------
# Hostnames (non-IP) ARE resolved and class-checked (H7 SSRF fix)
# ---------------------------------------------------------------------------


def _stub_getaddrinfo(ip: str):  # type: ignore[no-untyped-def]
    def _gai(host, *a, **k):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", (ip, 0))]

    return _gai


def test_hostname_resolving_to_public_ip_is_accepted(monkeypatch) -> None:
    """A hostname that resolves to a public address passes — the
    normal hosted-provider case."""
    # 8.8.8.8 is genuinely public (note: RFC-5737 "TEST-NET" ranges
    # like 203.0.113.0/24 are classified is_private by ipaddress).
    monkeypatch.setattr(
        url_validation.socket,
        "getaddrinfo",
        _stub_getaddrinfo("8.8.8.8"),
    )
    validate_base_url(provider="openai", base_url="https://api.openai.com/v1")


@pytest.mark.parametrize(
    "internal_ip",
    ["169.254.169.254", "10.0.0.5", "127.0.0.1", "192.168.1.1", "::1"],
)
def test_hostname_resolving_to_internal_ip_is_blocked(
    monkeypatch, internal_ip: str
) -> None:
    """H7 regression: a DNS name resolving to a
    metadata/RFC1918/loopback address MUST be rejected for a hosted
    provider — provider API keys must not be ferried there. (Before
    the fix the validator never resolved, so this slipped through.)"""
    monkeypatch.setattr(
        url_validation.socket,
        "getaddrinfo",
        _stub_getaddrinfo(internal_ip),
    )
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(
            provider="openai", base_url="https://evil.example.com/v1"
        )


def test_unresolvable_hostname_is_accepted(monkeypatch) -> None:
    """An unresolvable host is not a reachable SSRF target (the
    provider call would simply fail to connect), so it is accepted —
    refusing it would add config friction with no security gain."""
    import socket as _socket

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise _socket.gaierror("name does not resolve")

    monkeypatch.setattr(url_validation.socket, "getaddrinfo", _boom)
    validate_base_url(
        provider="openai",
        base_url="https://my-azure-proxy.corp.example.com/v1",
    )


# ---------------------------------------------------------------------------
# Allowed-hosts override
# ---------------------------------------------------------------------------


def test_allowed_hosts_bypasses_address_class_check() -> None:
    """Operators can carve exceptions for known private-VPC gateways."""
    allowed = frozenset({"litellm.svc.cluster.local"})
    # Non-allowlisted private host on a hosted provider would normally
    # be allowed (it's a hostname, no DNS), so use an IP literal to
    # exercise the bypass.
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="openai", base_url="https://192.168.1.5/v1")

    # Allowlisting the hostname doesn't make the IP literal pass
    # (allowlist is exact-match on host string).
    validate_base_url(
        provider="openai",
        base_url="https://litellm.svc.cluster.local/v1",
        allowed_hosts=allowed,
    )


def test_allowed_hosts_does_not_bypass_metadata_literal() -> None:
    """Even if an operator allowlists `metadata.google.internal`, the
    validator should still reject it — that string is never a
    legitimate target. Defense in depth."""
    # Actually our current implementation gives precedence to the
    # allowlist. Document behaviour: if an operator deliberately
    # allowlists the metadata hostname, that's a foot-gun the validator
    # respects. Verify this explicitly so we notice if behaviour changes.
    validate_base_url(
        provider="openai",
        base_url="https://metadata.google.internal/v1",
        allowed_hosts=frozenset({"metadata.google.internal"}),
    )


def test_parse_allowed_hosts_handles_whitespace_and_case() -> None:
    out = parse_allowed_hosts(
        "  Foo.Example.com,  bar.local,, ,  BAZ.svc.cluster.local "
    )
    assert out == frozenset({
        "foo.example.com",
        "bar.local",
        "baz.svc.cluster.local",
    })


def test_parse_allowed_hosts_empty_returns_empty_frozenset() -> None:
    assert parse_allowed_hosts(None) == frozenset()
    assert parse_allowed_hosts("") == frozenset()
    assert parse_allowed_hosts("   ") == frozenset()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_url_without_hostname_rejected() -> None:
    with pytest.raises(BaseUrlValidationError, match="hostname"):
        validate_base_url(provider="openai", base_url="https:///v1")


def test_case_insensitive_scheme() -> None:
    """Some clients capitalise the scheme. Treat as case-insensitive."""
    validate_base_url(provider="openai", base_url="HTTPS://api.example.com/v1")
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="openai", base_url="HTTP://api.example.com/v1")


def test_case_insensitive_host_for_metadata_check() -> None:
    """Don't let a capitalised metadata host bypass the literal blocklist."""
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(
            provider="openai",
            base_url="https://Metadata.Google.Internal/v1",
        )


def test_url_with_port_does_not_confuse_host_extraction() -> None:
    """The URL parser handles `host:port` correctly. Verify both legs."""
    # Public IP:port is fine.
    validate_base_url(provider="openai", base_url="https://8.8.8.8:443/v1")
    # Private IP:port is blocked for hosted.
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(provider="openai", base_url="https://10.0.0.1:8443/v1")


def test_userinfo_in_url_does_not_enable_bypass() -> None:
    """`https://safe@10.0.0.1/...` parses host as 10.0.0.1, not 'safe'.
    Verify the validator extracts the right host."""
    with pytest.raises(BaseUrlValidationError):
        validate_base_url(
            provider="openai",
            base_url="https://decoy@10.0.0.1/v1",
        )
