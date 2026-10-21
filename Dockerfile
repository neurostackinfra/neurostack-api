# ============================================
# NeuroStack API - Dockerfile
# Multi-stage build for optimized production image
# ============================================

# ============================================
# Stage 1: Base image with Python
# ============================================
# Pinned by tag AND by the multi-arch index digest. Resolved on 2026-10-19 with
# `docker buildx imagetools inspect python:3.11-slim`: the floating tag
# python:3.11-slim was python:3.11.17-slim-trixie, index
# sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce,
# whose linux/amd64 manifest is
# sha256:e529028263dbe6910a2d96f7d2b8f5266385e917fd45d286ef166977c094a51e.
# The build stages derive from this FROM; the production stage starts afresh
# from the same image, and its FROM must stay byte-identical to this one.
FROM python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-time system packages: compilers and headers for the deps and
# development stages. None of them reaches the production stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# The interpreter's own packages, at the poetry.lock versions and wheel hashes.
# The production stage repeats this install on its fresh interpreter and then
# copies the deps stage's site-packages over it: a package the deps install
# had to replace would otherwise leave its old metadata behind (the image
# ships packaging 26.3, the lock pins 26.2); pip and setuptools move past the
# base image's known advisories.
COPY scripts/requirements-production-base.txt /opt/requirements/production-base.txt
RUN pip install --require-hashes --only-binary :all: -r /opt/requirements/production-base.txt

# Create non-root user for security
RUN groupadd --gid 1000 neurostack \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home neurostack

# ============================================
# Stage 2: Dependencies
# ============================================
FROM base AS deps

WORKDIR /app

# Poetry (the version that writes poetry.lock and that CI runs) lives in its own
# virtual environment. With virtualenvs.create false, a Poetry inside a venv
# installs into the base interpreter (/usr/local), and the production stage
# copies only that interpreter's site-packages: neither Poetry nor its
# dependencies reach the production image. The venv first replaces its bundled
# pip with the hashed one, then installs Poetry and every dependency from
# scripts/requirements-poetry.txt (hash-locked, regenerated with the uv command
# in its header).
COPY scripts/requirements-poetry.txt /opt/requirements/poetry.txt
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --require-hashes --only-binary :all: -r /opt/requirements/production-base.txt \
    && /opt/poetry/bin/pip install --require-hashes --only-binary :all: -r /opt/requirements/poetry.txt

# Copy dependency files and the editable path dependency before installation.
COPY pyproject.toml poetry.lock* ./
COPY packages/neurostack-core ./packages/neurostack-core

# Configure Poetry to not create virtual environment
RUN /opt/poetry/bin/poetry config virtualenvs.create false

# Install dependencies (production only)
RUN /opt/poetry/bin/poetry install --no-interaction --no-ansi --only main --no-root

# ============================================
# Stage 3: Development image
# ============================================
FROM deps AS development

# Install dev dependencies
RUN /opt/poetry/bin/poetry install --no-interaction --no-ansi --no-root

# Copy application code
COPY --chown=neurostack:neurostack . .

# Switch to non-root user
USER neurostack

# Expose port
EXPOSE 8000

# Development command with reload.
# ``--no-access-log`` is defence-in-depth: :mod:`src.config.logging` also
# disables uvicorn's access logger. The application emits a post-routing
# access log via RequestIdMiddleware that uses the route template instead
# of the raw path so the Z-API capability token cannot leak.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--no-access-log"]

