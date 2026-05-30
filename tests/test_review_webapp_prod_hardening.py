"""Second-pass webapp prod hardening (PR D).

H1 — the webapp served its stylesheet from the Tailwind Play CDN in prod (an
     unpinned third-party runtime dep on an authenticated app + a permissive
     CSP the in-page JIT forces). Prod now requires the self-hosted built CSS
     (validator), the prod compose sets WEBAPP_USE_BUILT_CSS=true, and
     Dockerfile.suite builds /static/tailwind.css into the image.
H2 — WEBAPP_SESSION_SECRET had no strength/prod validator (the router's JWT
     secret does). Now: ≥32-byte floor + prod rejects dev placeholders,
     insecure cookies, and the CDN.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import SecretStr, ValidationError

from bp_agents.agents.webapp.config import WebappConfig

_REPO = pathlib.Path(__file__).resolve().parent.parent
_DOCKERFILE = (_REPO / "Dockerfile.suite").read_text()
_COMPOSE = (_REPO / "docker-compose.prod.yml").read_text()


def _cfg(**kw):  # type: ignore[no-untyped-def]
    kw.setdefault("session_secret", SecretStr("x" * 32))
    return WebappConfig(_env_file=None, **kw)


# --- H2: session_secret strength --------------------------------------------


def test_short_session_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        _cfg(session_secret=SecretStr("too-short"))


def test_32_byte_secret_accepted_in_dev() -> None:
    cfg = _cfg()  # deployment_env defaults to dev
    assert cfg.deployment_env == "dev"
    # Dev keeps the CDN + may use an http cookie — no prod constraints.
    assert cfg.use_built_css is False


# --- H1 + H2: prod fail-closed checks ---------------------------------------


def test_prod_requires_built_css() -> None:
    with pytest.raises(ValidationError, match="USE_BUILT_CSS"):
        _cfg(deployment_env="prod", use_built_css=False)


def test_prod_requires_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        _cfg(
            deployment_env="prod",
            use_built_css=True,
            session_cookie_secure=False,
        )


@pytest.mark.parametrize(
    "placeholder",
    ["dev-insecure-change-me-aaaaaaaaaaaaaaaaaa", "CHANGE-ME-please-32-bytes-xxxxxxxx"],
)
def test_prod_rejects_placeholder_secret(placeholder: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _cfg(
            session_secret=SecretStr(placeholder),
            deployment_env="prod",
            use_built_css=True,
        )


def test_prod_accepts_well_configured() -> None:
    cfg = _cfg(
        session_secret=SecretStr("Zr9" + "q" * 40),
        deployment_env="prod",
        use_built_css=True,
        session_cookie_secure=True,
    )
    assert cfg.use_built_css is True and cfg.session_cookie_secure is True


# --- H1: image builds the CSS, compose enables it ---------------------------


def test_dockerfile_builds_tailwind_css() -> None:
    assert "AS css-build" in _DOCKERFILE
    assert "tailwindcss@3.4" in _DOCKERFILE  # pinned v3 (config is v3-style)
    assert "tailwind.css" in _DOCKERFILE
    # The built artifact is copied into the runtime image.
    assert "COPY --from=css-build" in _DOCKERFILE


def test_prod_compose_enables_built_css() -> None:
    assert 'WEBAPP_USE_BUILT_CSS: "true"' in _COMPOSE
