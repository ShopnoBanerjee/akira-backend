# AKIRA Ops Suite API - production image.
#
# One process, one worker. The in-process scheduler (app/jobs/scheduler.py)
# must run in exactly one place, and the rate limiter and identity cache are
# per-process; a second worker or replica would silently halve one and double
# the other. Scale the machine, not the count.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# uv from PyPI rather than COPY --from=ghcr.io/astral-sh/uv: one registry
# fewer for the build to depend on. Pinned to the version CI uses.
RUN pip install --no-cache-dir --disable-pip-version-check uv==0.5.25

# An unprivileged user. Nothing here writes to disk.
RUN groupadd --system akira && useradd --system --gid akira --home /srv --shell /usr/sbin/nologin akira

WORKDIR /srv

# Dependency layer - cached until pyproject/lockfile change. `--frozen` fails
# the build if the lockfile is out of step rather than resolving something
# else on the quiet.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
RUN chown -R akira:akira /srv

USER akira
EXPOSE 8000

# Liveness only: /healthz never touches the database, so a database blip does
# not get the process killed and restarted into the same blip.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# --proxy-headers: behind the platform's edge, trust X-Forwarded-* so
# request.client is the real caller (the rate limiter keys on it for
# unauthenticated requests) and the scheme is https. The edge is the only
# thing that can reach this port, so every proxy is trusted.
CMD ["/srv/.venv/bin/uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "65", \
     "--no-server-header"]
