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

Via the UI: **Settings → Connections → [connection name] → Clear Cache**

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

Or use the **Settings → Connections → [connection name] → Examples** section in the UI.

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

Savvina AI serves HTTPS exclusively. The `frontend` nginx container listens on port 443 (mapped to `APP_PORT`, default `3000`) and hard-redirects all HTTP traffic from port 80. TLS is terminated at nginx using certificate files bind-mounted from the host.

### How it works

```
docker-compose.yaml:
  frontend:
    ports:
      - "${APP_PORT:-3000}:443"
    volumes:
      - ./volumes/certs:/etc/nginx/certs:ro   ← host directory
```

nginx expects exactly two files inside that directory (paths are baked into the image):

| File | Purpose |
|---|---|
| `volumes/certs/localhost+1.pem` | TLS certificate (or full chain) |
| `volumes/certs/localhost+1-key.pem` | Private key |

Place your certificate files there and restart the frontend — no rebuild required.

### Default development certificates

The repository ships with self-signed certificates generated by [`mkcert`](https://github.com/FiloSottile/mkcert) for `localhost` and `127.0.0.1`. Browsers will show a warning unless you install the mkcert root CA:

```bash
# Install mkcert (once per machine)
# macOS:  brew install mkcert
# Linux:  see https://github.com/FiloSottile/mkcert#installation

# Trust the root CA that signed the bundled certs
mkcert -install

# Or trust it manually — import volumes/certs/mkcert-rootCA.pem into
# your OS / browser certificate store.
```

To regenerate the dev certs (e.g. after the root CA changes):

```bash
mkcert -cert-file volumes/certs/localhost+1.pem \
       -key-file  volumes/certs/localhost+1-key.pem \
       localhost 127.0.0.1

docker compose restart frontend
```

### Production: use a real certificate

Replace the two files with your CA-signed certificate and key, keeping the same filenames:

```bash
# Example: certificate from Let's Encrypt / certbot
cp /etc/letsencrypt/live/example.com/fullchain.pem volumes/certs/localhost+1.pem
cp /etc/letsencrypt/live/example.com/privkey.pem   volumes/certs/localhost+1-key.pem

docker compose restart frontend
```

The container mounts `volumes/certs` read-only — nginx reads the files at startup. After replacing the files, a restart is all that is needed.

### Using a different hostname or filename

The certificate paths are hardcoded in [frontend/nginx.conf](../../frontend/nginx.conf). To use different filenames or add a `server_name` directive:

1. Edit `frontend/nginx.conf` — update `ssl_certificate`, `ssl_certificate_key`, and optionally add `server_name your.domain.com;`
2. Rebuild the frontend image:
   ```bash
   docker compose build frontend
   docker compose up -d frontend
   ```

### Certificate renewal

nginx loads the certificate at startup. After renewing or replacing the files in `volumes/certs/`, restart the frontend to pick up the new certificate:

```bash
docker compose restart frontend

# Verify the served certificate
openssl s_client -connect localhost:${APP_PORT:-3000} -servername localhost </dev/null 2>/dev/null \
  | openssl x509 -noout -dates
```

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
