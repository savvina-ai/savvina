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
| Database | Yes | `myapp` | Database (schema) the session starts in. It does not limit what the connection sees: see below |
| Username | Yes | `readonly_user` | Use a dedicated read-only account (see below) |
| Password | Yes | — | Fernet-encrypted before storage; never logged |

The password is stored Fernet-encrypted in PostgreSQL and decrypted only when opening a connection.

---

## Schema Introspection

When a connection is saved (or refreshed), the adapter queries `INFORMATION_SCHEMA` — no user
tables are scanned.

On MySQL a schema is a database, and introspection reads **every database the account can see**
(all but `information_schema`, `mysql`, `performance_schema` and `sys`), not only the one named
in the **Database** field. Their tables reach the LLM, and queries can join across them. To keep
the connection to one database, grant the account access to that database only (see
[Read-Only Account](#read-only-account-recommended)), or list the other databases under
**Excluded schemas** in the connection's privacy settings.

| Metadata | Source |
|---|---|
| Tables and columns | `INFORMATION_SCHEMA.COLUMNS` |
| Column data types | `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` |
| Row counts (approx.) | `INFORMATION_SCHEMA.TABLES.TABLE_ROWS` |
| Column comments | `INFORMATION_SCHEMA.COLUMNS.COLUMN_COMMENT` |
| Sample values | ENUM/SET members parsed from `INFORMATION_SCHEMA.COLUMNS.COLUMN_TYPE` (only when **Include sample values** is on; other columns have none) |

**Privacy controls flow through introspection:**

- Schemas, tables, or columns in the `excluded_*` lists are never fetched or passed to the LLM.
- Columns matching `sensitive_column_patterns` (e.g., `email`, `password`, `ssn`) or listed in
  `sensitive_columns` have sample
  values suppressed, and their comments are never sent.
- An ENUM/SET column's member list is part of its type (`enum('placed','confirmed',…)`). Unless
  the column's values are allowed (sample values on and the column not sensitive), the list is
  withheld from the type as `enum(…)`, at introspection and again whenever a cached schema is read.
- `include_row_counts`, `include_column_comments`, and `include_sample_values` can each be
  independently toggled per connection. `include_sample_values` and `include_column_comments`
  are off by default.

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
| Variable assignment | `@v := …` is rejected: the value could be read back as `@v` past result masking |
| Writing keywords | Any `SELECT … INTO` (`@var`, `OUTFILE`, `DUMPFILE`) is rejected (`INTO` as a keyword; the word in a comment, string or quoted identifier is allowed) |
| Auto-limit | A `LIMIT` clause is injected if none is present (default: 1,000 rows) |

Every statement then runs with `SET SESSION TRANSACTION READ ONLY` in effect, restored to `READ WRITE` afterwards. A write the validator misses is refused by the server with errno 1792.

---

## Read-Only Account (Recommended)

Always connect with a dedicated read-only MySQL user:

```sql
-- Create a read-only user
CREATE USER `savvina_reader`@`%` IDENTIFIED BY '<choose a password>';

-- Grant read access to all tables in your database
GRANT SELECT ON `myapp`.* TO `savvina_reader`@`%`;
```

Grant no other database: the connection sees every database the account can read.

Savvina validates every statement as a read-only SELECT and also runs it inside a database-enforced read-only transaction, so a write cannot succeed even if the text check is fooled. A read-only user is a third layer: it also limits what the connection can *read*.

Savvina checks the account when a connection is created or its details are saved, on **Check again** (a connection test), and on a privacy save that changes the row filters or the excluded tables, and shows the findings in the connection panel (see [Privacy Controls](../user-guide/04_privacy-controls.md#privilege-check)). It reads:

- `SHOW GRANTS` for the account, naming it explicitly (`` SHOW GRANTS FOR `u`@`h` `` with the value of `CURRENT_USER()`), because on MySQL 8 `SHOW GRANTS FOR CURRENT_USER()` folds the active roles in. If that statement fails, it falls back to `SHOW GRANTS FOR CURRENT_USER()` and the roles' share is attributed to the account;
- `SELECT CURRENT_ROLE()` for the active roles and, per active role, `` SHOW GRANTS FOR `u`@`h` USING role `` (MariaDB: `SHOW GRANTS FOR role`). What the role adds beyond the account's own grants is the role's share, so each `REVOKE` names the account or the role that holds the grant. A privilege held both directly and through a role is shown on the account, and a role whose grants cannot be read is attributed to the account. When MySQL prints the account's and a role's privileges together as `ALL PRIVILEGES`, the role's `REVOKE` can also name a privilege only the account holds; MySQL accepts it. A role the account holds but has not activated does not apply to the session and is not read;
- `information_schema.tables` for the row-filtered and excluded tables;
- `information_schema.schemata` for the databases of the dedicated-account script.

Column-level grants are kept with their columns.

It reports `ALL PRIVILEGES` at the global level, or `SUPER` together with every privilege of the write set below (MySQL 8 lists `ALL` one privilege at a time), as a superuser; `SUPER` without that as its own finding; any privilege of the write set granted at the global, database, table or column level; and `SELECT` on a table the connection excludes. The write set is every privilege that changes data, structure or files: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `FILE`, `ALTER`, `CREATE`, `INDEX`, `TRIGGER`, `CREATE ROUTINE`, `ALTER ROUTINE`, `EVENT`, `LOCK TABLES`, `CREATE VIEW`, `CREATE TEMPORARY TABLES`, `REFERENCES`, `CREATE TABLESPACE`, and `ALL PRIVILEGES` at any level; on MariaDB also `DELETE HISTORY`, which removes the history rows of a system-versioned table (the superuser rule does not need it). `SELECT`, `SHOW VIEW`, `EXECUTE`, `PROCESS`, `RELOAD`, `SHUTDOWN`, `REPLICATION *`, `CREATE USER`, `GRANT OPTION` and `USAGE` are not writes. At most 500 write grants are read. Each finding comes with a `REVOKE` for the grant level and the grantee it found, one line per level, so a table-level grant under a database-level one gets its own line. A database-level grant is revoked as `SHOW GRANTS` prints it: MySQL keeps the pattern escapes of the grant (`` `sample\_delivery`.* ``) and a `REVOKE` must name it that way. An unescaped `_` or `%` in a database-level grant is a wildcard; the check reads it as a literal name, so a table the pattern covers reads no privilege from it. `ALL PRIVILEGES` on a database is revoked as the write set minus the global-only `FILE` and `CREATE TABLESPACE`, and on a table as `INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, INDEX, TRIGGER, CREATE VIEW, REFERENCES`, so `SELECT` stays (MariaDB adds `DELETE HISTORY` at both levels); global `ALL PRIVILEGES` is the superuser finding. Grants `ON FUNCTION` and `ON PROCEDURE` are not read. `SUPER` is revoked from each grantee that holds it (the account or an active role). Column-level `SELECT` grants are not read.

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
