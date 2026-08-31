# open-teleset — production multi-stage image
# Build: docker build -f deploy/Dockerfile -t openclose8/open-teleset:latest .
# Run:   docker run -p 8080:8080 --env-file .env openclose8/open-teleset:latest

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src:/app

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies layer
COPY requirements-prod.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements-prod.txt

# Application code
COPY . .

# Non-root user
RUN useradd -m -u 10001 appuser && \
    chown -R appuser:appuser /app && \
    mkdir -p /app/accounts && chown appuser:appuser /app/accounts

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

LABEL org.opencontainers.image.source="https://github.com/hillstreet-ph/open-teleset" \
      org.opencontainers.image.description="open-teleset Telegram multi-account manager" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "-m", "uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
