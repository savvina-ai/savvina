# MySQL Connector

Savvina AI supports MySQL 8.0+ as a first-class data source via the `MySQLDataSource` adapter.
It uses `aiomysql` — a native async MySQL driver — for all connection and query operations.

---

## Connecting

| Field | Required | Example | Notes |
|---|---|---|---|
| Connection name | Yes | `Analytics DB` | Display label in the UI |
| Host | Yes | `mysql.example.com` | Hostname or IP. Use `host.docker.internal` for a DB on your host machine outside Docker |
| Port | Yes | `3306` | Default MySQL port |
| Database | Yes | `myapp` | Database (schema) name |
| Username | Yes | `readonly_user` | Use a dedicated read-only account (see below) |
| Password | Yes | — | Fernet-encrypted before storage; never logged |

The password is stored Fernet-encrypted in PostgreSQL and decrypted only when opening a connection.

---

## Schema Introspection

When a connection is saved (or refreshed), the adapter queries `INFORMATION_SCHEMA` — no user
tables are scanned.

| Metadata | Source |
|---|---|
| Tables and columns | `INFORMATION_SCHEMA.COLUMNS` |
| Column data types | `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` |
| Row counts (approx.) | `INFORMATION_SCHEMA.TABLES.TABLE_ROWS` |
| Column comments | `INFORMATION_SCHEMA.COLUMNS.COLUMN_COMMENT` |
| Sample values | `SELECT DISTINCT <column> FROM <table> LIMIT 5` |

**Privacy controls flow through introspection:**

- Schemas, tables, or columns in the `excluded_*` lists are never fetched or passed to the LLM.
- Columns matching `sensitive_column_patterns` (e.g., `email`, `password`, `ssn`) have sample
  values suppressed.
- `include_row_counts`, `include_column_comments`, and `include_sample_values` can each be
  independently disabled per connection.

> **Note:** MySQL's `INFORMATION_SCHEMA.TABLES.TABLE_ROWS` is an estimate based on InnoDB
> statistics, not an exact count. It may differ significantly from `COUNT(*)` on large tables.

---

## Query Validation

All generated SQL passes through `MySQLSQLValidator` before execution:

| Check | Detail |
|---|---|
| Statement type | Only `SELECT` and `WITH` (CTE) are permitted |
| DML blocking | `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REPLACE` are rejected |
| DDL blocking | `CREATE`, `DROP`, `ALTER`, `RENAME` are rejected |
| Dangerous statements | `LOAD DATA`, `SELECT INTO OUTFILE`, `CALL` are blocked |
| Auto-limit | A `LIMIT` clause is injected if none is present (default: 1,000 rows) |

---

## Read-Only Account (Recommended)

Always connect with a dedicated read-only MySQL user:

```sql
-- Create a read-only user
CREATE USER 'savvina_reader'@'%' IDENTIFIED BY 'your-password';

-- Grant read access to all tables in your database
GRANT SELECT ON myapp.* TO 'savvina_reader'@'%';

-- Apply changes
FLUSH PRIVILEGES;
```

Even though the validator blocks DML/DDL, a read-only user provides defence in depth.

---

## MySQL vs PostgreSQL Dialect

The LLM is given MySQL-specific system prompt additions so it generates correct dialect SQL:

- Uses `INFORMATION_SCHEMA` terminology in examples
- Backtick quoting for identifiers (`` `table_name` ``) instead of double-quotes
- `LIMIT` without `OFFSET` when only limiting rows
- Avoids PostgreSQL-specific functions (`pg_*`, `::` cast syntax, `ILIKE`)

---

## Bundled Sample Database

The `docker-compose.yaml` includes a pre-seeded MySQL 8.0 sample database for evaluation:

| Field | Value |
|---|---|
| Host | `sample-mysql` (within Docker network) or `localhost` (from host) |
| Port | `3306` (Docker network) or `3307` (host port) |
| Database | `sample_delivery` |
| Username | `savvina` |
| Password | *(value of `SAMPLE_MYSQL_PASSWORD` from your `.env`)* |

Tables: `restaurants`, `customers`, `orders`, `order_items`, `drivers`.

---

## File Map

| File | Role |
|---|---|
| `backend/app/datasources/adapters/mysql.py` | Full adapter implementation |
| `backend/app/datasources/validators/mysql_validator.py` | SQL validator (SELECT-only allowlist) |
| `backend/tests/test_datasources/test_mysql.py` | Adapter unit tests |
