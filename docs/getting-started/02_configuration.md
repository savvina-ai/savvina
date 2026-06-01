# Configuration Reference

All configuration is via environment variables loaded from `.env` at the project root. Copy `.env.example` as a starting point.

```bash
cp .env.example .env
```

---

## Required Variables

These must be set before the backend will start.

| Variable | Description |
|---|---|
| `ENCRYPTION_KEY` | Fernet symmetric key used to encrypt database credentials and API keys at rest. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET_KEY` | Secret used to sign and verify access tokens. Must be at least 32 characters. Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | asyncpg connection string for the application PostgreSQL database. Required in all modes — see [Database Mode](#database-mode) below. |
| `APP_DB_PASSWORD` | Password for the bundled `db` container. Only required when using the `local-db` profile. |

---

## Database Mode

Savvina AI supports two mutually exclusive database modes, selected via `COMPOSE_PROFILES` in `.env`.

### Local Docker PostgreSQL (`local-db` profile)

Recommended for development. The bundled `db` container is started automatically.

```bash
COMPOSE_PROFILES=local-db
APP_DB_PASSWORD=<strong-password>
DATABASE_URL=postgresql+asyncpg://savvina:<strong-password>@db:5432/savvina_app
```

`DATABASE_URL` must use `db` (the Docker service name) as the host — not `localhost`. If you run the backend directly on the host outside Docker, change the host to `localhost:5434` for that session.

### External / Managed PostgreSQL

For cloud-hosted databases (AWS RDS, GCP Cloud SQL, Azure, Supabase, Neon, Aiven, etc.). Leave `COMPOSE_PROFILES` unset; the `db` container will not start.

```bash
# COMPOSE_PROFILES=local-db   ← leave commented out
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@your-db-host.example.com:5432/savvina_app
# Most managed providers enforce SSL — append ?ssl=require if connections fail:
# DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@host:5432/savvina_app?ssl=require
```

`APP_DB_PASSWORD` is not needed in this mode.

---

## LLM Provider Keys

At least one LLM provider must be configured before any chat queries can be made. Provider API keys set here act as environment-level defaults; they can be overridden or supplemented per-provider in the UI (**Settings → Providers**).

| Variable | Provider | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Claude | Begins with `sk-ant-` |
| `OPENAI_API_KEY` | OpenAI GPT | Begins with `sk-` |
| `GROQ_API_KEY` | Groq | Begins with `gsk_` — free tier available |
| `GEMINI_API_KEY` | Google Gemini | Begins with `AIza` — free tier available |
| `CEREBRAS_API_KEY` | Cerebras | Free tier available |
| `MISTRAL_API_KEY` | Mistral | Free tier available |
| `OLLAMA_BASE_URL` | Ollama (local) | Default: `http://ollama:11434`. No key required. |

### Priority: UI Config vs. Environment Variable

When a chat request specifies a provider, the backend looks for credentials in this order:
1. Saved DB config for that provider (created via **Settings → Providers**)
2. Corresponding environment variable from the list above

This means you can provision keys via environment variables for automation, while individual users can override them through the UI.

**What the UI shows for env-only providers:** If a key is set via env var but no saved config exists, the Settings page displays a green *"Configured via environment variable · default model: X"* banner for that provider. The provider is fully functional — it uses the env key and the provider's hardcoded default model. To select a different model, click **+ Add config** and leave the API key field blank (the env key is used automatically).

**Model resolution:** `GET /api/v1/providers` always returns `current_model` as the model that will actually be used — the saved model if one is set, or the provider's default model otherwise. An empty `current_model` should never appear in a correctly configured provider.

---

## SSL / TLS Settings

| Variable | Type | Default | Description |
|---|---|---|---|
| `VERIFY_SSL` | bool | `true` | Set to `false` in corporate environments with TLS-intercepting proxies where the container cannot trust the custom CA. Applies to all LLM provider HTTP clients. Also accepted as `OPENAI_VERIFY_SSL` for backwards compatibility. |

---

## Application Settings

