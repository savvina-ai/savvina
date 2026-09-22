# Docker Infrastructure

This document describes every Docker service, volume, and network in `docker-compose.yaml`.

---

## Services Overview

```
docker-compose.yaml
├── db                  ← PostgreSQL 16 app database (port 5434) [profile: local-db]
├── sample-postgres     ← PostgreSQL 16 sample/test database (port 5435) [profile: test-dbs]
├── sample-mysql        ← MySQL 8.0 demo/test database (port 3307) [profile: test-dbs]
├── backend             ← FastAPI application server (port 8000)
├── frontend            ← React app served by Nginx (port 3000)
└── ollama              ← Local LLM server (optional, requires --profile local-llm, port 11434)

Networks: savvina (bridge)
Volumes:  ollama_models (named), volumes/ (bind-mount)
```

Three profiles are available:

| Profile | Purpose |
|---|---|
| `local-db` | Starts the bundled `db` PostgreSQL container (for local development) |
| `local-llm` | Starts the Ollama container for local inference |
| `test-dbs` | Starts the `sample-postgres` and `sample-mysql` containers pre-seeded with demo data |

Activate profiles via `COMPOSE_PROFILES` in `.env` (e.g. `COMPOSE_PROFILES=local-db`) or with `--profile local-db` on the CLI. Multiple profiles can be combined: `COMPOSE_PROFILES=local-db,test-dbs`.

---

## `db`

