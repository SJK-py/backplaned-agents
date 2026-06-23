"""Rate-limit bucket-key prefixes.

Centralises the string constants for every per-IP / per-user /
per-target / per-agent bucket the router consumes from. Keeps
the bucket-key shape consistent across call sites (no typos in
the `:` separator, no drift between the rate-limit consume and
the audit-log denial event) and gives operators one place to
grep for "all rate-limited operations".

The shape across all sites is `<prefix>:<axis>:<id>` (e.g.
`login:ip:1.2.3.4`) EXCEPT `agent_info_update`, which is keyed
directly by `agent_id` without an axis segment because there's
only ever one axis for that endpoint.
"""

from __future__ import annotations

# HTTP auth endpoints — per-IP buckets.
BUCKET_LOGIN = "login"
BUCKET_REFRESH = "refresh"
BUCKET_RESET_PASSWORD = "reset_password"
BUCKET_REGISTRATION_WEB = "registration_web"
BUCKET_OIDC = "oidc"

# Authenticated endpoints — per-user buckets.
BUCKET_CHANGE_PASSWORD = "change_password"
BUCKET_LINK_TOKEN_MINT = "link_token_mint"

# Per-target mint endpoints — caller writes to a bucket scoped by
# the user being minted-for, not the caller.
BUCKET_PASSWORD_RESET_MINT = "password_reset_mint"
BUCKET_SERVICE_MINT_REFRESH_TOKEN = "service_mint_refresh_token"

# WS / frame endpoints — per-agent.
BUCKET_AGENT_INFO_UPDATE = "agent_info_update"
BUCKET_FILE_UPLOAD_REQUEST = "file_upload_request"

__all__ = [
    "BUCKET_LOGIN",
    "BUCKET_REFRESH",
    "BUCKET_RESET_PASSWORD",
    "BUCKET_REGISTRATION_WEB",
    "BUCKET_OIDC",
    "BUCKET_CHANGE_PASSWORD",
    "BUCKET_LINK_TOKEN_MINT",
    "BUCKET_PASSWORD_RESET_MINT",
    "BUCKET_SERVICE_MINT_REFRESH_TOKEN",
    "BUCKET_AGENT_INFO_UPDATE",
    "BUCKET_FILE_UPLOAD_REQUEST",
]
