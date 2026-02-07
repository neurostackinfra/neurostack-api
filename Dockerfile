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

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Configure Poetry to not create virtual environment
RUN poetry config virtualenvs.create false

# Install dependencies (production only)
RUN poetry install --no-interaction --no-ansi --only main

# ============================================
# Stage 3: Development image
# ============================================
FROM deps AS development

# Install dev dependencies
RUN poetry install --no-interaction --no-ansi

# Copy application code
COPY --chown=neurostack:neurostack . .

# Switch to non-root user
USER neurostack

# Expose port
EXPOSE 8000

# Development command with reload
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ============================================
# Stage 4: Production image
# ============================================
FROM base AS production

WORKDIR /app

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=neurostack:neurostack src ./src
COPY --chown=neurostack:neurostack config ./config
COPY --chown=neurostack:neurostack alembic.ini ./
COPY --chown=neurostack:neurostack alembic ./alembic

# Switch to non-root user
USER neurostack

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production command with workers
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

