# Self-host build — production-ready with layered cache.
# Differences vs Vercel serverless:
#   - Persistent IP you can warm up (residential IPs best).
#   - Optional cookie support via YOUTUBE_COOKIES.
#   - Optional PO Token bypass via bgutil provider (YT_DLP_POT_PROVIDER_URL).
#
# Build & run:
#   docker build -t toolz-downloadz-api .
#   docker run --rm -p 8000:8000 -e API_SECRET_KEY=your_key toolz-downloadz-api
# With compose (recommended):
#   docker compose up --build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: ca-certificates for yt-dlp + curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source — keep layer cache for deps
COPY api ./api
COPY vercel.json README.md ./

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
