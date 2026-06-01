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
FROM python:3.12-slim AS model-cache
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
5. Pre-warm the sentence-transformer embedding model via `asyncio.to_thread`
6. Start accepting requests

---

## `frontend`

The React application compiled and served by Nginx over HTTPS.

```yaml
frontend:
  build: ./frontend
  ports:
    - "${APP_PORT:-3000}:8443"
  volumes:
    - ./volumes/certs:/etc/nginx/certs:ro
  depends_on:
    backend:
      condition: service_healthy
```

The frontend Dockerfile in `frontend/` builds the React app with `npm run build` and copies the output into an `nginx:alpine` image. Nginx serves the static files over HTTPS and proxies `/api/` requests to the backend.

**Port:** Host `${APP_PORT:-3000}` → container `8443` (Nginx HTTPS). Port 80 inside the container issues a permanent redirect to HTTPS.

**TLS certificates:** Nginx reads the cert and key from `/etc/nginx/certs/` inside the container, which is bind-mounted from `./volumes/certs/` on the host. The cert filenames expected by `frontend/nginx.conf` are:

| File | Purpose |
|---|---|
| `volumes/certs/localhost+1.pem` | Certificate (chain) |
| `volumes/certs/localhost+1-key.pem` | Private key |

Place your own cert/key files here for production (see [HTTPS setup](#https-setup) below).

**API proxy:** Requests to `/api/*` are forwarded to `http://backend:8000` within the Docker network. No CORS issues because the proxy is same-origin from the browser's perspective.

The frontend waits for the backend health check before Nginx starts — this prevents the UI from loading in a state where API calls would fail.

---

## HTTPS Setup

TLS is terminated inside the `frontend` container by Nginx. You supply the certificate files — the stack does not generate or renew them automatically. This means any cert source works: mkcert, Let's Encrypt, a commercial CA, or your organisation's internal CA.

### Local development (mkcert)

[mkcert](https://github.com/FiloSottile/mkcert) generates locally-trusted certs and installs its CA into the OS/browser trust store so no warnings appear.

```bash
# Install mkcert (Debian/Ubuntu/WSL) — fetches the latest release automatically
sudo apt install libnss3-tools
curl -Lo mkcert "$(curl -s https://api.github.com/repos/FiloSottile/mkcert/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d '"' -f 4)"
chmod +x mkcert && sudo mv mkcert /usr/local/bin/
```

```bash
# Install mkcert (RHEL / Fedora / CentOS)
sudo dnf install nss-tools
curl -Lo mkcert "$(curl -s https://api.github.com/repos/FiloSottile/mkcert/releases/latest \
  | grep browser_download_url | grep linux-amd64 | cut -d '"' -f 4)"
chmod +x mkcert && sudo mv mkcert /usr/local/bin/
```

```bash
# Install the CA (run once)
mkcert -install

# Generate certs for localhost and 127.0.0.1
mkdir -p volumes/certs
cd volumes/certs
mkcert localhost 127.0.0.1
cd ../..
```

This produces `localhost+1.pem` and `localhost+1-key.pem` — exactly the filenames Nginx expects. After `docker compose up --build`, the app is accessible at `https://localhost:<APP_PORT>` with a green padlock.

> **WSL users:** `mkcert -install` inside WSL only updates the Linux certificate store — it does not reach the Windows browser trust store. To avoid certificate warnings in Chrome/Edge/Firefox on Windows, also run `mkcert -install` once from a **Windows** Command Prompt or PowerShell (requires mkcert installed on the Windows side via `winget install FiloSottile.mkcert`).

**Accessing from a custom hostname or remote machine:** include the extra hostname in the `mkcert` command:

```bash
mkcert localhost 127.0.0.1 your-hostname.example.com
```

The cert filename changes when more SANs are added (e.g. `localhost+2.pem` for three SANs) — update `frontend/nginx.conf` to match:

```nginx
ssl_certificate     /etc/nginx/certs/localhost+2.pem;
ssl_certificate_key /etc/nginx/certs/localhost+2-key.pem;
```

Update `CORS_ORIGINS` in `.env` to include every origin users will access the app from:

```bash
CORS_ORIGINS=["https://localhost:3000","https://your-hostname.example.com:3000"]
```

### Production (custom certs)

Drop your certificate files into `volumes/certs/` and update the filenames in `frontend/nginx.conf`:

```nginx
ssl_certificate     /etc/nginx/certs/fullchain.pem;
ssl_certificate_key /etc/nginx/certs/privkey.pem;
```

Let's Encrypt with Certbot on the host:

```bash
certbot certonly --standalone -d yourdomain.com
cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem volumes/certs/
cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   volumes/certs/
```

Then rebuild the frontend image (`docker compose build frontend`) so Nginx picks up the updated config, and restart. The certs themselves are bind-mounted, so you can rotate them without a rebuild — just copy the new files and run `docker compose exec frontend nginx -s reload`.

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

Create `docker-compose.override.yaml` (automatically picked up by Docker Compose) for development-specific settings. This file should not be committed to git.

Example for hot-reload development:

```yaml
# docker-compose.override.yaml
services:
  backend:
    volumes:
      - ./backend/app:/app/app        # mount source code
      - ./volumes/backend-data:/app/data
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    environment:
      LOG_LEVEL: DEBUG

  frontend:
    build:
      context: ./frontend
      target: dev                     # use multi-stage dev target
    ports:
      - "3000:3000"
    volumes:
      - ./frontend/src:/app/src       # mount source for HMR
    command: npm run dev
```

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
