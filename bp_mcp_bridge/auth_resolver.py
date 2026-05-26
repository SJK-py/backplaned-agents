"""Resolve `auth_value_ref` strings to concrete credentials.

Two schemes:

  * `env://VAR_NAME` — read from the bridge process's environment.
    Required for Phase 10c; how operators get credentials into the
    bridge.
  * `secret://path/to/secret` — would consult a secret-store
    backend (Vault, AWS Secrets Manager, etc.). NOT implemented
    in Phase 10c; raises `NotImplementedError` with a pointer to
    the env:// fallback. Operators wanting Vault/Secrets-Manager
    integration today run a sidecar that interpolates the secret
    into an env var BEFORE the bridge starts.

Storing the literal `auth_value` in the DB is deliberately
refused (PR #117's Pydantic validator on `auth_value_ref`). This
module is the only place credentials become plaintext inside the
bridge process — kept tight so audit / static-analysis can pin
the boundary.
"""

from __future__ import annotations

import os


class AuthResolveError(RuntimeError):
    """A configured `auth_value_ref` can't be resolved to a value."""


def resolve_auth_value(ref: str | None) -> str | None:
    """Resolve `env://VAR_NAME` to the env var's value.

    Returns None when `ref` is None or empty (auth_kind=none case).
    Raises `AuthResolveError` for: malformed scheme, missing env
    var, unsupported scheme. Phase 10c supports `env://` only.

    The returned string is the LITERAL secret. The caller (bridge
    runtime) is responsible for not logging it, not stashing it on
    any tracked object, not echoing it to error messages."""
    if not ref:
        return None
    if ref.startswith("env://"):
        var = ref[len("env://"):]
        if not var:
            raise AuthResolveError(
                f"auth_value_ref {ref!r} has empty env var name"
            )
        try:
            return os.environ[var]
        except KeyError as exc:
            raise AuthResolveError(
                f"env var {var!r} (referenced by {ref!r}) is not set"
            ) from exc
    if ref.startswith("secret://"):
        raise NotImplementedError(
            f"secret:// auth refs require a secret-store sidecar in "
            f"Phase 10c (got {ref!r}). Workaround: have your sidecar "
            "interpolate the secret into an env var, then point this "
            "field at env://VAR_NAME. Native secret-store support is "
            "tracked for a future phase."
        )
    raise AuthResolveError(
        f"auth_value_ref {ref!r} must use the env:// scheme "
        "(secret:// is reserved for a future phase)"
    )
