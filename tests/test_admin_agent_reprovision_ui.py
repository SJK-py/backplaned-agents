"""Admin webUI: the "Reset & reprovision" button on the agent-detail page.

One-click recovery for a stuck agent — reset to pending + mint a fresh
invitation, revealing the token once. Source-pins the template button (shown
for active/suspended/pending, not for terminal `removed`), the reveal page,
and the BFF route that calls the upstream `/reprovision` endpoint.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from bp_admin.pages import agents as agents_page


def _tpl(name: str) -> str:
    return (
        Path(__file__).parent.parent / "bp_admin" / "templates" / "agents" / name
    ).read_text()


# ---------------------------------------------------------------------------
# Detail-page button
# ---------------------------------------------------------------------------


def test_reprovision_button_posts_to_reprovision_route() -> None:
    body = _tpl("detail.html")
    assert 'action="/admin/agents/{{ agent.agent_id }}/reprovision"' in body
    assert "Reset &amp; reprovision" in body
    # CSRF-protected like the other mutating actions.
    assert '{% include "_partials/csrf.html" %}' in body


def test_reprovision_button_offered_for_recoverable_states_not_removed() -> None:
    """active / suspended / pending can be reprovisioned; `removed` is
    terminal and must NOT offer it."""
    body = _tpl("detail.html")
    # Reused via a macro, invoked under each non-terminal branch.
    assert "{% macro reprovision_button() %}" in body
    assert body.count("{{ reprovision_button() }}") == 3
    # The `removed` branch stays a dead end (no reprovision call between it
    # and the next branch / endif).
    removed_idx = body.index('agent.status == "removed"')
    pending_idx = body.index('agent.status == "pending"')
    assert "reprovision_button()" not in body[removed_idx:pending_idx]


# ---------------------------------------------------------------------------
# One-time token reveal
# ---------------------------------------------------------------------------


def test_reveal_page_shows_token_once_and_links_back() -> None:
    body = _tpl("reprovisioned.html")
    assert "{{ invitation_token }}" in body
    assert "shown <em>once</em>" in body
    assert "AGENT_INVITATION_TOKEN" in body
    assert 'href="/admin/agents/{{ agent_id }}"' in body
    # Surfaces whether the service principal is re-provisioned.
    assert "provisions_service_user" in body


# ---------------------------------------------------------------------------
# BFF route
# ---------------------------------------------------------------------------


def test_route_calls_upstream_and_renders_reveal() -> None:
    src = inspect.getsource(agents_page.reprovision_agent)
    assert 'f"/agents/{agent_id}/reprovision"' in src
    assert '"agents/reprovisioned.html"' in src
    assert '"invitation_token"' in src
    # Upstream failure flashes back to the detail page rather than 500ing.
    assert "redirect_with_flash" in src