> **Profile required:** `local-db`. Only starts when `COMPOSE_PROFILES=local-db` is set in `.env` or `--profile local-db` is passed on the CLI. To use an external managed PostgreSQL instead, see [External / Managed PostgreSQL](#external--managed-postgresql) below.

The bundled PostgreSQL 16 application database. Stores all Savvina AI state: user accounts, connections, chat history, cache, and provider configs. Managed by Alembic migrations.

```yaml
db:
  image: pgvector/pgvector:pg16
  container_name: savvina-app-db
  profiles:
    - local-db
  ports:
    - "0.0.0.0:5434:5432"
  environment:
    POSTGRES_DB: savvina_app
    POSTGRES_USER: savvina
    POSTGRES_PASSWORD: ${APP_DB_PASSWORD}
  volumes:
    - ./volumes/app-db:/var/lib/postgresql/data
```

**Port mapping:** Host port `5434` → container `5432` (bound on all interfaces).

**Data volume:** `./volumes/app-db/` on the host — back this up regularly (see [Maintenance](../administration/maintenance.md)).

**Connection string (for direct inspection):**

```
docker compose exec db psql -U savvina savvina_app
```

---

## External / Managed PostgreSQL

If you already have a PostgreSQL instance from a cloud provider (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL, Supabase, Neon, Aiven, etc.), you can skip the bundled `db` container entirely.

### Configuration

In `.env`:

1. Remove or comment out `COMPOSE_PROFILES=local-db` (so the `db` container is not started).
2. Remove or comment out `APP_DB_PASSWORD` (not needed without the local container).
3. Set `DATABASE_URL` to your provider's connection string:

```bash
# Most managed providers enforce SSL — append ?ssl=require if connections are refused.
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@your-db-host.example.com:5432/savvina_app
```

Then start the stack as usual:

```bash
docker compose up --build
```

The `backend` container picks up `DATABASE_URL` from `.env` and connects directly to your managed instance. The local `db` container, its port mapping, and its `./volumes/app-db/` directory are never created.

### SSL

Most managed PostgreSQL providers require or default to SSL. If you see connection errors, append `?ssl=require` to the URL:

```bash
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@host:5432/savvina_app?ssl=require
```

Some providers (e.g. Supabase transaction pooler, some AWS RDS configs) also need `sslmode=require` specified differently — consult your provider's asyncpg/SQLAlchemy connection string docs.

### Migrations

Alembic migrations run automatically on container start (`entrypoint.sh` calls `alembic upgrade head`). The database user needs `CREATE TABLE`, `ALTER TABLE`, and `CREATE INDEX` privileges on the target database.

### Backups

Backups are the responsibility of your managed provider. Point-in-time recovery (PITR), automated snapshots, and cross-region replication are all provider-specific features — enable them in your provider's console.

---

## `sample-postgres`

> **Profile required:** `test-dbs`. Only starts when `COMPOSE_PROFILES=test-dbs` is set in `.env` or `--profile test-dbs` is passed on the CLI. This database is optional — omit the profile if you don't need sample data.

A PostgreSQL 16 database pre-seeded with demo data for testing and first-time evaluation. Seeded by scripts in `test_dbs/postgres/`.

```yaml
sample-postgres:
  image: postgres:16-alpine
  profiles: [test-dbs]
  ports:
    - "0.0.0.0:5435:5432"     # host:container — port 5435 avoids conflicts
  environment:
    POSTGRES_DB: savvina_test
    POSTGRES_USER: savvina
    POSTGRES_PASSWORD: ${SAMPLE_POSTGRES_PASSWORD}
```

**Port mapping:** Host port `5435` → container `5432` (bound on all interfaces).

**Init scripts:** `initdb/` is mounted read-only and run once on first startup.

**Connection string:**

```
Host:     localhost (from host machine)
Port:     5435
Database: savvina_test
Username: savvina
Password: <value of SAMPLE_POSTGRES_PASSWORD in .env>
SSL Mode: disable
```

---

## `sample-mysql`

> **Profile required:** `test-dbs`. Only starts when `COMPOSE_PROFILES=test-dbs` is set in `.env` or `--profile test-dbs` is passed on the CLI. This database is optional — omit the profile if you don't need sample data.

A MySQL 8.0 database pre-seeded with sample food-delivery data for first-time evaluation.

```yaml
sample-mysql:
  image: mysql:8.0
  profiles: [test-dbs]
  ports:
    - "0.0.0.0:3307:3306"     # host:container — port 3307 avoids conflicts with local MySQL
  environment:
    MYSQL_DATABASE: sample_delivery
    MYSQL_USER: savvina
    MYSQL_PASSWORD: ${SAMPLE_MYSQL_PASSWORD}
    MYSQL_ROOT_PASSWORD: ${SAMPLE_MYSQL_ROOT_PASSWORD}
```

**Port mapping:** Host port `3307` → container `3306` (bound on all interfaces).

**Init scripts:** `mysqldata/` is mounted read-only. MySQL runs all `.sql` files on first startup.

**Health check:** `mysqladmin ping` polled every 10 seconds, up to 15 retries with a 60-second start period (MySQL is slower to initialise than PostgreSQL).

**Connection string for the UI:**

```
Host:     sample-mysql (within Docker network)
Port:     3306
Database: sample_delivery
Username: savvina
Password: <value of SAMPLE_MYSQL_PASSWORD in .env>
SSL Mode: disable
```

---

## `backend`

The FastAPI application server.

```yaml
backend:
  build:
    context: ./backend
    dockerfile: Dockerfile
  user: "${LOCAL_UID:-1000}:${LOCAL_GID:-1000}"
  extra_hosts:
    - "host.docker.internal:host-gateway"
  ports:
    - "0.0.0.0:8000:8000"
  env_file:
    - ./.env
  volumes:
    - ./volumes/backend-data:/app/data
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')\""]
    interval: 5s
    timeout: 5s
    retries: 20
    start_period: 300s
  depends_on:
    db:
      condition: service_healthy
      required: false
```

### Build

The image is built from `backend/Dockerfile` using a three-stage build:

```dockerfile
# ── model stage — cached independently of Python deps ─────────────────────────
FROM --platform=$BUILDPLATFORM python:3.12-slim AS model-cache
RUN pip install --no-cache-dir fastembed
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache
# Optional HF_TOKEN arrives as a BuildKit secret (never a build arg), so it
# is not recorded in this stage's config or history. download_model.sh
# retries three times and then fails the build rather than producing an
# image without the embedding model.
COPY download_model.sh /download_model.sh
RUN --mount=type=secret,id=hf_token sh /download_model.sh

# ── builder ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
```

Key decisions:
- **Three-stage build** — `model-cache` downloads the HuggingFace model independently; `builder` installs Python deps and copies the model from that stage; `runtime` receives only the finished venv and model cache. No compiler or build tools in production.
- **Isolated model stage** — separating the model download from `requirements.txt` means changes to Python dependencies never bust the model cache layer (and vice versa).
- **Python venv** — installing into `/app/venv` makes the entire dependency tree a single directory that can be cleanly `COPY --from=builder`-ed into the runtime stage.
- **HF model pre-downloaded** — the embedding model is baked into the image at build time. Container startup is instant — no model download on first start.
- **`entrypoint.sh`** — runs `alembic upgrade head` before starting Uvicorn, ensuring migrations are applied on every container start.
- **`app/` only** — `tests/` and `pytest.ini` are never copied into the runtime image.
- Non-root `appuser` — runs as UID 1000 for security.

### `user` Setting

`"${LOCAL_UID:-1000}:${LOCAL_GID:-1000}"` runs the container process as your host user UID/GID. This prevents the container from writing files owned by root into `volumes/backend-data/`. On Linux, export your IDs before running:

```bash
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
docker compose up -d
```

On macOS and Windows Docker Desktop, file ownership is handled transparently and this setting is less critical.

### `extra_hosts`

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Makes `host.docker.internal` resolve to the Docker host's IP from within the container. This allows the backend to connect to databases running directly on the host machine (not in Docker). When a user enters `host.docker.internal` as the database host in the connection form, the connection reaches the host's network.

### Environment File

All configuration comes from `backend/.env` (never committed to git):

```yaml
env_file:
  - ./backend/.env
```

See [Configuration](../getting-started/02_configuration.md) for the full variable list.

### Data Volume

```yaml
volumes:
  - ./volumes/backend-data:/app/data
```

This bind-mount persists application data (embedding model cache, temporary files) to `volumes/backend-data/` on the host. The application database is stored in the `db` PostgreSQL service, not in this directory.

### Health Check

Polls `GET /health` using Python's `urllib.request` (no curl dependency in the slim image). Parameters:
- `start_period: 300s` — allows up to 5 minutes for cold start before health failures count (first build downloads the embedding model into the image; subsequent starts are fast)
- `retries: 20` — up to 100 seconds of retries after the start period

The `frontend` service waits for this check before starting.

### Startup Sequence

On startup (`entrypoint.sh` + `lifespan` in `main.py`):
1. `entrypoint.sh` runs `alembic upgrade head` — applies any pending PostgreSQL migrations
2. Configure Python logging
3. Bootstrap default admin user if none exists
4. Import all datasource adapters and LLM providers (triggers `@register_*` decorators)
5. Pre-warm the fastembed ONNX embedding model via `asyncio.to_thread`
6. Start accepting requests

---

## `frontend`

The React application compiled and served by Nginx over plain HTTP.

```yaml
frontend:
  image: savvinaai/savvina-frontend:${SAVVINA_IMAGE_TAG:-latest}
  build: ./frontend
  ports:
    - "${APP_PORT:-3000}:8080"
  depends_on:
    backend:
      condition: service_healthy
```

The frontend Dockerfile in `frontend/` is a two-stage build. The build stage (`node:22-alpine`) runs `npm ci` and `npm run build`; the runtime stage starts from a bare `alpine:3.21`, installs nginx with `apk add --no-cache nginx`, and copies `dist/` into `/usr/share/nginx/html`. It is deliberately not `nginx:alpine` — starting from bare Alpine and running `apk upgrade` in the same layer means the image ships current Alpine packages rather than whatever was current when the upstream nginx image was last published. Nginx serves the static files and proxies `/api/` requests to the backend.

**Port:** Host `${APP_PORT:-3000}` → container `8080` (Nginx, HTTP). The container does not terminate TLS.

**Pre-built image:** `docker compose pull` fetches `savvinaai/savvina-frontend:<SAVVINA_IMAGE_TAG>` (default `latest`, multi-arch `linux/amd64` + `linux/arm64`) from Docker Hub; `docker compose up --build` builds it locally instead.

**API proxy:** Requests to `/api/*` are forwarded to `http://backend:8000` within the Docker network. No CORS issues because the proxy is same-origin from the browser's perspective. Nginx forwards `Host`, `X-Real-IP`, and `X-Forwarded-For` only. `X-Real-IP` is the direct peer's address unless `TRUSTED_LB_CIDR` is set, in which case `docker-entrypoint.sh` renders `set_real_ip_from` / `real_ip_header X-Forwarded-For` directives into the config at startup and the address comes from the header the trusted proxy set when the direct peer falls inside one of those ranges — see [Rate Limiting and Proxy Trust](../getting-started/02_configuration.md#rate-limiting-and-proxy-trust). Whether the session cookie is marked `Secure` (and HSTS sent) is decided by the backend's `BEHIND_TLS_PROXY` setting, never by an `X-Forwarded-Proto` header — see [HTTPS Behind a Reverse Proxy](../getting-started/02_configuration.md#https-behind-a-reverse-proxy).

The frontend waits for the backend health check before Nginx starts — this prevents the UI from loading in a state where API calls would fail.

---

## HTTPS

The stack itself speaks plain HTTP, which is what you want for `localhost` and trusted LAN use. To serve it over HTTPS, put a TLS-terminating reverse proxy in front of the frontend port and bind the frontend to `127.0.0.1`. Set `BEHIND_TLS_PROXY=true` in `.env` so the backend marks the session cookie `Secure` and sends HSTS; the proxy must disable response buffering and allow read timeouts of at least 300 s (900 s for semantic-model generation) so streamed chat responses are not cut off. A worked Caddy example is in [Deployment → Configure HTTPS](../administration/deployment.md#5-configure-https).

---

## `ollama`

Local LLM inference server. Only starts when the `local-llm` profile is active.

```yaml
ollama:
  image: ollama/ollama:latest
  ports:
    - "0.0.0.0:11434:11434"
  volumes:
    - ollama_models:/root/.ollama
  profiles:
    - local-llm
  deploy:
    resources:
      limits:
        memory: 16g
      reservations:
        devices:
          - capabilities: [gpu]
  networks:
    - savvina
```

### Starting Ollama

```bash
# Start with the local-llm profile
docker compose --profile local-llm up -d

# Pull a model (must be done after the container starts)
docker compose exec ollama ollama pull llama3
docker compose exec ollama ollama pull codellama
```

### GPU Support

The `deploy.resources.reservations.devices` block requests GPU access. Docker passes the GPU through automatically if:
- You have NVIDIA Docker runtime (`nvidia-container-toolkit`) installed
- The container runtime supports GPU reservation

Without a GPU, Ollama runs on CPU (much slower — 30-120 seconds per response).

### Model Storage

Models are stored in the `ollama_models` named volume (not a bind-mount). This prevents accidental deletion with `docker compose down` while still allowing management via `docker volume rm` when needed.

### Backend Connection

The backend connects to Ollama at `http://ollama:11434` (using the Docker service name as hostname within the `savvina` network). This is the default `OLLAMA_BASE_URL` in the env file.

---

## Networks

```yaml
networks:
  savvina:
    driver: bridge
```

All services communicate on the `savvina` bridge network using their service names as hostnames:
- `sample-postgres` → resolved from `backend` (only when `test-dbs` profile is active)
- `sample-mysql` → resolved from `backend` (only when `test-dbs` profile is active)
- `backend` → resolved from `frontend` (for the API proxy)
- `ollama` → resolved from `backend` (only when `local-llm` profile is active)

No service is accessible from outside Docker unless a `ports:` mapping is defined.

---

## Volumes

```yaml
volumes:
  ollama_models:    # Named volume — Ollama model files
```

Named volumes (managed by Docker) vs. bind-mounts (host paths):

| Storage | Type | Location | Notes |
|---|---|---|---|
| `ollama_models` | Named volume | Docker managed | Survives `compose down`, deleted by `compose down --volumes` |
| `volumes/app-db` | Bind-mount | `./volumes/app-db/` | App PostgreSQL data — **back this up**; only created when using `local-db` profile |
| `volumes/backend-data` | Bind-mount | `./volumes/backend-data/` | Always on host; never deleted by Docker |
| `volumes/sample-postgres` | Bind-mount | `./volumes/sample-postgres/` | Only created when using `test-dbs` profile; delete to reset sample PostgreSQL DB |
| `volumes/sample-mysql` | Bind-mount | `./volumes/sample-mysql/` | Only created when using `test-dbs` profile; delete to reset MySQL demo DB |

---

## Development Overrides

`docker-compose.override.yaml` is committed to the repository and picked up automatically by every `docker compose` command — you do not create it, and there is nothing to opt into. It provides volume permissions and two one-off helpers (below). Backend hot-reload lives in a second, opt-in file:

**Backend hot-reload (`docker-compose.dev.yaml`).** Bind-mounts `./backend/app` into the container and sets `RELOAD: "1"`, which `backend/entrypoint.sh` reads to start uvicorn with `--reload`. Editing backend source then takes effect without a rebuild. The mechanism is the environment variable, not a `command:` override — `entrypoint.sh` still needs to run `alembic upgrade head` before uvicorn starts. Enable it by adding to `.env`:

```bash
COMPOSE_FILE=docker-compose.yaml:docker-compose.override.yaml:docker-compose.dev.yaml
```

It is deliberately not part of the automatic override: with pre-built images (`docker compose pull`) the bind-mount would run this checkout's code on top of the image's `alembic/` migrations, and the two can disagree.

**Volume permissions.** An `init-permissions` service (running as root) creates and chowns the bind-mounted directories under `./volumes/` before anything else starts. The `backend`, `frontend`, `sample-mysql` and `sample-postgres` services all declare `depends_on: init-permissions` with `condition: service_completed_successfully`, so this always runs first.

**Two one-off helper services**, each behind a profile so it never starts with a normal `docker compose up`:

```bash
# Regenerate frontend/package-lock.json without a local Node install
docker compose run --rm frontend-lockfile

# Run the frontend test suite in a container (npm ci && npm test)
docker compose --profile test run --rm frontend-test
```

Both use `node:22-alpine`, matching `frontend/Dockerfile` and CI. Keep them in step: `frontend/package.json` declares `engines.node >= 22.22.0` (react-router 8's floor), so an older image fails with `EBADENGINE`.

There is no `dev` stage in `frontend/Dockerfile` and no Vite dev server in the compose stack — the frontend container always serves the production build through nginx. For frontend hot-reload, run Vite directly on the host instead:

```bash
cd frontend && npm run dev     # http://localhost:3000, proxies /api to localhost:8000
```

### Personal settings

Because the override file is committed, the automatic override slot is already taken. Keep machine-specific settings in a separate file and pass it explicitly:

```bash
docker compose -f docker-compose.yaml -f docker-compose.override.yaml -f docker-compose.local.yaml up
```

Add `docker-compose.local.yaml` to `.gitignore` if you use this. Most per-machine settings — ports, passwords, `LOG_LEVEL`, `COMPOSE_PROFILES` — belong in `.env` instead, which is already ignored.

---

## Common Commands

```bash
# Start with local Docker DB (COMPOSE_PROFILES=local-db in .env, or pass --profile)
docker compose up -d
docker compose --profile local-db up -d   # equivalent explicit form

# Start with sample/test databases (pre-seeded demo data)
docker compose --profile test-dbs up -d

# Local DB + test databases
docker compose --profile local-db --profile test-dbs up -d

# Start with local Ollama (add to COMPOSE_PROFILES or pass --profile)
docker compose --profile local-llm up -d

# Both local DB and local Ollama
docker compose --profile local-db --profile local-llm up -d

# Rebuild and restart
docker compose build && docker compose up -d

# View logs
docker compose logs -f

# Run backend tests
docker compose run --rm backend pytest tests/ -v

# Open a shell in the backend container
docker compose exec backend bash

# Inspect the PostgreSQL app database
docker compose exec db psql -U savvina savvina_app -c "\dt"

# Stop all services (preserves volumes)
docker compose down

# Stop and remove named volumes (Ollama models)
docker compose down --volumes

# Check service health
docker compose ps
```
