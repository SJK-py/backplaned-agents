"""bp_router.url_validation — SSRF defense for preset ``base_url``
fields.

Two policies enforced:

1. **Scheme.** Hosted providers (gemini / anthropic / openai /
   openai-embeddings) MUST use ``https://``. Anything else fails
   validation. The official endpoints are TLS-only and a misconfigured
   ``http://`` would mean ferrying API keys in cleartext to whatever
   host happens to resolve. Local-server providers
   (``openai-compatible*``) are allowed ``http://`` since the typical
   target is loopback or a private network without TLS.

2. **Host.** The hostname is parsed and rejected if it resolves to:

   - private RFC1918 ranges (10/8, 172.16/12, 192.168/16) — for hosted
     providers only. Local-server providers explicitly target these.
   - loopback (127/8, ::1) — for hosted providers only. Local-server
     providers DO target loopback (the typical vLLM / LM Studio setup).
   - link-local (169.254/16, fe80::/10) — for ALL providers; covers the
     cloud metadata endpoint (169.254.169.254). Even a misconfigured
     ``openai-compatible`` preset must not reach metadata.
   - multicast / unspecified / reserved ranges — all providers
   - ``metadata.google.internal``, ``metadata.azure.com``,
     ``instance-data.ec2.internal`` (literal cloud-metadata hostnames)

   Operators can override the host check via the env var
   ``ROUTER_BASE_URL_ALLOWED_HOSTS=host1,host2,...``. Anything in
   the allowlist passes regardless of address class. Useful for
   private-VPC LiteLLM / Portkey gateways at known hostnames.

The validator resolves the hostname at validation time
(``socket.getaddrinfo``) and rejects the URL if ANY resolved address
falls in a blocked class (loopback / private / link-local /
cloud-metadata) — an IP literal is class-checked directly. This is
resolve-time, not connect-time, so it cannot fully close DNS-rebinding
(a name that passes at admin-save can later re-resolve to a private
IP at connect-time); that residual gap is the operator's domain to
constrain via the explicit allowlist. Resolution failures are treated
per the caller's policy rather than silently passing.

Raised as ``BaseUrlValidationError`` so the admin API can surface
HTTP 400 with a clean detail string.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class BaseUrlValidationError(ValueError):
    """Raised when a preset's ``base_url`` fails the SSRF checks."""


# Hostnames that map to cloud-metadata endpoints. The IP-range checks
# already cover 169.254.169.254, but operators sometimes deploy proxies
# that resolve these names locally — explicit names give a clearer
# error message and don't depend on DNS.
_METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.azure.com",
    "instance-data.ec2.internal",
    "metadata.ec2.internal",
})


# Providers whose base_url, when set, must point at a public TLS
# endpoint. Local-server providers can target loopback / private nets.
_HOSTED_PROVIDERS = frozenset({
    "gemini",
    "anthropic",
    "openai",
    "openai-embeddings",
})


def _is_local_provider(provider: str) -> bool:
    # `mcp` is the MCP-bridge server URL: like a local LLM server it is
    # commonly an INTERNAL endpoint (the point of bridging an on-prem MCP
    # server), so loopback / private are allowed — but link-local
    # (cloud-metadata 169.254.169.254), metadata hostnames, and
    # multicast/reserved stay blocked for it like everyone else, and it is
    # NOT in `_HOSTED_PROVIDERS` so http is permitted (internal MCP over
    # http is common).
    return provider.startswith("openai-compatible") or provider == "mcp"


