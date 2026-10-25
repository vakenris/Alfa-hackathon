# =============================================================================
# Alfa Business News Analytics — Backend Dockerfile
# =============================================================================
# Multi-stage build:
#   Stage 1 (builder) — installs Python deps into /install
#   Stage 2 (runtime) — copies only the wheels, no build tools in prod image
#
# Result: ~200 MB image vs ~500 MB single-stage
# =============================================================================

# ---- Stage 1: dependency builder --------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build-time system deps (gcc, libpq-dev for asyncpg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Stage 2: runtime image -------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime system deps only (libpq for asyncpg, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY backend/ ./backend/

# Environment
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Graceful shutdown with --timeout 30
CMD ["uvicorn", "backend.app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-graceful-shutdown", "30", \
     "--access-log"]
