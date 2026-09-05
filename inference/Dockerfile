# syntax=docker/dockerfile:1.7
# Inference image — FastAPI + sklearn. Multi-stage, non-root.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:$PATH \
    PORT=8000 \
    LOG_LEVEL=INFO

# Non-root user
RUN groupadd --system --gid 1000 mlops \
 && useradd --system --uid 1000 --gid mlops --home /home/mlops --shell /bin/bash mlops \
 && mkdir -p /app /home/mlops && chown -R mlops:mlops /app /home/mlops

WORKDIR /app

# Copy installed python packages from builder
COPY --from=builder /install /usr/local

# Copy app code
COPY --chown=mlops:mlops app /app/app

USER mlops

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        u=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); \
        sys.exit(0 if u.status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--access-log"]
