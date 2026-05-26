"""bp_agents.common.urlsafe — SSRF guard for agent-initiated web fetches.

`web_fetch` / `web_download` / `md_converter.webpage` pull LLM-chosen URLs,
so a research turn (possibly steered by untrusted page content) must not be
able to reach loopback, RFC1918, link-local (incl. the cloud-metadata
endpoint 169.254.169.254), or other non-public addresses. `ensure_fetchable_url`
resolves the host and rejects any URL whose address(es) fall in a blocked
class. Mirrors the platform's `bp_router.url_validation`, tuned for general
web fetches (http allowed; not just LLM `base_url`s).

Caveat: this is a resolve-time check. Full DNS-rebinding protection needs
pinning the connection to the validated IP — out of scope here; the check
covers the common SSRF cases.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# Literal cloud-metadata hostnames (the IP-range checks already cover
# 169.254.169.254, but proxies sometimes resolve these names locally).
_METADATA_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.azure.com",
    "metadata.ec2.internal",
    "instance-data.ec2.internal",
})


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for an agent to fetch."""


def _addr_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check(url: str) -> None:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeUrlError(f"only http/https URLs may be fetched (got {scheme or 'none'!r})")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host in _METADATA_HOSTS:
        raise UnsafeUrlError(f"host {host!r} targets a cloud-metadata endpoint")

    try:
        addrs = [ipaddress.ip_address(host)]  # IP literal
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"cannot resolve host {host!r}: {exc}") from exc
        addrs = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]

    for addr in addrs:
        if _addr_blocked(addr):
            raise UnsafeUrlError(
                f"host {host!r} resolves to a non-public address ({addr})"
            )


async def ensure_fetchable_url(url: str) -> None:
    """Raise `UnsafeUrlError` if `url` is unsafe to fetch. Resolves DNS in a
    thread so the event loop isn't blocked."""
    await asyncio.to_thread(_check, url)
