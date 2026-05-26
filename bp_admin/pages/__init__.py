"""bp_admin.pages — page-handler modules, one per top-level admin section.

Each module exports a `router` (FastAPI APIRouter) that the main app
mounts. Page handlers receive the upstream client, the templating
helper, and the user's session via dependencies wired in `app.py`.
"""
