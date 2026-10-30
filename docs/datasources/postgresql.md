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
| Sample values | `pg_stats.most_common_vals` (catalog statistics; only when **Include sample values** is on) |

**Privacy controls flow through introspection:**

- Schemas, tables, or columns in the `excluded_*` lists are never fetched or passed to the LLM.
- Columns matching `sensitive_column_patterns` (e.g., `email`, `password`, `ssn`) or listed in
  `sensitive_columns` have sample
  values suppressed — the column appears in the schema description but without examples.
- `include_row_counts`, `include_column_comments`, and `include_sample_values` can each be
  independently toggled per connection. `include_sample_values` and `include_column_comments`
  are off by default.

---

## Query Validation

All generated SQL passes through `PostgreSQLValidator` before execution:

| Check | Detail |
|---|---|
| Statement type | Only `SELECT` and `WITH` (CTE) are permitted |
| DML blocking | `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `MERGE` are rejected |
| DDL blocking | `CREATE`, `DROP`, `ALTER`, `RENAME` are rejected |
| Dangerous functions | `pg_sleep`, `pg_read_file`, `pg_write_file`, `lo_export`, `copy_to`, `set_config` are blocked |
| Privilege escalation | `SET ROLE`, `SET SESSION AUTHORIZATION`, `RESET` are blocked |
| Writing keywords | `SELECT … INTO` is rejected (`INTO` as a keyword; the word in a comment, string or quoted identifier is allowed) |
| Auto-limit | A `LIMIT` clause is injected if none is present (default: 1,000 rows) |

Validation is based on `sqlparse` token analysis — it runs before any database connection is opened.

Every statement then runs inside a read-only transaction (asyncpg `transaction(readonly=True)`). A write the validator misses, such as `nextval()`, is refused by PostgreSQL with SQLSTATE `25006`.

---

## Read-Only Role (Recommended)

Always connect with a dedicated read-only PostgreSQL role:

```sql
-- Create a read-only role
CREATE ROLE savvina_reader LOGIN PASSWORD '<choose a password>';

-- Grant access
GRANT CONNECT ON DATABASE "myapp" TO savvina_reader;
GRANT USAGE ON SCHEMA "public" TO savvina_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO savvina_reader;

-- Cover future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA "public" GRANT SELECT ON TABLES TO savvina_reader;
```

Savvina validates every statement as a read-only SELECT and also runs it inside a database-enforced read-only transaction, so a write cannot succeed even if the text check is fooled. A read-only role is a third layer: it also limits what the connection can *read*.

Savvina checks the role when a connection is created or its details are saved, on **Check again** (a connection test), and on a privacy save that changes the row filters or the excluded tables, and shows the findings in the connection panel (see [Privacy Controls](../user-guide/04_privacy-controls.md#privilege-check)). It reads, from the catalog only:

- `pg_roles` (superuser, `BYPASSRLS`);
- `pg_class` and `pg_policies` for the row-filtered and excluded tables (`relrowsecurity`, `relforcerowsecurity`; `pg_has_role` for whether the role is the owner or inherits the owner's privileges, and for whether a `SELECT`/`ALL` policy applies to the role directly, through `PUBLIC` or through inherited membership);
- `has_schema_privilege` and `has_table_privilege` (a table counts as readable only with `USAGE` on its schema as well);
- the relation and column ACLs (`aclexplode` over `relacl` and `attacl`) of every non-system table (`pg_catalog`, `information_schema`, `pg_toast*` and `pg_temp*` are left out; tables, partitioned tables, views and foreign tables, not materialized views), restricted to the grants the role holds directly, through a role it inherits or through `PUBLIC`: the `INSERT`, `UPDATE` and `DELETE` grants and the column-level `INSERT` and `UPDATE` grants for the write finding, and the grantees of `SELECT` on the excluded tables. A `REVOKE` therefore names the grantee that holds the privilege, and a table the role owns (or whose owner it inherits) is reported as owned, not as a grant. At most 500 write grants are read;
- `pg_auth_members` for memberships in `pg_read_all_data` and `pg_write_all_data` (PostgreSQL 14+), which give access with no grant on any table. Only memberships whose privileges are inherited count: on PostgreSQL 16 the membership's `inherit_option`, on 14 and 15 the member's `rolinherit`. The grantor is read too, and the `REVOKE` adds `GRANTED BY <grantor>` when the grantor is not the bootstrap superuser;
- `pg_namespace` for the schemas of the dedicated-role script;
- outside `pg_catalog`, `pg_proc` with `has_function_privilege` for the functions that run SQL from a string: the `dblink` and `tablefunc` extensions, and any user function with one of those names.

A superuser or `BYPASSRLS` role, or the owner of a table without `FORCE ROW LEVEL SECURITY` (or a role that inherits the owner's privileges), makes native row-level security inert for the connection: the policies exist, the engine does not apply them to this role. For a superuser the grant and membership scans are skipped, since every privilege follows from it; the finding is the script that creates a dedicated read-only role.

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
