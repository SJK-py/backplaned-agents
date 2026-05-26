"""bp_admin.asgi_utils — Shared ASGI helpers for mount-aware
middleware and handlers.

The admin app supports two deployment shapes:
  - Standalone via the `bp-admin` console script (no mount prefix).
  - Mounted under the router via `parent.mount("/admin", admin_app)`.

ASGI's `scope["root_path"]` carries the mount prefix, but
`request.url.path` does NOT strip it — so any code that compares
the path against a hard-coded literal (`PUBLIC_PATHS`,
`EXEMPT_PATHS`, route guards in `_safe_next`, etc.) needs to
strip the prefix first or the comparison silently fails in one
of the two modes. This module is the single source of truth so
every middleware / handler stays in lockstep.

The function used to live as a private `_strip_root_path` in
`bp_admin/auth.py`. Promoted here so `bp_admin/csrf.py` and
`bp_admin/pages/auth_pages.py` can share the same implementation
— the CSRF middleware needed the same fix the auth middleware
used, and `_safe_next` had hard-coded `/admin/` paths that broke
the standalone mode.
"""

from __future__ import annotations

from starlette.requests import Request


def strip_root_path(request: Request) -> str:
    """Return `request.url.path` with the ASGI `root_path` mount
    prefix removed.

    Examples:
      - Standalone (`root_path=""`): `/login` → `/login`.
      - Mounted under `/admin`: `/admin/login` → `/login`.
      - Arbitrary mount: `/foo/bar/login` with `root_path="/foo/bar"`
        → `/login`.
      - Path is the prefix itself: `/admin` with `root_path="/admin"`
        → `/` (so callers don't have to special-case empty).

    Promoted from a private `_strip_root_path` in `bp_admin/auth.py`
    so both auth + CSRF middlewares share one implementation.
    A future middleware that needs to compare `request.url.path`
    against unprefixed literals MUST use this helper.
    """
    root_path = request.scope.get("root_path", "")
    path = request.url.path
    if root_path and path.startswith(root_path):
        return path[len(root_path):] or "/"
    return path


def root_path(request: Request) -> str:
    """Return the ASGI `root_path` for this request, or `""` when
    unmounted. Use this when building absolute paths back into
    the app (redirect targets, `next=` query params, …) so the
    URL stays valid in both standalone and mounted modes."""
    return request.scope.get("root_path", "")
