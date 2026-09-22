# Maintenance

Routine maintenance tasks for a running Savvina AI instance.

---

## Viewing Logs

```bash
# All services
docker compose logs -f

# Backend only (most useful for debugging)
docker compose logs backend -f

# Last 50 lines from each service
docker compose logs --tail 50
```

Log level is controlled by `LOG_LEVEL` in `.env` (default: `INFO`). Set to `DEBUG` temporarily to trace LLM prompts and SQL execution. The default `LOG_FORMAT` is `json` (machine-parseable, recommended for production). Set to `text` for human-readable output during local development.

---

## Backups

### What to Back Up

All persistent application state lives in the PostgreSQL `db` service (host port 5434). The following data is stored there:

- Saved data source connections (with Fernet-encrypted credentials)
- Chat sessions and message history
- Query cache entries and embeddings
- Verified example question→query pairs
- Semantic model definitions
- Provider configurations (with encrypted API keys)
- Application settings
- Users, organisations, audit logs

### Backup Command

Use `pg_dump` to create a consistent logical backup without stopping the application:

```bash
# Dump to a timestamped SQL file
docker compose exec db pg_dump -U savvina savvina_app \
  > backups/savvina-$(date +%Y%m%d).sql

# Or a compressed binary dump (smaller, faster to restore)
docker compose exec db pg_dump -U savvina -Fc savvina_app \
  > backups/savvina-$(date +%Y%m%d).dump
```

Copy the file off the host to your backup storage after the dump completes.

### Restore from Backup

```bash
# From a plain SQL dump
docker compose exec -T db psql -U savvina savvina_app \
  < backups/savvina-20260115.sql

# From a compressed binary dump
docker compose exec -T db pg_restore -U savvina -d savvina_app \
  backups/savvina-20260115.dump
```

The application picks up the restored data immediately — no restart required for a same-schema restore.

### Backup the Encryption Key

**The `ENCRYPTION_KEY` in `.env` is critical.** Without it, all stored encrypted credentials (database passwords, LLM API keys) are permanently unreadable. Back it up separately from the database, ideally in a secrets manager.

---

## Restoring from Backup

```bash
# Stop the application (optional but recommended for a clean restore)
docker compose stop backend

# Drop and recreate the database, then restore
docker compose exec db psql -U savvina -c "DROP DATABASE IF EXISTS savvina_app;"
docker compose exec db psql -U savvina -c "CREATE DATABASE savvina_app;"
docker compose exec -T db psql -U savvina savvina_app < backups/savvina-20260115.sql

# Restart the backend — Alembic migrations run automatically
docker compose start backend
```

---

## Encryption Key Rotation

Rotating the encryption key requires re-encrypting every stored secret in the database. **There is no built-in key rotation command.** The safest approach:

1. Export all connections and provider configs via the API (decrypted values not exposed — only metadata)
2. Stop the application
3. Replace `ENCRYPTION_KEY` in `.env` with a new Fernet key
4. Re-enter all connection credentials and API keys via the UI

This is intentionally manual — never expose decrypted credentials over the API.

---

## Clearing the Query Cache

The query cache stores LLM-generated SQL queries indexed by natural language question. Clear it when:
- The database schema changes significantly
- Many incorrect queries have accumulated
- You want to force re-generation of all cached queries

**Per-connection (recommended):**

Via the UI: **Settings → AI & Optimization → Query Cache → Clear All**. This clears the cache for the *active* connection — switch connections first if you need to clear a different one. Individual entries can be deleted from the same panel.

Via API:

```bash
curl -X DELETE http://localhost:8000/api/v1/chat/cache/<connection_id>
```

**All connections:**

```bash
docker compose exec db psql -U savvina savvina_app \
  -c "DELETE FROM query_cache;" \
  -c "SELECT 'Deleted ' || count(*) || ' cache entries' FROM query_cache;"
```

Note: Schema refresh (`POST /api/v1/connections/{id}/schema/refresh`) automatically clears the cache for that connection.

---

## Managing Verified Examples

