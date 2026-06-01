# Deployment Guide

This guide covers deploying Savvina AI to a production server. The application is a Docker Compose stack designed to run on a single host with external access via a reverse proxy.

> For local development, see the [Quick Start in README](../../README.md#quick-start-free--no-api-costs) instead.

---

## Prerequisites

- Linux server (Ubuntu 22.04+ or RHEL 9+ recommended)
- Docker Engine 24+ and Docker Compose plugin (`docker compose`, not `docker-compose`)
- At least 4 GB RAM; 8 GB+ recommended under concurrent load
- At least 30 GB disk — the backend image alone is ~11 GB (sentence-transformer model baked in); budget an additional 5–10 GB for the PostgreSQL data volume (`./volumes/app-db/`) depending on query history and cache volume, plus headroom for Docker build cache and OS
- A domain name with DNS pointing to the server (for HTTPS)
- At least one LLM API key (see [Provider Setup](../user-guide/06_llm-providers.md))

---

## 1. Prepare the Environment

Clone the repository on your server:

```bash
git clone https://github.com/savvina-ai/savvina
cd savvina
cp .env.example .env
```

Follow [Quick Start steps 2 and 3](../../README.md#2-generate-required-secrets) to generate all required secrets (`ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `APP_DB_PASSWORD`, and the sample database passwords) and to add at least one LLM API key.

> **Production note — `ENCRYPTION_KEY`:** Generate it once and **never change it**. Rotating the key requires re-encrypting every stored credential. Use a unique key per environment; never reuse a development key in production.

### Restrict File Permissions

The `.env` file contains secrets — ensure it is readable only by root and the process user:

```bash
chmod 600 .env
```

---

## 2. Choose a Database Mode

### Option A — Local Docker PostgreSQL (default)

Set `COMPOSE_PROFILES=local-db` in `.env` and set `APP_DB_PASSWORD`. The `DATABASE_URL` must use the Docker service name as host:

```bash
COMPOSE_PROFILES=local-db
APP_DB_PASSWORD=<strong-password>
DATABASE_URL=postgresql+asyncpg://savvina:<strong-password>@db:5432/savvina_app
```

### Option B — External / Managed PostgreSQL

If you are using a managed PostgreSQL (AWS RDS, GCP Cloud SQL, Supabase, Neon, Azure, etc.):

1. Leave `COMPOSE_PROFILES` unset (or remove it) — the bundled `db` container will not start.
2. Remove or omit `APP_DB_PASSWORD`.
3. Set `DATABASE_URL` to your provider's connection string:

```bash
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@your-db-host.example.com:5432/savvina_app
# Append ?ssl=require if your provider enforces SSL (most do)
```

The database user needs `CREATE TABLE`, `ALTER TABLE`, and `CREATE INDEX` privileges. Migrations run automatically on startup.

See [External / Managed PostgreSQL](../infrastructure/docker.md#external--managed-postgresql) for full details.

---

## 3. Build and Start

```bash
docker compose up --build -d
```

This starts the core services:
- `backend` — FastAPI API on port 8000
- `frontend` — React app served by Nginx on port 3000
- `db` — PostgreSQL app database (only when `COMPOSE_PROFILES` includes `local-db`)
- `sample-postgres` and `sample-mysql` — pre-seeded demo databases (only when `COMPOSE_PROFILES` includes `test-dbs`; optional)

Verify all services are healthy:

```bash
docker compose ps
```

All three services should show `healthy` or `running`.

Check the backend log for startup confirmation:

```bash
docker compose logs backend --tail 30
```

You should see:
```
INFO:     Alembic migrations applied
INFO:     Embedding model loaded
INFO:     Uvicorn running on http://0.0.0.0:8000
```

The embedding model download happens automatically on first startup and is cached in the Docker image layer.

---

## 4. Create the Admin Account

On a fresh deployment, the **first person to open the browser** is taken to the **Create Admin Account** screen. Enter an email and a password (minimum 12 characters) to create the account. This screen is only shown once — the registration endpoint is permanently closed as soon as any account exists.

---

## 5. Configure HTTPS

TLS is handled by the Nginx server inside the `frontend` container. You supply the certificate files; the stack does not generate or renew them.

### Obtain a certificate

**Let's Encrypt (recommended):**

```bash
apt install certbot -y
certbot certonly --standalone -d yourdomain.com
```

**Commercial or internal CA:** obtain `fullchain.pem` (certificate + chain) and `privkey.pem` (private key) from your provider.

### Install the certificate

Copy your cert and key into the `volumes/certs/` directory on the server:

```bash
cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem volumes/certs/
cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   volumes/certs/
```

Then update the filenames in `frontend/nginx.conf`:

```nginx
ssl_certificate     /etc/nginx/certs/fullchain.pem;
ssl_certificate_key /etc/nginx/certs/privkey.pem;
```

Rebuild the frontend image once so Nginx picks up the config change:

```bash
docker compose build frontend && docker compose up -d frontend
```

For subsequent cert renewals (no config change), you only need to copy the new files and reload Nginx — no rebuild required:

```bash
# Copy renewed certs
cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem volumes/certs/
cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   volumes/certs/

# Reload Nginx inside the running container
docker compose exec frontend nginx -s reload
```

### Update CORS_ORIGINS

Add your production domain to `CORS_ORIGINS` in `.env`:

```bash
CORS_ORIGINS=["https://yourdomain.com"]
```

Then restart the backend to apply:

```bash
docker compose restart backend
```

### Automate Let's Encrypt renewal

Add a cron job on the host to renew and reload:

```bash
# /etc/cron.d/savvina-certbot
0 3 * * * root certbot renew --quiet && \
  cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem /path/to/savvina/volumes/certs/ && \
  cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   /path/to/savvina/volumes/certs/ && \
  docker compose -f /path/to/savvina/docker-compose.yaml exec frontend nginx -s reload
```

---

## 6. Expose Ports (or Keep Internal)

By default, `docker-compose.yaml` publishes:

| Port | Service |
|---|---|
| `3000` | Frontend (React/Nginx) |
| `8000` | Backend API (FastAPI) |

**For production:** do not expose `8000` to the internet. Only expose `3000` (or let Nginx be the single entry point and bind all services to `127.0.0.1`):

```yaml
# Override in docker-compose.override.yaml
services:
  backend:
    ports:
      - "127.0.0.1:8000:8000"  # only accessible from localhost
  frontend:
    ports:
      - "127.0.0.1:3000:80"
```

---

## 7. Persistent Data

All application data lives in `volumes/`:

```
volumes/
├── app-db/            ← PostgreSQL data (local-db profile only — see below)
├── sample-postgres/   ← Sample PostgreSQL test data (test-dbs profile only)
└── sample-mysql/      ← Sample MySQL test data (test-dbs profile only)
```

These are bind-mounted from the host, so they survive container restarts and image rebuilds. Directories for sample databases are only created when the `test-dbs` profile is active.

**Local Docker DB (`local-db` profile):** `volumes/app-db/` holds all user accounts, saved connections (with encrypted credentials), chat history, and the query cache. Back it up regularly with `pg_dump`:

```bash
# local-db profile only
docker compose exec db pg_dump -U savvina savvina_app > backup.sql
```

**External / managed PostgreSQL:** `volumes/app-db/` is never created. Backups are the responsibility of your cloud provider — enable automated snapshots and PITR in your provider's console.

---

## 8. Starting with Sample / Test Databases (Optional)

The `test-dbs` profile starts two pre-seeded databases for evaluating Savvina AI without connecting to a real data source:

| Service | Engine | Port | Database |
|---|---|---|---|
| `sample-postgres` | PostgreSQL 16 | 5435 | `savvina_test` |
| `sample-mysql` | MySQL 8.0 | 3307 | `sample_delivery` |

To start them alongside the core stack:

```bash
# With local Docker DB:
docker compose --profile local-db --profile test-dbs up -d

# With external/managed PostgreSQL (omit local-db):
docker compose --profile test-dbs up -d
```

Or set `COMPOSE_PROFILES=local-db,test-dbs` in `.env` and run `docker compose up -d`.

Set the required passwords in `.env` before starting:

```bash
SAMPLE_POSTGRES_PASSWORD=<strong-password>
SAMPLE_MYSQL_ROOT_PASSWORD=<strong-password>
SAMPLE_MYSQL_PASSWORD=<strong-password>
```

> **Production note:** The sample databases contain demo data only. Do not use them as a primary data source.

---

## 9. Starting with Local LLM (Ollama)

To run a local Ollama instance (requires GPU for acceptable performance):

```bash
docker compose --profile local-llm up -d
```

Then pull a model:

```bash
docker compose exec ollama ollama pull llama3
```

The Ollama container is accessible from the backend at `http://ollama:11434` (within the Docker network). Configure it in the UI via **Settings → Providers → Add Provider → Ollama**.

---

## 10. Updating

```bash
git pull
docker compose build backend frontend
docker compose up -d
```

The `volumes/` directory is not touched during updates. Database schema migrations are applied automatically by `alembic upgrade head` in `entrypoint.sh` before uvicorn starts.

---

## 11. Environment Variables Reference

All variables go in `.env` at the project root. See [Configuration](../getting-started/02_configuration.md) for the complete list.

**Production checklist:**

| Variable | Production value |
|---|---|
| `ENCRYPTION_KEY` | Unique Fernet key (never reuse across environments) |
| `JWT_SECRET_KEY` | At least 32 random characters |
| `LOG_LEVEL` | `WARNING` or `INFO` (not `DEBUG`) |
| `DEBUG` | `false` |
| `DEFAULT_ROW_LIMIT` | `500` or `1000` (cap at reasonable size) |
| `DEFAULT_QUERY_TIMEOUT` | `30` seconds |
| `VERIFY_SSL` | `true` (only set `false` if behind a TLS-inspecting proxy) |

---

## 12. Health Check

The backend exposes `GET /health`:

```bash
curl http://localhost:8000/health
# {"status": "ok", "app": "Savvina AI"}
```

Docker Compose uses this endpoint automatically with a 20-second start period and 5-second intervals. The frontend waits for the backend to be healthy before starting.

---

## 13. Corporate Environments (TLS Proxy)

If your network uses a TLS-intercepting proxy (e.g., Zscaler, Netskope, Palo Alto), LLM API calls may fail with certificate errors. Set in `backend/.env`:

```
VERIFY_SSL=false
```

This disables SSL verification for all LLM provider HTTP clients. It does **not** affect the database SSL connections (asyncpg uses its own TLS stack).

---

## Troubleshooting

### Backend won't start

```bash
docker compose logs backend
```

Common causes:
- `ENCRYPTION_KEY` missing or malformed (must be a valid Fernet key)
- Port 8000 already in use on the host

### Embedding model download fails

The model (`BAAI/bge-small-en-v1.5`) is downloaded from HuggingFace Hub at first startup. If the server has no internet access, you must pre-bake the model into the Docker image. Add to the `Dockerfile`:

```dockerfile
ENV FASTEMBED_CACHE_PATH=/app/fastembed_cache
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5').embed(['warmup']))"
```

### LLM calls fail with SSL errors

Set `VERIFY_SSL=false` in `backend/.env` (see corporate environment note above).

### Database connection errors

**Local Docker DB (`local-db` profile):** check that the `db` container is healthy:

```bash
docker compose ps db
docker compose logs db --tail 20
```

Common causes:
- `APP_DB_PASSWORD` not set in `.env`
- `COMPOSE_PROFILES=local-db` missing from `.env` (container won't start without it)
- `DATABASE_URL` still pointing to `localhost:5434` — inside Docker the host must be `db`, not `localhost`
- Port 5434 already in use on the host
- Volume permission issue on Linux (set `LOCAL_UID`/`LOCAL_GID` in `.env` to match your host user)

**External / managed PostgreSQL:** check backend logs for the specific asyncpg error:

```bash
docker compose logs backend --tail 30
```

Common causes:
- Wrong hostname, port, user, or password in `DATABASE_URL`
- Missing `?ssl=require` (most managed providers enforce SSL)
- Firewall or security group not allowing the Docker host's outbound IP
