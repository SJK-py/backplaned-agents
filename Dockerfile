# Backplaned router image.
#
# Build (from repo root):   docker build -t backplaned-router:latest .
# Run:                      bp-router  (uvicorn + create_app factory)
#
# Migrations are intentionally NOT run on container start — run
# `alembic upgrade head` once as a separate step (see the `migrate`
# one-shot service in docker-compose.prod.yml) so scaling the router
# never races the schema.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # bp-router binds settings.bind_host:bind_port; inside a container we
    # must listen on all interfaces.
    ROUTER_BIND_HOST=0.0.0.0 \
    ROUTER_BIND_PORT=8000

# Non-root runtime user.
RUN useradd --create-home --uid 10001 bp
WORKDIR /app

# asyncpg / argon2-cffi ship manylinux wheels, so python:3.12-slim needs
# no compiler. If a wheel is unavailable for your arch, add a builder
# stage with build-essential and copy the resulting site-packages.
COPY . .
RUN pip install ".[router,storage-s3,admin,llm-gemini]"

USER bp
EXPOSE 8000

# Liveness only (no DB/Redis dependency) — readiness is /readyz.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["bp-router"]