# ============================================
# Stage 4: Frontend build (SPA served by the API)
# ============================================
# Node is pinned by tag AND by the multi-arch index digest; pnpm is pinned by
# version AND by the sha512 of its registry tarball (corepack format, hex),
# equal to "packageManager" in frontend/package.json. Only dist/public leaves
# this stage: no node_modules, sources, sourcemaps (neither .map files, inline
# sourceMappingURL data URLs nor renamed maps carrying sourcesContent), no
# TypeScript file and no Node runtime reach the production image. The guarded
# build RUN MUST stay the last instruction of this stage (the guards only vouch
# for what they inspected), and every WORKDIR/COPY/RUN here is pinned by
# tests/scripts/test_production_dockerfile_frontend.py: nothing else may write
# into the workspace (Vite copies artifacts/neurostack/public verbatim).
FROM node:22.22.3-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS frontend-build
ARG PNPM_PIN=11.1.2+sha512.415a1cc25974731e75455c1468371be74c5aa5fb7621b50d4056d222451609f11412f23fd602e6169f1e060466641f798597e1be961a10688836a67b16569499
ARG FRONTEND_PORT=3000
# MUST stay "/": the SPA calls /v1 root-relative and /billing is the
# hosted-checkout return URL.
ARG FRONTEND_BASE_PATH=/
# NODE_ENV is NOT set before the install: every frontend dependency is a
# devDependency and NODE_ENV=production would skip them.
ENV CI=true pnpm_config_verify_deps_before_run=false
RUN corepack enable && corepack prepare "pnpm@${PNPM_PIN}" --activate
WORKDIR /frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/.npmrc ./
COPY frontend/lib/api-client-react/package.json lib/api-client-react/
COPY frontend/scripts/package.json scripts/
COPY frontend/artifacts/mockup-sandbox/package.json artifacts/mockup-sandbox/
COPY frontend/artifacts/neurostack/package.json artifacts/neurostack/
RUN pnpm install --frozen-lockfile --ignore-scripts --filter "@workspace/neurostack..."
COPY frontend/tsconfig.base.json frontend/tsconfig.json ./
COPY frontend/lib ./lib
COPY frontend/attached_assets/logo_png_1775411054369.png ./attached_assets/
COPY frontend/artifacts/neurostack ./artifacts/neurostack
RUN PORT="${FRONTEND_PORT}" BASE_PATH="${FRONTEND_BASE_PATH}" NODE_ENV=production pnpm --filter @workspace/neurostack run build \
    && test -s artifacts/neurostack/dist/public/index.html \
    && test -z "$(find artifacts/neurostack/dist/public -name '*.map' -print -quit)" \
    && ! grep -rqs sourceMappingURL artifacts/neurostack/dist/public \
    && ! grep -rqs sourcesContent artifacts/neurostack/dist/public \
    && test -z "$(find artifacts/neurostack/dist/public \( -name '*.ts' -o -name '*.tsx' -o -name '*.mts' \) -print -quit)"

# ============================================
# Stage 5: Production image (runtime only, no compilers)
# ============================================
# A fresh stage on the same pinned image as base (the pin is recorded above
# the base FROM; the two FROM lines must stay identical). It never builds on
# base, so build-essential and libpq-dev (gcc, g++, make, binutils, cpp,
# pg_config, the C headers) stay out: the image gets only what the
# application needs to run. scripts/production_image_smoke.py checks it.
FROM python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS production

# Same as base.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime system packages: curl only, for the HEALTHCHECK below (and any
# orchestrator probe that runs curl in the container, e.g. an ECS task
# health check: change that first before dropping curl). Every shared library
# the interpreter and the locked wheels link against already ships with the
# pinned image (the smoke's native-libraries check runs ldd over every ELF
# file under /usr/local), and asyncpg speaks the PostgreSQL protocol itself:
# no libpq. No -dev package, no recommends, no apt lists left behind. Not
# pinned: the versions are whatever the Debian mirror serves on build day.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# BEFORE the deps site-packages are copied: this fresh interpreter ships pip
# 24.0, setuptools 79.0.1 and packaging 26.3, and a copied packaging 26.2
# dist-info next to the image's 26.3 one would leave two metadata directories
# for one package. After this install both sides hold the same versions.
COPY scripts/requirements-production-base.txt /opt/requirements/production-base.txt
RUN pip install --require-hashes --only-binary :all: -r /opt/requirements/production-base.txt

# Non-root user, same ids as base.
RUN groupadd --gid 1000 neurostack \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home neurostack

WORKDIR /app

# The deps interpreter's packages (the lock's main group over the same base
# tooling as above) and console scripts; the .so files load against the
# pinned image's libraries, the deps stage's compilers stay behind.
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY --from=deps --chown=neurostack:neurostack /app/packages/neurostack-core /app/packages/neurostack-core

# Copy application code
COPY --chown=neurostack:neurostack src ./src
COPY --chown=neurostack:neurostack config ./config
COPY --chown=neurostack:neurostack alembic.ini ./
COPY --chown=neurostack:neurostack alembic ./alembic
# One-off first-access bootstrap; the rest of scripts/ stays out of the image.
COPY --chown=neurostack:neurostack scripts/bootstrap_admin.py ./scripts/bootstrap_admin.py
# Built SPA (static files only); src/api/spa.py serves it when SPA_DIST_DIR is set.
COPY --from=frontend-build --chown=neurostack:neurostack /frontend/artifacts/neurostack/dist/public /app/spa
ENV SPA_DIST_DIR=/app/spa

# Switch to non-root user
USER neurostack

# Expose port
EXPOSE 8000

# Health check (curl is the runtime package installed above)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Source revision of the build (OCI annotation key). Declared after every COPY
# so a new value never invalidates a cached layer.
ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision="${VCS_REF}"

# Production command with workers.
# ``--no-access-log`` is defence-in-depth: :mod:`src.config.logging` also
# disables uvicorn's access logger. The application emits a post-routing
# access log via RequestIdMiddleware that uses the route template instead
# of the raw path so the Z-API capability token cannot leak.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-access-log"]
