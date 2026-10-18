# ============================================
# NeuroStack API - Dockerfile
# Multi-stage build for optimized production image
# ============================================

# ============================================
# Stage 1: Base image with Python
# ============================================
FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd --gid 1000 neurostack \
    && useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home neurostack

# ============================================
# Stage 2: Dependencies
# ============================================
FROM base AS deps

WORKDIR /app

# Install Poetry
RUN pip install poetry==1.8.2

# Copy dependency files and the editable path dependency before installation.
COPY pyproject.toml poetry.lock* ./
COPY packages/neurostack-core ./packages/neurostack-core

# Configure Poetry to not create virtual environment
RUN poetry config virtualenvs.create false

# Install dependencies (production only)
RUN poetry install --no-interaction --no-ansi --only main --no-root

# ============================================
# Stage 3: Development image
# ============================================
FROM deps AS development

# Install dev dependencies
RUN poetry install --no-interaction --no-ansi --no-root

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
# this stage: no node_modules, sources, sourcemaps (neither .map files nor
# inline sourceMappingURL data URLs) or Node runtime reach the production image.
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
    && ! grep -rqs sourceMappingURL artifacts/neurostack/dist/public

# ============================================
# Stage 5: Production image
# ============================================
FROM base AS production

WORKDIR /app

# Copy installed packages from deps stage
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

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production command with workers.
# ``--no-access-log`` is defence-in-depth: :mod:`src.config.logging` also
# disables uvicorn's access logger. The application emits a post-routing
# access log via RequestIdMiddleware that uses the route template instead
# of the raw path so the Z-API capability token cannot leak.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-access-log"]
