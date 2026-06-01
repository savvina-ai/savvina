#!/bin/sh
set -e

# Run migrations with retry — handles a race condition when PostgreSQL is
# provided via an external connection string and not a Compose-managed
# service with a healthcheck (required: false in depends_on).
MAX_RETRIES=30
i=0
until alembic upgrade head; do
    i=$((i + 1))
    if [ "$i" -ge "$MAX_RETRIES" ]; then
        echo "ERROR: Database migration failed after $MAX_RETRIES attempts" >&2
        exit 1
    fi
    echo "Database not ready, retrying in 2s (attempt $i/$MAX_RETRIES)..." >&2
    sleep 2
done

if [ "${RELOAD:-0}" = "1" ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir /app/app
else
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
