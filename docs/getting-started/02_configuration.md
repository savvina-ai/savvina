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
| `ENCRYPTION_KEY` | Fernet symmetric key used to encrypt database credentials and API keys at rest. **Docker**: auto-generated on first boot and persisted to `/app/data/secrets.env` — no manual step needed. **Bare-metal / non-Docker**: generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and add to `.env`. Back up this key — losing it makes all stored credentials permanently unreadable. |
| `JWT_SECRET_KEY` | Secret used to sign and verify user access tokens. Must be at least 32 characters. **Docker**: auto-generated on first boot alongside `ENCRYPTION_KEY` — sessions survive container restarts. **Bare-metal / non-Docker**: generate with `python -c "import secrets; print(secrets.token_hex(32))"` and add to `.env`. |
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

## LLM Providers

At least one LLM provider must be configured before any chat queries can be made. Provider API keys are entered **only through the UI** (the setup wizard on first login, or **Settings → LLM Providers**) and stored in the app database encrypted with `ENCRYPTION_KEY`. No environment variable is read for provider keys — `ANTHROPIC_API_KEY`, `GROQ_API_KEY` and the like are ignored if set.

The only provider-related environment variables are:

| Variable | Purpose | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | Ollama (local) base URL | Default: `http://ollama:11434`. Ollama needs no API key. **Merely setting this variable — even to the same value as the default — is what makes Ollama appear as a configured provider in the UI.** Leaving it unset is what keeps Ollama hidden on installations that never use it. |
| `VERIFY_SSL` | TLS verification for all provider calls | Default `true`; see [SSL / TLS Settings](#ssl--tls-settings). |

A provider with no saved config shows a grey "not configured" dot on the Settings page and cannot be selected for chat until a config with a key is added.

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

Both are also editable from **Settings → Query Execution**. Once saved there, the value in `app_settings` wins and the environment variable is only the fallback used before a row exists — see [Runtime Settings](#runtime-settings-managed-in-the-ui).

---

## Cache Settings

The query cache stores question → SQL pairs and uses fastembed ONNX embeddings for semantic similarity matching.

**Cache enabled** and **semantic similarity threshold** are managed from the UI (**Settings → AI & Optimization**) and persisted in the database — do not set them as environment variables.

| Variable | Type | Default | Description |
|---|---|---|---|
| `EMBEDDING_MODEL` | string | `BAAI/bge-small-en-v1.5` | fastembed ONNX model used to compute question embeddings. **Warning:** Changing this model invalidates all stored embeddings. Clear the cache (`DELETE FROM query_cache`) before deploying a model change. |
| `CACHE_MAX_AGE_DAYS` | int | `30` | Cache entries not accessed within this window are excluded from semantic lookup. Set to `0` to disable TTL. Startup default only — **Settings → AI & Optimization** writes `cache_max_age_days` to `app_settings`, and the cache reads that value live on every lookup. |

---

## JWT Settings

| Variable | Type | Default | Description |
|---|---|---|---|
| `JWT_SECRET_KEY` | string | **Required** | Secret used to sign and verify access tokens. Must be at least 32 characters. |
| `JWT_ALGORITHM` | string | `HS256` | JWT signing algorithm. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | int | `30` | Access token lifetime in minutes. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | int | `30` | Refresh token lifetime in days. |

---

## Rate Limiting and Proxy Trust

| Variable | Type | Default | Description |
|---|---|---|---|
| `AUTH_RATE_LIMIT` | string | `10/minute` | Rate limit applied to authentication endpoints (login, register, refresh, password reset, profile update). Uses `slowapi` syntax, e.g. `20/minute`, `100/hour`. |
| `TRUSTED_PROXIES` | JSON array | `["127.0.0.1","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","::1","fc00::/7"]` | CIDR ranges (IPv4 and IPv6) whose direct connection is trusted to set `X-Real-IP` (client IP). |

**`TRUSTED_PROXIES` decides one thing: which IP a request is attributed to.** If the request's *direct* TCP peer is inside one of these ranges, the client IP is taken from the `X-Real-IP` header; otherwise the direct peer address is used and the header is ignored. Note it is `X-Real-IP` specifically — `X-Forwarded-For` is not consulted, so a proxy that only sets the latter will leave every request attributed to the proxy itself. That resolved IP is the rate-limit key and is what login attempts are logged against. It has no bearing on HTTPS, the session cookie's `Secure` flag, or HSTS — those are governed by `BEHIND_TLS_PROXY` (next section), not by any header.

The default trusts loopback plus all three RFC1918 private IPv4 ranges, plus IPv6 loopback (`::1`) and unique-local addresses (`fc00::/7`), which is right for the standard deployment — the frontend Nginx container proxying to the backend over the Docker network. Two cases need attention:

- **A reverse proxy in front of the frontend container.** The bundled nginx is what sets `X-Real-IP`, and it sets it to its own direct peer — the proxy — unless told which peers to trust. Set `TRUSTED_LB_CIDR` (a frontend-container variable, comma-separated CIDRs, e.g. `172.16.0.0/12` for a proxy on the same host) so nginx takes the client address from the proxy's `X-Forwarded-For` instead. Without it every request appears to come from the proxy and all clients share one rate-limit bucket. `TRUSTED_PROXIES` itself usually needs no change here, since nginx is on the Docker network.
- **The backend reachable directly from untrusted networks** — do not widen this. Any client that can connect from a trusted range could then spoof `X-Real-IP` and evade rate limiting entirely by rotating the header.

Set it as a JSON array string, like `CORS_ORIGINS`:
```bash
TRUSTED_PROXIES=["127.0.0.1","10.0.0.0/8","203.0.113.5/32"]
```

---

## HTTPS Behind a Reverse Proxy

| Variable | Type | Default | Description |
|---|---|---|---|
| `BEHIND_TLS_PROXY` | bool | `false` | Set to `true` when a TLS-terminating reverse proxy serves the UI over `https://`. Turns on the session cookie's `Secure` flag and the `Strict-Transport-Security` header. |

The backend and the frontend container only ever speak plain HTTP; TLS, if any, terminates at a reverse proxy you put in front (see [Deployment → Configure HTTPS](../administration/deployment.md#5-configure-https)). The backend therefore cannot see whether the *browser's* connection was HTTPS, and it does not try to guess from `X-Forwarded-Proto`: on a plain-HTTP install that header is whatever the client sent, and behind a real proxy it may simply be missing — inferring from it fails open in both directions. You declare it instead:

- **Plain `http://` on localhost or a LAN IP** (the Quickstart setup): leave it unset. A `Secure` cookie would be refused by the browser and every login would silently fail at the first token refresh.
- **`https://` through a reverse proxy:** set `BEHIND_TLS_PROXY=true`. The backend refuses to start if `CORS_ORIGINS` contains an `https://` origin while this is off, so the misconfiguration is a visible startup error rather than a session cookie quietly issued without `Secure`.

The check is one-directional: `BEHIND_TLS_PROXY=true` with only `http://localhost` origins is allowed (a common dev setup next to a proxy).

```bash
BEHIND_TLS_PROXY=true
```

---

## Runtime Settings (managed in the UI)

These are stored in the `app_settings` table and edited from the **Settings** page. An environment variable (or the default in `config.py`) only supplies the value until a row exists for that key; once the setting has been saved from the UI, the database value wins and changing the environment variable has no effect.

`GET`/`PUT /api/v1/settings` answer from the database, so a saved value is reflected in the UI immediately in every worker. How quickly it changes *behaviour* depends on the setting:

- **Immediately, everywhere:** `bcrypt_rounds` — read from `app_settings` on each password operation.
- **Immediately in the worker that served the save, at next restart elsewhere:** every other setting except the two below. The request pipeline reads the process-wide `Settings` object, which `PUT /api/v1/settings` writes through for the serving worker only; other workers keep their boot-time value until they restart and the lifespan restore re-applies the saved rows. Single-worker deployments — the default Docker stack — therefore pick everything up at once.
- **Next process restart:** `db_pool_size` and `db_max_overflow`, because the SQLAlchemy engine is built once at startup in every worker.

| Setting | Default | Description |
|---|---|---|
| `default_query_timeout` | `30` | Seconds a generated query may run before cancellation. |
| `default_row_limit` | `1000` | Maximum rows returned per query. Also caps rows served by public share links. |
| `cache_enabled` | `true` | Whether the query cache is consulted and written. |
| `cache_max_age_days` | `30` | Entries not accessed within this window are excluded from semantic lookup. `0` disables the TTL. |
| `semantic_similarity_threshold` | `0.87` | Cosine similarity a cached question must reach to count as a hit. Raising it makes the cache stricter. |
| `schema_pruning_enabled` | `true` | Embed table names and send only the most relevant tables for each question, instead of the whole schema. |
| `schema_pruning_top_k` | `15` | Maximum tables kept after pruning. A `fallback_min` guard keeps small schemas intact. |
| `db_pool_size` | `10` | SQLAlchemy connection pool size. **Restart required.** |
| `db_max_overflow` | `20` | Connections allowed beyond the pool size under load. **Restart required.** |
| `bcrypt_rounds` | `12` | Password hashing cost factor. Raising it slows every login and registration. |

Read them with `GET /api/v1/settings` and change them with `PUT /api/v1/settings`.

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
| `LOCAL_UID` | `1000` | Backend container runs as this user ID. Leave unset unless your host UID differs from 1000; on Linux/WSL set both with `printf '\nLOCAL_UID=%s\nLOCAL_GID=%s\n' "$(id -u)" "$(id -g)" >> .env` to avoid volume permission issues. |
| `LOCAL_GID` | `1000` | Backend container group ID. Set together with `LOCAL_UID` above. |
| `HF_TOKEN` | *(unset)* | Hugging Face access token used **only during `docker compose build`**. Avoids anonymous rate-limiting when the `model-cache` stage downloads the fastembed ONNX model (`BAAI/bge-small-en-v1.5`) from HuggingFace. It is passed as a BuildKit secret mount, not a build arg, so it is never recorded in any image layer or config. Not used at runtime. A free read-only token is sufficient — get one at huggingface.co → Settings → Access Tokens. |
| `SAVVINA_IMAGE_TAG` | `latest` | Tag of the `savvinaai/savvina-backend` and `savvinaai/savvina-frontend` images that `docker compose pull` fetches. `latest` is the newest release; pin one with e.g. `SAVVINA_IMAGE_TAG=v2.0.0`. With `docker compose up --build`, Compose still builds from local source but stamps the result with this tag — it does not change what gets built. |
| `PG_UID` | `70` | UID that `init-permissions` chowns the `sample-postgres` volume to. Only needed if the bundled sample-Postgres image (`test-dbs` profile) is swapped for one that runs as a different UID. |
| `PG_GID` | `70` | GID for the same `sample-postgres` volume chown. Pairs with `PG_UID`. |
| `MYSQL_UID` | `999` | UID that `init-permissions` chowns the `sample-mysql` volume to. Only needed if the bundled sample-MySQL image (`test-dbs` profile) is swapped for one that runs as a different UID. |
| `MYSQL_GID` | `999` | GID for the same `sample-mysql` volume chown. Pairs with `MYSQL_UID`. |

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

# ── LLM Providers ─────────────────────────────────────────────────────────
# API keys are entered in the UI (Settings → LLM Providers); none go here.

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

# ── HTTPS behind a reverse proxy ─────────────────────────────────────────
# Required (true) whenever CORS_ORIGINS has an https:// entry; see
# "HTTPS Behind a Reverse Proxy" above.
# BEHIND_TLS_PROXY=false

# ── Rate limiting / proxy trust (usually no change needed) ────────────────
# See "Rate Limiting and Proxy Trust" above.
# TRUSTED_PROXIES=["127.0.0.1","10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","::1","fc00::/7"]

# ── Sample database passwords (only needed with test-dbs profile) ─────────
# COMPOSE_PROFILES=local-db,test-dbs   ← add test-dbs to enable these containers
SAMPLE_POSTGRES_PASSWORD=<strong-password>
SAMPLE_MYSQL_ROOT_PASSWORD=<strong-password>
SAMPLE_MYSQL_PASSWORD=<strong-password>

# ── Host UID/GID (Linux/WSL only; omit to accept the 1000:1000 default) ───
# Only needed if the sample-postgres/sample-mysql images are swapped for ones
# running as a different UID/GID — see Docker-Specific Settings above.
# PG_UID=70
# PG_GID=70
# MYSQL_UID=999
# MYSQL_GID=999

# ── Build optimisation (optional) ─────────────────────────────────────────
# Avoids HuggingFace anonymous rate-limiting during `docker compose build`.
# Free read-only token: huggingface.co → Settings → Access Tokens
HF_TOKEN=

# ── Pre-built images (optional) ────────────────────────────────────────────
# Tag of the savvinaai/savvina-* images `docker compose pull` fetches.
# SAVVINA_IMAGE_TAG=latest
```

---

## Applying Changes

Configuration is read at container startup, not per request, so an edited `.env` only takes effect once the affected container is recreated. `docker compose restart` reuses the existing container and its old environment — use `docker compose up -d <service>` instead:

```bash
# Most settings (JWT_SECRET_KEY, CORS_ORIGINS, BEHIND_TLS_PROXY, LLM/database config, etc.)
docker compose up -d backend

# Frontend-facing settings (APP_PORT, TRUSTED_LB_CIDR)
docker compose up -d frontend
```

If you changed `ENCRYPTION_KEY` (which requires re-encrypting all stored secrets — see [Encryption Key Rotation](../administration/maintenance.md#encryption-key-rotation)):

```bash
docker compose down
docker compose up
```