Verified examples (created via thumbs-up feedback) are used as few-shot examples in LLM prompts. View and manage them via the API:

```bash
# List examples for a connection
curl http://localhost:8000/api/v1/chat/examples/<connection_id>

# Delete a specific example
curl -X DELETE http://localhost:8000/api/v1/chat/examples/<example_id>
```

Or use the **Settings → Examples Library** tab in the UI, which manages the examples of the active connection.

---

## Database Inspection

Connect to the PostgreSQL app database directly for inspection or manual queries:

```bash
# Open a psql session
docker compose exec db psql -U savvina savvina_app

# List all tables
docker compose exec db psql -U savvina savvina_app \
  -c "\dt"

# Row counts per table
docker compose exec db psql -U savvina savvina_app \
  -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
```

Key tables:

| Table | Contents |
|---|---|
| `connections` | Data source connections (encrypted config) |
| `provider_configs` | LLM provider configs (encrypted API keys) |
| `chat_sessions` | Conversation sessions |
| `chat_messages` | Individual messages with query/results/status |
| `query_cache` | Cached NL→SQL mappings with embeddings |
| `verified_examples` | Thumbs-up examples used for few-shot prompting |
| `app_settings` | Application-wide settings |
| `users` | User accounts |

---

## Updating the Application

```bash
# Pull latest changes
git pull

# Rebuild only changed images
docker compose build

# Restart services with new images (zero-downtime if using a load balancer)
docker compose up -d

# Verify health
docker compose ps
curl http://localhost:8000/health
```

Alembic migrations run automatically via `entrypoint.sh` (`alembic upgrade head`) before uvicorn starts. New tables and columns are applied without manual intervention.

---

## Upgrading from a TLS-terminating Release

