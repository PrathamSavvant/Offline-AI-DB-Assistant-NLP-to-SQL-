# Multi-stage / Production Dockerfile for AI Database Assistant Backend
FROM python:3.11-slim AS base

# Install OS dependencies: curl for container healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -m -s /bin/bash appuser

WORKDIR /app

# Install Python dependencies
COPY project/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application source code
COPY project/ /app/

# Ensure runtime directories exist and have proper non-root permissions
RUN mkdir -p /app/logs /app/knowledge && \
    chown -R appuser:appgroup /app

USER appuser

# Expose standard production port
EXPOSE 5000

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=5000 \
    WAITRESS_THREADS=8

# Container healthcheck using liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

# Production startup
CMD ["python", "production.py"]
