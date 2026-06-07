"""bp_admin — Admin web UI for the bp_router.

A thin FastAPI BFF served on `/admin` (mounted by the router by default).
HTMX + Jinja2 + Alpine.js + SortableJS via CDN; no JS build pipeline.

The admin UI talks to the router's existing JSON API (`/v1/admin/*`,
`/v1/auth/*`) over HTTP — never imports router internals. This keeps
the boundary clean for a future split into a separate process.

See `docs/backplaned/sdk/...` (TBD) and `docs/backplaned/admin-ui.md` (TBD) for the full
design once the implementation stabilises.
"""

__version__ = "0.1.0"