Older releases had the `frontend` container terminate TLS itself and serve `https://…:3000`. That container now serves plain HTTP on port `8080` (published as `APP_PORT`, default `3000`); HTTPS moved to a reverse proxy you place in front (see [Deployment → Configure HTTPS](deployment.md#5-configure-https)). A few things change for anyone upgrading from that older setup:

- **Your browser may refuse the new `http://` URL.** If you ever loaded the old release over `https://<host>:3000`, HSTS may have your browser pinned to HTTPS for that host for up to a year — it will silently rewrite `http://` back to `https://` and then fail to connect, since nginx no longer speaks TLS. Clear the pinned policy for that host:
  - **Chrome:** go to `chrome://net-internals/#hsts`, enter the domain under "Delete domain security policies", and delete it.
  - **Firefox:** open the page, click the site information icon, and choose "Forget About This Site" (or clear the site's data).
  - Installs reached by IP literal (e.g. `192.168.1.50`) are unaffected — HSTS is never stored for bare IP addresses, only hostnames.
- **Update anything that hardcodes the old scheme.** Bookmarks, scripts, uptime checks, and `curl` commands using `https://…:3000` must change to `http://…:3000` (or to your reverse proxy's HTTPS URL, if you've put one in front).
- **Update a private `docker-compose.override.yaml`.** Any override that maps to container port `8443` (the old TLS listener) must change to `8080`, the container's current plain-HTTP port.
- **Update `CORS_ORIGINS` — but only if the origin is localhost or a private IP.** If you reach the UI at `http://localhost:3000`, `http://127.0.0.1:3000`, or a private LAN IP (`192.168.x`, `10.x`, `172.16-31.x`), change that entry's scheme from `https://` to `http://`. If you reach it by a hostname (e.g. `https://savvina.example.com`), **leave the entry as `https://`** and keep TLS terminated at a reverse proxy in front of the stack — the backend refuses to start with a plain-HTTP hostname origin, so rewriting it to `http://` turns a working install into a crash loop. See [Quickstart → Step 7](../getting-started/01_quickstart.md#step-7--create-your-admin-account) for the full explanation and worked examples.
- **Set `BEHIND_TLS_PROXY=true` if you reach the UI over `https://`.** The backend no longer works out for itself whether the browser was on HTTPS; you declare it. With an `https://` entry in `CORS_ORIGINS` and this flag unset, the backend refuses to start (`CORS origin '...' is https:// but BEHIND_TLS_PROXY is not set`). Add the line to `.env` and recreate the backend so it picks up the new environment (`docker compose up -d backend` — `restart` alone reuses the old one). Plain-`http://` localhost/LAN installs leave it unset. See [HTTPS Behind a Reverse Proxy](../getting-started/02_configuration.md#https-behind-a-reverse-proxy).
- **`volumes/certs/` is no longer read.** The frontend container never opens it now, so it's safe to delete. If you had a certbot cron job (or similar) copying renewed certificates into it, remove that cron job too — TLS termination now belongs at a reverse proxy in front of the stack, not inside this container. See [Deployment → Configure HTTPS](deployment.md#5-configure-https) for the current setup.

---

## Scaling Considerations

Savvina AI is designed for **single-instance deployment**:

- The `QueryCache` singleton (embedding model) lives in process memory — not shareable across processes
- The Fernet `ENCRYPTION_KEY` would need to be shared across instances

To scale horizontally, you would need to replace the in-process query cache with a shared cache (Redis + external embedding service). The PostgreSQL application database already supports concurrent connections from multiple backend instances natively.

For most internal analytics use cases, a single instance handles the load comfortably.

---

## Disk Space

Monitor disk usage periodically:

```bash
# Volume sizes on host
du -sh volumes/*/

# PostgreSQL database size
docker compose exec db psql -U savvina savvina_app \
  -c "SELECT pg_size_pretty(pg_database_size('savvina_app')) AS db_size;"

# Largest tables
docker compose exec db psql -U savvina savvina_app \
  -c "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS size FROM pg_class WHERE relkind='r' ORDER BY pg_total_relation_size(oid) DESC LIMIT 10;"

# Docker image sizes
docker images savvina*
```

The `query_cache` table grows with each new query. Each row stores:
- The question text and normalized form
- The generated SQL query
- A 384-dimensional float32 embedding vector (~1.5 KB per row)

For most deployments, the database stays well under 1 GB unless you have thousands of cached queries per connection.

---

## Monitoring

Savvina AI does not ship with a metrics endpoint. For production monitoring:

- **Uptime:** Add `GET /health` to your uptime monitoring service
- **Logs:** Ship Docker logs to a log aggregator (Loki, CloudWatch, etc.)
- **Cache performance:** `GET /api/v1/chat/cache/stats` returns `total_entries`, `hit_count`, `miss_count`, `hit_rate`, and `top_cached_queries` (top 5 most-hit questions)
- **Disk:** Set up alerts on `volumes/` disk usage

---

## HTTPS / TLS Configuration

The `frontend` nginx container serves plain HTTP on container port `8080` (mapped to `APP_PORT`, default `3000`) and does not terminate TLS — there are no certificate files to install, renew, or rebuild the image for. That is fine for `localhost` and trusted LAN use.

For anything reachable beyond your own machine, put a TLS-terminating reverse proxy (Caddy, nginx, Traefik, a cloud load balancer) in front of the frontend port and bind the frontend to `127.0.0.1`. Caddy is the least configuration, since it obtains and renews Let's Encrypt certificates on its own. See [Deployment → Configure HTTPS](deployment.md#5-configure-https) for a worked example, including the `BEHIND_TLS_PROXY=true` setting the backend needs and the streaming-timeout requirements the proxy must meet.

---

## Restarting Services

```bash
# Restart a single service
docker compose restart backend

# Full restart
docker compose down && docker compose up -d

# Restart without losing data (volumes preserved)
docker compose down   # volumes/ is NOT removed
docker compose up -d
```

`docker compose down --volumes` would delete the named `ollama_models` volume but NOT the bind-mounted `volumes/` directory. The application data is always safe.

Note that `docker compose restart` reuses each service's existing container as-is — it does not pick up edits to `.env`. If you changed a setting and need it applied, recreate the affected service instead: `docker compose up -d <service>`.
