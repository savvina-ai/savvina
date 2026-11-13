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

All generated SQL passes through `MySQLValidator` before execution:

| Check | Detail |
|---|---|
| Statement type | The first word after comments and any opening parentheses must be `SELECT` or `WITH` (`(TABLE t)` and `(VALUES …)` are refused); `SET` is refused with its own message. A `#` line is a comment whatever it says, so one before the `SELECT` is skipped. `TABLE` is refused anywhere as a code word, not only first (`SELECT * FROM (TABLE t) s`, `… UNION TABLE t`): it reads a table no `FROM` names. A name right after a dot (`t.table`) passes, except in a text with a backslash directly before a quote or a dollar-quote opener, where every `TABLE` word is refused |
| Executable comments | `/*! … */` and `/*M! … */` (either case) before the first word are refused unless empty, because the server may skip them: `/*!NNNNN …*/` runs only on a server of at least that version, and MySQL skips MariaDB's `/*M! …*/`, so `/*!99999 SELECT 1 */ SHOW TABLES` would show a read while the server runs the `SHOW`. Only an empty one (`/*!*/`, whitespace alone) is a gap. Three refusals: `Query is a SET statement, which is not allowed` (content or raw text opens with `SET`), `Only SELECT queries and CTEs (WITH) are allowed. Got: SHOW` (content opens with another statement, naming the word), otherwise `Query opens with an executable comment (/*! or /*M!), which is not allowed`. After the first word an unversioned one (`SELECT /*! STRAIGHT_JOIN */ …`) stays allowed, and a versioned `/*!NNNNN …*/` or a non-empty `/*M! …*/` is refused wherever it stands (`Query holds a versioned or MariaDB-only executable comment (/*!NNNNN or /*M!), which is not allowed`): MySQL 8 skips `/*!99999 ' */` quote and all and runs the `/*! INTO @v */` after it, which no single reading of the two shows |
| DML blocking | `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` are rejected, and the `REPLACE INTO` statement (also by the first-word and `INTO` rules). The `REPLACE(str, from, to)` string function passes |
| DDL blocking | `CREATE`, `DROP`, `ALTER`, `RENAME` are rejected |
| Dangerous statements | `LOAD DATA`, `SELECT INTO OUTFILE`, `CALL` are blocked |
| Variable assignment | `@v := …` is rejected: the value could be read back as `@v` past result masking |
| Writing keywords | Any `SELECT … INTO` (`@var`, `OUTFILE`, `DUMPFILE`) is rejected (`INTO` as a keyword; the word in a comment, string or quoted identifier is allowed), also inside `/*! … */` (after `--` too: `--/*! 0 INTO @v*/` is two minus signs and a comment the server runs), after a `#` comment (`INTO#x` ends the word at the `#`), after `--1` or `--` and a no-break space (two minus signs, not a comment: MySQL opens a `--` comment only before an ASCII whitespace or control character), after a dot (`SELECT 7.INTO @v`: the number `7.`, then the clause) and behind an exponent (`1E5INTO @v`, `7.E5INTO @v`). Where a backslash stands directly before a quote, or a dollar-quote opener (`$$`, `$tag$`) appears anywhere, `INTO` is refused as any word, a comment's or a literal's too: sqlparse reads `\'` as an escaped quote while under `NO_BACKSLASH_ESCAPES` `'a\'` is a whole literal. A backslash elsewhere (`'\s+'`) and a JSON path (`'$.address'`) change nothing, so `JSON_EXTRACT(doc, '$.k')` beside `LIKE '%logged into%'` passes. An unquoted column called `into` after a dot (`t.into`) is refused with it: quote the name |
| Share locks | `LOCK IN SHARE MODE`, `FOR SHARE`, `FOR KEY SHARE` as code words, in every comment form and glued to a number (`1.E0FOR SHARE`); a literal `'lock in share mode'` passes. In a text with a backslash before a quote or a dollar-quote opener (the `INTO` row's trigger) the clause may start at any word, a literal's too, and its words are still read with nothing but whitespace and comments between them: `'$$/* x' <> '' FOR /* y */ SHARE` is refused, `'for' AS f, share` passes |
| Auto-limit | `LIMIT` (default 1,000 rows) is appended after the last code token when the query carries neither its own `LIMIT` nor its own `FETCH FIRST/NEXT` clause (MariaDB 10.6+). A clause inside a subquery or a CTE bounds only that subquery and does not count, and neither does the word `LIMIT` or `FETCH` in a comment or a literal (`WHERE note = 'limit'`; except in a text with a backslash directly before a quote or a dollar-quote opener, where the word `LIMIT` anywhere counts), or as part of a name (`s.fetch FIRST`, `a$fetch FIRST`, the `@fetch` user variable). The row filter uses the same rule (`has_row_limit`) |
| Multi-statement | A second statement after `;`, with comments (`/* */`, `--`, `#`, `/*! */`) read as the server reads them. Every `;` counts, one inside a string literal too |
| `sql_mode` | Read once per pool at connect. `ANSI_QUOTES`, or a combination mode that includes it reported alone (`ANSI`, and MariaDB's `DB2`, `MAXDB`, `MSSQL`, `ORACLE`, `POSTGRESQL`), marks `"…"` as a name so result masking tests it; the validator refuses the statement forms that could change the mode mid-session |

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

Savvina checks the account when a connection is created or its details are saved, on **Check again** (a connection test), and on a privacy save that changes the row filters, the excluded tables or the excluded schemas, and shows the findings in the connection panel (see [Privacy Controls](../user-guide/04_privacy-controls.md#privilege-check)). It reads:

- `SHOW GRANTS` for the account, naming it explicitly (`` SHOW GRANTS FOR `u`@`h` `` with the value of `CURRENT_USER()`), because on MySQL 8 `SHOW GRANTS FOR CURRENT_USER()` folds the active roles in. If that statement fails, it falls back to `SHOW GRANTS FOR CURRENT_USER()` and the roles' share is attributed to the account;
- `SELECT CURRENT_ROLE()` for the active roles and, per active role, `` SHOW GRANTS FOR `u`@`h` USING role `` (MariaDB: `SHOW GRANTS FOR role`). What the role adds beyond the account's own grants is the role's share, so each `REVOKE` names the account or the role that holds the grant. A privilege held both directly and through a role is shown on the account, and a role whose grants cannot be read is attributed to the account. When MySQL prints the account's and a role's privileges together as `ALL PRIVILEGES`, the role's `REVOKE` can also name a privilege only the account holds; MySQL accepts it. A role the account holds but has not activated does not apply to the session and is not read;
- `information_schema.tables` for the row-filtered and excluded tables;
- `information_schema.schemata` for the databases of the dedicated-account script.

Column-level grants are kept with their columns.

It reports `ALL PRIVILEGES` at the global level, or `SUPER` together with every privilege of the write set below (MySQL 8 lists `ALL` one privilege at a time), as a superuser; `SUPER` without that as its own finding; any privilege of the write set granted at the global, database, table or column level; and `SELECT` on a table the connection excludes. The write set is every privilege that changes data, structure or files: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `FILE`, `ALTER`, `CREATE`, `INDEX`, `TRIGGER`, `CREATE ROUTINE`, `ALTER ROUTINE`, `EVENT`, `LOCK TABLES`, `CREATE VIEW`, `CREATE TEMPORARY TABLES`, `REFERENCES`, `CREATE TABLESPACE`, and `ALL PRIVILEGES` at any level; on MariaDB also `DELETE HISTORY`, which removes the history rows of a system-versioned table (the superuser rule does not need it). `SELECT`, `SHOW VIEW`, `EXECUTE`, `PROCESS`, `RELOAD`, `SHUTDOWN`, `REPLICATION *`, `CREATE USER`, `GRANT OPTION` and `USAGE` are not writes. At most 500 write grants are read. Each finding comes with a `REVOKE` for the grant level and the grantee it found, one line per level, so a table-level grant under a database-level one gets its own line. A database-level grant is revoked as `SHOW GRANTS` prints it: MySQL keeps the pattern escapes of the grant (`` `sample\_delivery`.* ``) and a `REVOKE` must name it that way. An unescaped `_` or `%` in a database-level grant is a wildcard; the check reads it as a literal name, so a table the pattern covers reads no privilege from it. `ALL PRIVILEGES` on a database is revoked as the write set minus the global-only `FILE` and `CREATE TABLESPACE`, and on a table as `INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, INDEX, TRIGGER, CREATE VIEW, REFERENCES`, so `SELECT` stays (MariaDB adds `DELETE HISTORY` at both levels); global `ALL PRIVILEGES` is the superuser finding. Grants `ON FUNCTION` and `ON PROCEDURE` are not read. `SUPER` is revoked from each grantee that holds it (the account or an active role). Column-level `SELECT` grants are not read.

Known limits (not tested on these servers):

- On MariaDB, a role granted to an active role is not expanded: its grants may be attributed to the outer role or not read at all, and grants to `PUBLIC` are not read.
- `SUPER` is deprecated since MySQL 8.0 and may be removed, so the superuser rule, which relies on it, may stop recognising a superuser account on later versions (not tested on 8.4); its write privileges are still reported.

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
