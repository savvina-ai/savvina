#!/bin/sh
set -e

# ── Secrets bootstrap ─────────────────────────────────────────────────────────
# Manages secrets that must never live in .env or be baked into the image.
#
# Three cases per secret:
#   1. Already in secrets file (normal restart)                → no-op
#   2. Not in file + key in env (migration from old .env)      → sanitise and persist
#   3. Not in file + no env (new deployment)                   → generate and persist
#
# After all secrets are present in the file, source it once.
SECRETS_FILE=/app/data/secrets.env

if [ ! -f "$SECRETS_FILE" ]; then
    (umask 077 && touch "$SECRETS_FILE")
fi
chmod 600 "$SECRETS_FILE"

# Bootstrap a named secret: check file → migrate from env (sanitised) → generate.
# $1 = variable name; $2 = shell command that prints just the generated value.
bootstrap_secret() {
    _var="$1"
    _gen_cmd="$2"

    if grep -q "^${_var}=" "$SECRETS_FILE"; then
        return 0
    fi

    # Strip embedded newlines/CR from any migrated value to avoid corrupting the file.
    _env_val=$(printenv "$_var" 2>/dev/null | tr -d '\n\r')
    if [ -n "$_env_val" ]; then
        printf '%s=%s\n' "$_var" "$_env_val" >> "$SECRETS_FILE"
        echo "[entrypoint] Migrated ${_var} from environment → $SECRETS_FILE"
    else
        printf '%s=%s\n' "$_var" "$(sh -c "$_gen_cmd")" >> "$SECRETS_FILE"
        echo "[entrypoint] Generated new ${_var} → $SECRETS_FILE"
    fi
}

bootstrap_secret ENCRYPTION_KEY \
    'python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
bootstrap_secret JWT_SECRET_KEY \
    'python3 -c "import secrets; print(secrets.token_hex(32))"'

# Source all secrets so Pydantic picks them up from the environment.
set -a
. "$SECRETS_FILE"
set +a
# ─────────────────────────────────────────────────────────────────────────────

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
