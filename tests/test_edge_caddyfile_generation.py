"""Edge Caddyfile generation (EDGE_MODE = domain | ip | both).

`scripts/prod.sh` no longer env-templates a single static Caddyfile: it GENERATES
`deploy/Caddyfile.generated` via `scripts/render-caddyfile.sh` for the chosen
EDGE_MODE and points `CADDYFILE_HOST_PATH` (compose's caddy bind mount) at it.
Generating is what lets a single deploy serve a domain AND a bare IP at once,
each with the right TLS — which `{$VAR}` placeholders in one site block can't.

These tests drive the generator headlessly (it's pure env-in / Caddyfile-out)
and pin the wiring in compose / prod.sh / .gitignore.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_RENDER = _REPO / "scripts" / "render-caddyfile.sh"
_PROD = (_REPO / "scripts" / "prod.sh").read_text()
_COMPOSE = (_REPO / "docker-compose.prod.yml").read_text()
_GITIGNORE = (_REPO / ".gitignore").read_text()


def _render(**env: str) -> str:
    """Run render-caddyfile.sh with the given env; return stdout (raises on error)."""
    res = subprocess.run(
        ["bash", str(_RENDER)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"render failed ({res.returncode}): {res.stderr}"
    return res.stdout


def _render_fail(**env: str) -> str:
    """Run render-caddyfile.sh expecting a non-zero exit; return stderr."""
    res = subprocess.run(
        ["bash", str(_RENDER)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, f"expected failure, got stdout:\n{res.stdout}"
    return res.stderr


# --- domain mode -----------------------------------------------------------


def test_domain_https_emits_two_host_blocks_no_ip_artifacts() -> None:
    out = _render(
        EDGE_MODE="domain",
        PUBLIC_DOMAIN="bp.example.com",
        WEBAPP_DOMAIN="app.example.com",
        EDGE_SCHEME="https",
    )
    assert "https://bp.example.com {" in out
    assert "https://app.example.com {" in out
    assert "reverse_proxy @router router:8000" in out
    assert "reverse_proxy webapp:8002" in out
    assert "redir / /admin/login" in out
    # No bare-IP machinery for a pure domain deploy.
    assert "default_sni" not in out
    assert "tls internal" not in out


def test_domain_http_is_plain_http_origin_for_upstream_tls() -> None:
    # EDGE_SCHEME=http → Caddy serves plain HTTP (TLS terminated upstream); the
    # http:// site address is itself what disables Caddy's automatic HTTPS.
    out = _render(
        EDGE_MODE="domain",
        PUBLIC_DOMAIN="bp.example.com",
        WEBAPP_DOMAIN="app.example.com",
        EDGE_SCHEME="http",
    )
    assert "http://bp.example.com {" in out
    assert "http://app.example.com {" in out
    assert "https://bp.example.com" not in out


# --- ip mode ---------------------------------------------------------------


def test_ip_mode_is_always_https_with_internal_ca_and_default_sni() -> None:
    out = _render(EDGE_MODE="ip", PUBLIC_IP="192.168.1.50", WEBAPP_HTTPS_PORT="8443")
    # Router on https://<ip>, webapp on its own https port.
    assert "https://192.168.1.50 {" in out
    assert "https://192.168.1.50:8443 {" in out
    # internal CA (Let's Encrypt won't issue an IP cert) on BOTH IP blocks.
    assert out.count("tls internal") == 2
    # global default_sni (the no-SNI IP client needs a named default cert).
    assert "default_sni 192.168.1.50" in out
    # No domain http/https artifacts.
    assert "http://" not in out


def test_ip_mode_ignores_edge_scheme_and_never_serves_http() -> None:
    # Even if EDGE_SCHEME=http leaks in, a bare IP is always https.
    out = _render(
        EDGE_MODE="ip", PUBLIC_IP="10.0.0.5", WEBAPP_HTTPS_PORT="9443",
        EDGE_SCHEME="http",
    )
    assert "https://10.0.0.5 {" in out
    assert "https://10.0.0.5:9443 {" in out
    assert "http://10.0.0.5" not in out


# --- both mode -------------------------------------------------------------


def test_both_mode_serves_domain_and_ip_each_with_own_tls() -> None:
    out = _render(
        EDGE_MODE="both",
        PUBLIC_DOMAIN="bp.example.com",
        WEBAPP_DOMAIN="app.example.com",
        EDGE_SCHEME="http",            # domain via tunnel
        PUBLIC_IP="192.168.1.50",
        WEBAPP_HTTPS_PORT="8443",
    )
    # Domain served over plain http (tunnel terminates TLS)...
    assert "http://bp.example.com {" in out
    assert "http://app.example.com {" in out
    # ...while the bare IP is https + internal CA + default_sni.
    assert "https://192.168.1.50 {" in out
    assert "https://192.168.1.50:8443 {" in out
    assert "default_sni 192.168.1.50" in out
    assert out.count("tls internal") == 2
    # Two routers + two webapps (one identity each).
    assert out.count("reverse_proxy @router router:8000") == 2
    assert out.count("reverse_proxy webapp:8002") == 2


# --- validation ------------------------------------------------------------


def test_invalid_mode_rejected() -> None:
    assert "invalid EDGE_MODE" in _render_fail(EDGE_MODE="bogus")


def test_ip_mode_requires_ipv4_literal() -> None:
    assert "PUBLIC_IP" in _render_fail(EDGE_MODE="ip", PUBLIC_IP="not-an-ip")


def test_ip_webapp_port_must_differ_from_443() -> None:
    err = _render_fail(EDGE_MODE="ip", PUBLIC_IP="192.168.1.50", WEBAPP_HTTPS_PORT="443")
    assert "443" in err


def test_domain_mode_requires_both_hostnames() -> None:
    assert "WEBAPP_DOMAIN" in _render_fail(
        EDGE_MODE="domain", PUBLIC_DOMAIN="bp.example.com"
    )


@pytest.mark.parametrize("scheme", ["ftp", "wss", "tls"])
def test_domain_scheme_must_be_http_or_https(scheme: str) -> None:
    assert "EDGE_SCHEME" in _render_fail(
        EDGE_MODE="domain",
        PUBLIC_DOMAIN="bp.example.com",
        WEBAPP_DOMAIN="app.example.com",
        EDGE_SCHEME=scheme,
    )


# --- wiring: compose / prod.sh / gitignore ---------------------------------


def test_compose_caddy_mounts_caddyfile_host_path_with_committed_fallback() -> None:
    assert "${CADDYFILE_HOST_PATH:-./deploy/Caddyfile}:/etc/caddy/Caddyfile:ro" in _COMPOSE


def test_compose_router_public_url_is_overridable_with_legacy_fallback() -> None:
    # prod.sh sets ROUTER_PUBLIC_URL explicitly (domain or IP); the fallback
    # keeps a legacy env / bare compose up working off PUBLIC_DOMAIN.
    assert "ROUTER_PUBLIC_URL: ${ROUTER_PUBLIC_URL:-https://${PUBLIC_DOMAIN:-localhost}}" in _COMPOSE


def test_prod_sh_always_generates_caddyfile_and_writes_wiring_vars() -> None:
    # Single code path: build_env always invokes the generator into the
    # gitignored target and records the mode + mount path in the env file.
    assert "scripts/render-caddyfile.sh > \"$CADDYFILE_GENERATED\"" in _PROD
    assert 'CADDYFILE_GENERATED="./deploy/Caddyfile.generated"' in _PROD
    assert 'echo "EDGE_MODE=$EDGE_MODE"' in _PROD
    assert 'echo "CADDYFILE_HOST_PATH=$CADDYFILE_GENERATED"' in _PROD


def test_generated_caddyfile_is_gitignored() -> None:
    assert "deploy/Caddyfile.generated" in _GITIGNORE
