# This project is Vercel-only. No Docker self-host.
# This Dockerfile is kept only for local `pip` debugging and is NOT used by Vercel.
# Vercel builds via `vercel.json` + `requirements.txt` directly.
# If you need it locally: docker build -t toolz-downloadz-api . && docker run -p 8000:8000 --env-file .env toolz-downloadz-api
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api ./api
COPY vercel.json README.md ./
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
