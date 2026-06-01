# PostgreSQL Connector

Savvina AI supports PostgreSQL as a first-class data source via the `PostgreSQLDataSource` adapter.
It uses `asyncpg` — a purpose-built async driver — for all connection and query operations.

---

## Connecting

| Field | Required | Example | Notes |
|---|---|---|---|
| Connection name | Yes | `Production DB` | Display label in the UI |
| Host | Yes | `db.example.com` | Hostname or IP. Use `host.docker.internal` for a DB on your host machine outside Docker |
| Port | Yes | `5432` | Default PostgreSQL port |
| Database | Yes | `myapp` | Database name |
| Username | Yes | `readonly_user` | Use a dedicated read-only role (see below) |
| Password | Yes | — | Fernet-encrypted before storage; never logged |
| SSL Mode | Yes | `prefer` | One of: `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |

The password is stored Fernet-encrypted in the application database and decrypted only when opening a connection.

---

## Schema Introspection

When a connection is saved (or refreshed), the adapter queries `information_schema` to build the
schema. No user tables are scanned — only metadata catalog views are read.

| Metadata | Source |
|---|---|
| Tables and columns | `information_schema.columns` |
| Column data types | `information_schema.columns.data_type` |
| Row counts (approx.) | `pg_stat_user_tables.n_live_tup` |
| Column comments | `pg_description` via `col_description()` |
| Sample values | `SELECT DISTINCT <column> FROM <table> LIMIT 5` |

**Privacy controls flow through introspection:**

- Schemas, tables, or columns in the `excluded_*` lists are never fetched or passed to the LLM.
- Columns matching `sensitive_column_patterns` (e.g., `email`, `password`, `ssn`) have sample
  values suppressed — the column appears in the schema description but without examples.
- `include_row_counts`, `include_column_comments`, and `include_sample_values` can each be
  independently disabled per connection.

---

## Query Validation

All generated SQL passes through `PostgreSQLValidator` before execution:

| Check | Detail |
|---|---|
| Statement type | Only `SELECT` and `WITH` (CTE) are permitted |
| DML blocking | `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `MERGE` are rejected |
| DDL blocking | `CREATE`, `DROP`, `ALTER`, `RENAME` are rejected |
| Dangerous functions | `pg_sleep`, `pg_read_file`, `pg_write_file`, `lo_export`, `copy_to` are blocked |
| Privilege escalation | `SET ROLE`, `SET SESSION AUTHORIZATION`, `RESET` are blocked |
| Auto-limit | A `LIMIT` clause is injected if none is present (default: 1,000 rows) |

Validation is based on `sqlparse` token analysis — it runs before any database connection is opened.

---

## Read-Only Role (Recommended)

Always connect with a dedicated read-only PostgreSQL role:

```sql
-- Create a read-only role
CREATE ROLE savvina_reader WITH LOGIN PASSWORD 'your-password';

-- Grant access
GRANT CONNECT ON DATABASE myapp TO savvina_reader;
GRANT USAGE ON SCHEMA public TO savvina_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO savvina_reader;

-- Cover future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO savvina_reader;
```

Even though the validator blocks DML/DDL, a read-only role provides defence in depth.

---

## SSL Configuration

| Mode | Behaviour |
|---|---|
| `disable` | No SSL — use only on trusted private networks |
| `allow` | Connect without SSL; upgrade to SSL if the server offers it |
| `prefer` | Try SSL first; fall back to plaintext if the server doesn't support it |
| `require` | Require SSL; do not verify the server certificate |
| `verify-ca` | Require SSL and verify the server certificate against a known CA |
| `verify-full` | Require SSL, verify certificate, and verify the hostname |

For connections over the public internet, use `require` or `verify-full`.

---

## Bundled PostgreSQL Database

The `docker-compose.yaml` includes a PostgreSQL 16 instance (`sample-postgres`) for development and testing:

| Field | Value |
|---|---|
| Host | `sample-postgres` (within Docker network) or `localhost` (from host) |
| Port | `5432` (Docker network) or `5434` (host port) |
| Database | `savvina_test` |
| Username | `savvina` |
| Password | *(value of `SAMPLE_POSTGRES_PASSWORD` from your `.env`)* |
| SSL Mode | `disable` |

The database is seeded on first startup by scripts in the `initdb/` directory.

---

## File Map

| File | Role |
|---|---|
| `backend/app/datasources/adapters/postgresql.py` | Full adapter implementation |
| `backend/app/datasources/validators/postgresql_validator.py` | SQL validator (SELECT-only allowlist) |
| `backend/tests/test_datasources/test_postgresql.py` | Adapter unit tests |
