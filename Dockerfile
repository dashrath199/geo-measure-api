# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Geo Measure API — reproducible production image (Phase 20).
#
# Multi-stage build:
#   deps    — runtime dependencies installed into an isolated virtualenv
#   test    — deps + test tooling + suite (run with `docker run --rm`)
#   runtime — slim production image: application code + virtualenv only
#
# Geospatial stack: GeoPandas / Shapely / PyProj / Pyogrio (bundled GDAL)
# on Python 3.14 — the same engine the application already uses. Fiona is
# intentionally NOT installed: it publishes no Python 3.14 wheels.
#
# Dependency installation is deterministic: requirements.txt /
# requirements-dev.txt provide the version ranges, requirements.lock.txt
# pins every resolved version as pip constraints.
# ---------------------------------------------------------------------------

# Pinned base image: exact interpreter patch release + Debian codename.
ARG PYTHON_BASE=python:3.14.6-slim-bookworm

# ------------------------------------------------------------------- deps
FROM ${PYTHON_BASE} AS deps

# Reproducible pip behaviour: no cache, no upgrade prompts, no root-user
# warning noise (this stage is build-time only).
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# Constraints + range requirements first: this layer is shared by the
# runtime and test stages and only changes when dependencies change.
COPY requirements.lock.txt requirements.txt requirements-dev.txt ./

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Runtime dependencies only (no pytest/httpx here).
RUN pip install -r requirements.txt -c requirements.lock.txt

# ------------------------------------------------------------------- test
FROM deps AS test

# Development/test tooling on top of the runtime dependencies.
RUN pip install -r requirements-dev.txt -c requirements.lock.txt

WORKDIR /app

# Test tooling needs the suite, the application source, the pytest
# configuration and the Docker packaging files (validated by
# tests/test_docker_config.py).
COPY pytest.ini ./
COPY app ./app
COPY tests ./tests
COPY Dockerfile .dockerignore docker-compose.yml requirements.lock.txt ./

# `docker run --rm <test-image>` runs the full suite; the exit code is
# the result. The production image below never contains any of this.
CMD ["python", "-m", "pytest"]

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_BASE} AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Container-friendly interpreter behaviour: line-buffered stdout for
# docker logs, no .pyc writes into the read-only root filesystem,
# explicit import path, predictable home directory.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/app

# Runtime configuration defaults — non-secret, override at run time via
# `-e VAR=...` or docker compose environment (see README § Docker).
ENV APP_NAME="Geo Measure API" \
    ENVIRONMENT=production \
    LOG_LEVEL=INFO \
    API_PREFIX=/api \
    DATABASE_URL=sqlite:////data/geo_measure.db \
    STORAGE_DIR=/data/uploads \
    MAX_UPLOAD_MB=10 \
    MAX_FEATURES=50000

# Non-root application user (fixed uid/gid) and the explicit writable
# runtime directories it owns: /data holds the SQLite database and the
# upload storage; /tmp (container default) holds processing scratch.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data

WORKDIR /app

# Virtualenv built by the deps stage: runtime dependencies only — no
# test tooling, no build tooling. The installer itself is stripped: a
# running container never installs anything.
COPY --from=deps /opt/venv /opt/venv
RUN rm -rf /opt/venv/bin/pip* /opt/venv/lib/python3.*/site-packages/pip

# Application code only. The directory stays root-owned, so the runtime
# user cannot modify the code it executes.
COPY app ./app

# Build-time smoke test: the application must import, the geospatial
# environment must verify (Pyogrio + KML + ESRI Shapefile drivers), and
# the configured storage/database directories must be usable by the
# runtime user. A broken image fails here instead of at first request.
RUN python -c "import app.main; print('application import: OK')" \
    && python -c "from app.geo.environment import check_geospatial_environment as c; r = c(); assert r.ok, r.summary(); print(r.summary())" \
    && su -s /bin/sh app -c 'touch /data/uploads/.write_probe && rm /data/uploads/.write_probe' \
    && python -c "import shutil,sys; sys.exit(0 if shutil.which('uvicorn') else 1)"

USER app

EXPOSE 8000

# Healthy only when the API itself answers GET /health with success.
# (docker compose inherits this definition.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# Uvicorn serves the ASGI app on all container interfaces.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
