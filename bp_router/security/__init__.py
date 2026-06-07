"""bp_router.security — Auth, secrets, password hashing.

See `docs/backplaned/security.md`.
"""

from bp_router.security.jwt import (
    AgentPrincipal,
    SessionPrincipal,
    TokenError,
    issue_agent_token,
    issue_session_token,
    require_admin,
    require_authenticated,
    require_service,
    require_tier,
    revoke_jti,
    verify_agent_token,
    verify_token,
)
from bp_router.security.passwords import hash_password, verify_password
from bp_router.security.secrets import resolve_secret_ref

__all__ = [
    "AgentPrincipal",
    "SessionPrincipal",
    "TokenError",
    "hash_password",
    "issue_agent_token",
    "issue_session_token",
    "require_admin",
    "require_authenticated",
    "require_service",
    "require_tier",
    "resolve_secret_ref",
    "revoke_jti",
    "verify_agent_token",
    "verify_password",
    "verify_token",
]
