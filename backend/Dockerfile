# ── model stage — cached independently of Python deps ─────────────────────────
# Isolated so that changes to requirements.txt never bust this layer.
FROM python:3.12-slim AS model-cache
ARG HF_TOKEN
ENV HF_TOKEN=${HF_TOKEN}
RUN pip install --no-cache-dir fastembed
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache
RUN for i in 1 2 3; do \
        python -c "from fastembed import TextEmbedding; \
                   list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5').embed(['warmup']))" \
        && break; \
        echo "Retry $i..."; sleep 5; \
    done

# ── builder ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Create an isolated venv so the runtime stage can copy it cleanly
RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Model pulled from the dedicated stage — no network call here,
# so requirements.txt changes no longer bust the model cache layer.
COPY --from=model-cache /app/fastembed_cache /app/fastembed_cache
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache

COPY app/ ./app/

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/venv /app/venv
COPY --from=builder /app/fastembed_cache /app/fastembed_cache

COPY app/ ./app/
COPY static/ ./static/
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY entrypoint.sh .

RUN mkdir -p /app/data && chmod +x entrypoint.sh

ENV PATH="/app/venv/bin:$PATH"
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
