"""WS `SocketRegistry.detach` identity-checks before popping.

R6 third-pass review (CRITICAL): `detach(agent_id, into_resume=...)`
popped `_live[agent_id]` without verifying which entry was there.
The bug surface:

  1. Agent reconnects on a fresh socket → `attach(new)`:
     `_live[a]` is now `new`; old `previous` is returned.
  2. The old socket's `_send_loop` / `_recv_loop` finishes
     unwinding and `_on_disconnect` runs.
  3. `_on_disconnect` calls `detach(a, into_resume=True)`.
  4. Without identity check: pops `_live[a]` — which is `new`,
     not the old entry — and parks `new` into `_resume`.

Result: the brand-new live socket is silently evicted from the
live registry. `delivery.py:38` returns `AgentNotConnected` for
every inbound frame. The agent looks connected to itself (WS is
open) but the router considers it offline.

R6 fix: `detach(... expected=entry)` only pops if `_live[a] is
entry`. Same identity-check pattern as `expire_resume`.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest


def _make_socket_entry(agent_id: str) -> object:
    """Build a minimal `SocketEntry` for registry-level tests."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketEntry

    ws = MagicMock()
    return SocketEntry(
        agent_id=agent_id,
        websocket=ws,
        session_token="tok-" + agent_id,
    )


def test_detach_without_expected_pops_whatever_is_live() -> None:
    """Baseline: with no `expected` argument, `detach` keeps the
    pre-R6 behaviour for callers that don't (yet) pass identity.
    Used by tests / future tooling that explicitly want
    "discard whatever is here"."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketRegistry

    reg = SocketRegistry()
    entry = _make_socket_entry("agt_a")
    asyncio.run(reg.attach(entry))

    out = asyncio.run(reg.detach("agt_a", into_resume=False))
    assert out is entry
    assert reg.get("agt_a") is None


def test_detach_with_matching_expected_pops() -> None:
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketRegistry

    reg = SocketRegistry()
    entry = _make_socket_entry("agt_a")
    asyncio.run(reg.attach(entry))

    out = asyncio.run(
        reg.detach("agt_a", into_resume=True, expected=entry)
    )
    assert out is entry
    # And parked into resume.
    assert reg._resume["agt_a"] is entry


def test_detach_with_mismatched_expected_is_noop() -> None:
    """The CRITICAL fix: when `_live[agent_id]` has been replaced
    by a different entry (the supersede case), `detach(expected=old)`
    must NOT pop the new entry."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketRegistry

    reg = SocketRegistry()
    old = _make_socket_entry("agt_a")
    new = _make_socket_entry("agt_a")
    asyncio.run(reg.attach(old))
    # Simulate supersede: a new connection replaced the live entry.
    asyncio.run(reg.attach(new))
    assert reg.get("agt_a") is new

    # Old socket's `_on_disconnect` runs `detach(expected=old)`.
    out = asyncio.run(
        reg.detach("agt_a", into_resume=True, expected=old)
    )
    # No pop, no resume parking.
    assert out is None
    # New socket is STILL in the live registry — the bug we
    # closed would have evicted it.
    assert reg.get("agt_a") is new
    # And not parked into resume.
    assert "agt_a" not in reg._resume


def test_supersede_then_old_disconnect_preserves_new_live_socket() -> None:
    """End-to-end of the bug: simulate the full lifecycle that
    pre-R6 would have evicted the new socket."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketRegistry

    reg = SocketRegistry()

    # Connect agt_a on socket A.
    sock_a = _make_socket_entry("agt_a")
    asyncio.run(reg.attach(sock_a))
    assert reg.get("agt_a") is sock_a

    # Agent reconnects on socket B; A is superseded.
    sock_b = _make_socket_entry("agt_a")
    previous = asyncio.run(reg.attach(sock_b))
    assert previous is sock_a
    assert reg.get("agt_a") is sock_b

    # Socket A's `_on_disconnect` now runs, identity-checked.
    parked = asyncio.run(
        reg.detach("agt_a", into_resume=True, expected=sock_a)
    )
    # detach was a no-op — A is no longer in `_live`.
    assert parked is None

    # CRITICAL invariant: B is still the live socket. A regression
    # that drops the identity check fails this assertion.
    assert reg.get("agt_a") is sock_b
    # And B is NOT in resume (resume is for graceful drop, not
    # supersede victim).
    assert "agt_a" not in reg._resume


def test_on_disconnect_source_pin_passes_expected() -> None:
    """Source pin: `_on_disconnect` always passes `expected=entry`
    to `detach`. A future refactor that drops the kwarg reintroduces
    the CRITICAL bug."""
    pytest.importorskip("fastapi")
    from bp_router import ws_hub

    src = inspect.getsource(ws_hub._on_disconnect)
    assert "expected=entry" in src


def test_detach_signature_has_expected_kwarg() -> None:
    """API pin: `detach` MUST expose the `expected` keyword. A
    callable signature regression that drops it would be silently
    masked by the call-site source pin above."""
    pytest.importorskip("fastapi")
    from bp_router.ws_hub import SocketRegistry

    sig = inspect.signature(SocketRegistry.detach)
    assert "expected" in sig.parameters
    # And it's keyword-only (kw-only marker via the `*,` in the def).
    p = sig.parameters["expected"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