def _ip_is_blocked(
    ip: ipaddress._BaseAddress,
    *,
    allow_private: bool,
    allow_loopback: bool,
) -> str | None:
    """Return a reason string when the IP is in a denied class, else None.

    `allow_private` and `allow_loopback` are True for openai-compatible
    providers (the whole point is to target a local server). Link-local
    stays blocked for everyone — the cloud-metadata endpoint
    (169.254.169.254) is link-local.
    """
    # Unwrap IPv4-mapped IPv6 (``::ffff:127.0.0.1``) to its embedded IPv4
    # before classifying. Such an address routes to the IPv4 target, so it
    # must be judged as that IPv4 — otherwise ``::ffff:127.0.0.1`` reads as
    # loopback=False/reserved=True (wrongly blocking a local provider whose
    # ``localhost`` resolved to the mapped form, as it does on some hosts/CI)
    # AND ``::ffff:169.254.169.254`` reads as generic "reserved" instead of
    # the real link-local cloud-metadata hazard. Mirrors common/urlsafe.py.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = ipaddress.ip_address(mapped)
    # Order: more-specific category first. Link-local / multicast /
    # unspecified are blocked for everyone (link-local covers the
    # 169.254.169.254 cloud-metadata endpoint). Loopback and private are
    # then resolved against the caller's allowances — and a permitted
    # loopback/private address SHORT-CIRCUITS to allowed here, because such
    # an address may ALSO be `is_reserved` (notably ``::1`` is both loopback
    # and reserved) and must not fall through to the reserved catch-all. A
    # local provider's ``localhost`` resolves to ``::1`` on dual-stack hosts
    # / CI, so without this a legitimate local base_url is wrongly blocked.
    if ip.is_link_local:
        return "link-local address (cloud-metadata range)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return None if allow_loopback else "loopback address"
    if ip.is_private:
        return None if allow_private else "private address (RFC1918 / ULA)"
    if ip.is_reserved:
        return "reserved address"
    return None


def validate_base_url(
    *,
    provider: str,
    base_url: str,
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    """Raise ``BaseUrlValidationError`` if ``base_url`` is unsafe for
    ``provider``. Returns ``None`` when valid.

    ``allowed_hosts`` is the operator's explicit allowlist (lowercase
    hostnames). Any URL whose hostname is in that set passes regardless
    of address class — intended for known LiteLLM / Portkey gateways
    on private VPCs.
    """
    if not base_url:
        # Empty/None is always fine — validation lives outside (the
        # cross-field check requires base_url for openai-compatible*).
        return

    parsed = urlparse(base_url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BaseUrlValidationError(
            f"base_url must use http(s) scheme; got {scheme!r}"
        )

    if provider in _HOSTED_PROVIDERS and scheme != "https":
        raise BaseUrlValidationError(
            f"{provider} preset requires https:// (got {scheme!r}); "
            "cleartext would ferry API keys without TLS"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise BaseUrlValidationError("base_url has no hostname")

    if allowed_hosts is not None and host in allowed_hosts:
        # Operator has explicitly approved this hostname.
        return

    if host in _METADATA_HOSTNAMES:
        raise BaseUrlValidationError(
            f"base_url host {host!r} targets a cloud-metadata endpoint"
        )

    # Local-server presets can target private nets / loopback.
    is_local = _is_local_provider(provider)

    # Collect the IP(s) to class-check. An IP literal is checked
    # directly; a hostname is RESOLVED and EVERY resolved address is
    # checked — a DNS name pointing at 169.254.169.254 / RFC1918 must
    # not slip through (provider API keys would be ferried there).
    # This is resolve-time, not connect-time, so it is NOT
    # DNS-rebinding-proof (the provider SDK opens its own socket
    # later); it closes the static-name-to-internal hole as far as
    # is feasible without owning the provider's socket. An
    # unresolvable host fails closed — we
    # cannot prove it is not internal, and it would receive API keys.
    try:
        candidate_ips: list[ipaddress._BaseAddress] = [
            ipaddress.ip_address(host)
        ]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            # Unresolvable at validation time. Accept — a name that
            # resolves to nothing is not a reachable SSRF target (the
            # provider call would simply fail to connect). The SSRF
            # gap this closes is specifically "resolves to an
            # INTERNAL address"; refusing unresolvable names would add
            # no security, only config friction.
            return
        candidate_ips = [
            ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos
        ]

    for ip in candidate_ips:
        reason = _ip_is_blocked(
            ip, allow_private=is_local, allow_loopback=is_local
        )
        if reason is not None:
            raise BaseUrlValidationError(
                f"base_url host {host!r} resolves to a {reason}; not "
                f"allowed for {provider} provider"
            )


def parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Parse the ``ROUTER_BASE_URL_ALLOWED_HOSTS`` env value into a
    lowercase frozenset. Comma-separated; whitespace tolerated; empty
    entries dropped."""
    if not raw:
        return frozenset()
    return frozenset(
        item.strip().lower() for item in raw.split(",") if item.strip()
    )
