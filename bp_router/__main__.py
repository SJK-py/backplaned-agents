"""bp_router entry point — `python -m bp_router`.

Starts uvicorn against the FastAPI app produced by `create_app()`.
For production use a process supervisor (systemd, kubernetes) and
optionally `gunicorn -k uvicorn.workers.UvicornWorker` for multi-worker.
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from bp_router.app import create_app
    from bp_router.settings import load_settings

    settings = load_settings()
    uvicorn.run(
        create_app,
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # we configure logging ourselves
        # Enforce the per-frame size cap at the WebSocket protocol layer
        # so an oversized frame is rejected BEFORE uvicorn assembles the
        # full message in memory. The post-receive check in
        # `bp_router.ws_hub._recv_loop` stays as defence-in-depth, but
        # this is the actual choke point: without it, a peer can
        # allocate `max_payload_bytes`-many bytes (or more, up to
        # uvicorn's 16 MiB default) before any router code runs
        # Operators running the router under
        # `gunicorn -k uvicorn.workers.UvicornWorker` must set the same
        # cap via that worker's `ws_max_size` config.
        ws_max_size=settings.max_payload_bytes,
    )


if __name__ == "__main__":
    main()