| Variable | Type | Default | Description |
|---|---|---|---|
| `APP_NAME` | string | `Savvina AI` | Application name shown in logs and API responses |
| `DEBUG` | bool | `false` | Enable FastAPI debug mode (verbose errors in responses) |
| `LOG_LEVEL` | string | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | string | `json` | Log output format. Use `text` for development (human-readable) and `json` for production (one JSON object per line, suitable for log aggregators). |
| `APP_PORT` | int | `3000` | Port the frontend container is exposed on. Update `CORS_ORIGINS` if you change this. |
| `DATABASE_URL` | string | **Required** | asyncpg connection URL for the application PostgreSQL database. See [Database Mode](#database-mode) for the correct value for your setup. |
| `CORS_ORIGINS` | JSON array | `["http://localhost:3000"]` | Allowed CORS origins as a JSON array string. Example: `["http://localhost:3000","https://analytics.example.com"]` |

---

## Query Safety Settings

| Variable | Type | Default | Description |
|---|---|---|---|
| `DEFAULT_QUERY_TIMEOUT` | int | `30` | Maximum seconds a generated SQL query may run before being cancelled. Applies at the database driver level. |
| `DEFAULT_ROW_LIMIT` | int | `1000` | Maximum rows returned per query. If a generated query lacks a `LIMIT` clause, the validator automatically appends one. |

---

## Cache Settings

The query cache stores question → SQL pairs and uses sentence-transformer embeddings for semantic similarity matching.

**Cache enabled** and **semantic similarity threshold** are managed from the UI (Settings page) and persisted in the database — do not set them as environment variables.

| Variable | Type | Default | Description |
|---|---|---|---|
| `EMBEDDING_MODEL` | string | `BAAI/bge-small-en-v1.5` | fastembed ONNX model used to compute question embeddings. **Warning:** Changing this model invalidates all stored embeddings. Clear the cache (`DELETE FROM query_cache`) before deploying a model change. |
| `CACHE_MAX_AGE_DAYS` | int | `30` | Cache entries not accessed within this window are excluded from semantic lookup. Set to `0` to disable TTL. |

---

## JWT Settings

| Variable | Type | Default | Description |
|---|---|---|---|
| `JWT_SECRET_KEY` | string | **Required** | Secret used to sign and verify access tokens. Must be at least 32 characters. |
| `JWT_ALGORITHM` | string | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | int | `30` | Access token lifetime in minutes. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | int | `30` | Refresh token lifetime in days. |

---

## Backing Service Credentials

These configure the bundled Docker containers. Each password is only required when its associated profile is active.

| Variable | Service | Required when |
|---|---|---|
| `APP_DB_PASSWORD` | Bundled `db` PostgreSQL container | `COMPOSE_PROFILES` includes `local-db` |
| `SAMPLE_POSTGRES_PASSWORD` | Sample PostgreSQL database | `COMPOSE_PROFILES` includes `test-dbs` |
| `SAMPLE_MYSQL_ROOT_PASSWORD` | MySQL root password | `COMPOSE_PROFILES` includes `test-dbs` |
| `SAMPLE_MYSQL_PASSWORD` | MySQL application user | `COMPOSE_PROFILES` includes `test-dbs` |

Generate strong values with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## Docker-Specific Settings

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_PROFILES` | *(unset)* | Comma-separated list of active Docker Compose profiles. `local-db` starts the bundled PostgreSQL app container. `test-dbs` starts the sample PostgreSQL and MySQL containers pre-seeded with demo data. `local-llm` starts Ollama. Combine as needed: `local-db,test-dbs`. Leave unset when using an external managed database. |
| `LOCAL_UID` | `1000` | Backend container runs as this user ID. Set to your host UID on Linux/WSL to avoid volume permission issues: `echo "LOCAL_UID=$(id -u)" >> .env` |
| `LOCAL_GID` | `1000` | Backend container group ID. Set with: `echo "LOCAL_GID=$(id -g)" >> .env` |

---

## Sample .env File

```bash
# ── Required ─────────────────────────────────────────────────────────────
ENCRYPTION_KEY=<paste-generated-fernet-key>
JWT_SECRET_KEY=<paste-generated-jwt-secret>

# ── Database (choose one mode) ────────────────────────────────────────────
# Option A — local Docker DB:
COMPOSE_PROFILES=local-db
APP_DB_PASSWORD=<strong-password>
DATABASE_URL=postgresql+asyncpg://savvina:<strong-password>@db:5432/savvina_app
# Option B — external/managed PostgreSQL (comment out the three lines above):
# DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@your-host:5432/savvina_app

# ── LLM Providers (set at least one) ─────────────────────────────────────
GROQ_API_KEY=gsk_...
# GEMINI_API_KEY=AIza...
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...

# ── App ──────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FORMAT=text   # use "json" in production
DEBUG=false
APP_PORT=3000

# ── Query Safety ─────────────────────────────────────────────────────────
DEFAULT_QUERY_TIMEOUT=30
DEFAULT_ROW_LIMIT=1000

# ── CORS (add your deployment URL here) ───────────────────────────────────
# CORS_ORIGINS=["http://localhost:3000","https://analytics.example.com"]

# ── Sample database passwords (only needed with test-dbs profile) ─────────
# COMPOSE_PROFILES=local-db,test-dbs   ← add test-dbs to enable these containers
SAMPLE_POSTGRES_PASSWORD=<strong-password>
SAMPLE_MYSQL_ROOT_PASSWORD=<strong-password>
SAMPLE_MYSQL_PASSWORD=<strong-password>

# ── Host UID/GID (Linux/WSL only) ─────────────────────────────────────────
LOCAL_UID=1000
LOCAL_GID=1000
```

---

## Applying Changes

Configuration changes require a backend restart:

```bash
docker compose restart backend
```

If you changed `ENCRYPTION_KEY` (which requires re-encrypting all stored secrets — see [Key Rotation](../administration/deployment.md#key-rotation)):

```bash
docker compose down
docker compose up
```
