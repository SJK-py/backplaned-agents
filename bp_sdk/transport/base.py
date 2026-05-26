"""bp_sdk.transport.base — Transport Protocol."""

from __future__ import annotations

from typing import Any, Protocol

from bp_protocol.frames import Frame


class Transport(Protocol):
    """Layer between the framed protocol and the wire.

    Implementations: WebSocketTransport, InProcessTransport. Both are
    full-duplex and frame-oriented. Reconnection, heartbeat, and resume
    are implementation responsibilities — not surfaced to callers.
    """

    async def send(self, frame: Frame) -> None:
        ...

    async def recv(self) -> Frame:
        ...

    async def close(self) -> None:
        ...

    def update_catalog(self, catalog: dict[str, dict[str, Any]]) -> None:
        """Replace the cached `available_destinations`. Called by the
        dispatcher when the router pushes a `CatalogUpdateFrame`."""
        ...

    @property
    def is_connected(self) -> bool:
        ...
