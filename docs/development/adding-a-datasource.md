# Adding a Data Source Adapter

This guide walks through adding a new data source type to Savvina AI.

---

## How Adapters Are Registered

Savvina AI uses a decorator-based registry pattern. When `main.py` starts, it imports the `datasources` package, which triggers all `@register_datasource(...)` decorators in adapter files. Each decorator registers the class in a global `_REGISTRY` dict keyed by `source_type`.

```
backend/app/datasources/
├── base.py             ← BaseDataSource ABC
├── models.py           ← ColumnInfo, TableInfo, QueryResult, etc.
├── registry.py         ← register_datasource(), create_datasource()
├── validators/
│   ├── base_sql_validator.py   ← BaseSQLValidator
│   └── postgresql_validator.py ← PostgreSQLValidator (extend this)
└── adapters/
    ├── __init__.py     ← imports all adapters (triggers registration)
    └── postgresql.py   ← reference implementation
```

---

## Step 1: Create the Adapter File

Create `backend/app/datasources/adapters/mysql.py`. Declare the class attributes and delegate validation to your validator (created in Step 3):

```python
from ..base import BaseDataSource
from ..registry import register_datasource
from ..validators.mysql_validator import MySQLValidator


@register_datasource("mysql")
class MySQLDataSource(BaseDataSource):
    source_type = "mysql"
    display_name = "MySQL"
    query_dialect = "MySQL"
    icon = "🐬"

    def __init__(self) -> None:
        self._pool = None
        self._validator = MySQLValidator()
```

---

## Step 2: Implement `BaseDataSource`

Every adapter must implement all abstract methods. Use [postgresql.py](../../backend/app/datasources/adapters/postgresql.py) as the reference implementation.

| Method | What it does |
|---|---|
| `connect(config)` | Creates a connection pool from the decrypted config dict. Returns `ConnectionResult(success, message, server_version)`. |
| `disconnect()` | Closes the pool. Always called in a `finally` block by the service layer. |
| `test_connection(config)` | Same as `connect()` but closes the pool immediately — used by `POST /api/v1/connections/test`, nothing is persisted. |
| `introspect(privacy)` | Queries system tables (`INFORMATION_SCHEMA`) to build a `DataSourceSchema`. Must respect all `PrivacySettings` exclusions throughout, and attach a column's values (sample values, an enum's member list in its type) only when `privacy.column_values_allowed(column)` is true. |
| `get_sample_values(schema, table, column, limit)` | Returns up to `limit` distinct non-null column values as strings. Only called for non-sensitive columns. |
| `execute_query(query, timeout, max_rows)` | Runs validated SQL inside a database-enforced read-only transaction (as `execute_query_stream` must too, if overridden), so a write the validator misses is refused by the engine; returns `QueryResult`. Set `truncated=True` if `len(rows) >= max_rows`. |
| `explain(sql, timeout)` | Plans `sql` (`EXPLAIN <sql>`) under the same read-only guards as `execute_query`, and under `timeout` where the engine bounds an `EXPLAIN` (PostgreSQL `statement_timeout`, MariaDB `max_statement_time`; MySQL's `MAX_EXECUTION_TIME` applies to `SELECT` only and aiomysql has no read timeout, so on MySQL the adapter itself bounds nothing once connected), and returns nothing. PostgreSQL plans through the extended protocol (`conn.fetch`), so exactly one statement runs. The connections router bounds the call with `asyncio.wait_for` at the query timeout plus five seconds, because MySQL applies none to an `EXPLAIN`; a MySQL plan that is cancelled closes its connection instead of resetting the session. MySQL plans with `sql_notes` off so the note carrying the rewritten statement (and the condition's literals) is never raised. The engine may still evaluate constant expressions while planning, including stored functions declared `DETERMINISTIC` on MySQL, and MySQL may read one row by unique key; volatile functions are not evaluated. Raises `ExplainError` (`datasources/errors.py`) with the engine's text (the driver exception as `__cause__`, for `classify_error`) when the engine refuses the statement. Used by the connections router to verify a row-filter condition at save time. |
| `audit_privileges(tables, timeout)` | Reads, from the catalog only, what the connection role can do, as `PrivilegeFacts` (`datasources/models.py`): `is_superuser` (MySQL: global `ALL PRIVILEGES`, or global `SUPER` plus every write privilege), `bypasses_rls`, `has_super` (MySQL, global `SUPER` without the superuser rule) with `super_grantees` (MySQL: the account or active roles that hold `SUPER` when `has_super`), `writes` (a `PrivilegeGrant` per write grant: scope, privileges, grantee and, for a column-level grant, its columns; every non-system table on PostgreSQL, every level on MySQL; capped at 500 by the adapter, with `writes_truncated` set when the cap cut the list; MySQL `writes` never holds `ALL PRIVILEGES` below the global level, the adapter expands it into the write privileges valid there), `predefined_roles` (PostgreSQL memberships in `pg_read_all_data` and `pg_write_all_data`, as `RoleMembership(role, member, grantor)` with `grantor None` for the bootstrap superuser), `schemas` (the non-system schemas or databases, for the dedicated-role script) and the SQL-from-string functions outside `pg_catalog` the role can execute. Per `(schema, table)` pair it returns a `TablePrivilegeFacts`: exists (on MySQL, visible to the account), `can_select` (on PostgreSQL together with `USAGE` on the schema), `can_write`, owner or a role inheriting the owner, RLS enabled/forced, whether a `SELECT`/`ALL` policy applies to the role, and `select_grants`, each path through which the role holds `SELECT` at the scope it is granted at, with its grantee. A `PrivilegeGrant.grantee` is what a `REVOKE` must name: `None` for ownership (PostgreSQL), `"PUBLIC"`, a role name, `"u@h"` for a MySQL account or role, a bare role name on MariaDB. A pair with `schema None` is a bare excluded-table entry, matched by name in every non-system schema. Runs under the engine's bounds as `explain` does; a driver exception passes through, `RuntimeError` when not connected. The adapter reports; `services/privilege_audit.evaluate` judges. Used by the connections router on create, test, privacy save and config save. |
| `validate_query(query)` | Delegates to the validator: `return self._validator.validate(query)`. |
| `format_schema_for_llm(schema, privacy)` | Formats `DataSourceSchema` as `CREATE TABLE` DDL strings for the LLM system prompt. |
| `get_system_prompt_additions()` | Returns a short string of dialect-specific SQL hints shown to the LLM. |
| `get_config_schema()` | Returns the JSON schema for the connection form (see below). |
| `describe_connection_error(text) -> str \| None` | Optional. A readable sentence for the privilege audit's `unavailable` reason, given the text of the connect or catalog failure; it must never copy the engine's text, which can name the user and host. The default returns `None` and the router redacts the text by error category. MySQL maps the error number at the start of the driver message to a sentence (`MySQL error N` for an unknown number). |

Two optional overrides affect privacy. `classify_error(exc)` maps a failed query onto an `ErrorCategory` (`datasources/errors.py`) from the engine's documented error codes; it must never raise, and the default `OTHER` gives the correction prompt and the user-facing error the most conservative treatment. Set the class attribute `double_quote_is_string_literal = True` when `"…"` is a string literal in your dialect (as on MySQL), so result masking does not read it as a column reference.

### `get_config_schema()` field types

`DynamicConnectionForm` renders any adapter's `config_schema` automatically — no frontend changes needed.

| Type | Behaviour |
|---|---|
| `string` | Plain text input |
| `integer` | Numeric input |
| `password` | Masked in UI; encrypted at rest before storage |
| `boolean` | Checkbox |
| `select` | Dropdown; provide an `options` list and a `default` |

---

## Step 3: Create the Validator

Create `backend/app/datasources/validators/mysql_validator.py` extending `BaseSQLValidator`.

`BaseSQLValidator.validate()` already handles: single-statement check, SELECT/WITH allowlist, blocked DML/DDL keywords, dangerous patterns, and auto-appending `LIMIT`. Your subclass only needs to add database-specific blocked functions and patterns.

See [postgresql_validator.py](../../backend/app/datasources/validators/postgresql_validator.py) as the reference.

---

## Step 4: Register the Adapter

Import your adapter in `backend/app/datasources/adapters/__init__.py`:

```python
from . import postgresql  # noqa: F401
from . import mysql       # noqa: F401  ← add this line
```

The import triggers `@register_datasource("mysql")` and the adapter immediately appears in `GET /api/v1/datasources` and the connection form dropdown. No other files need to change.

---

## Step 5: Add the Dependency

Add the async Python driver to `backend/requirements.txt`, then rebuild:

```bash
docker compose build backend
docker compose up -d backend
```

---

## Step 6: Write Tests

Add tests under `backend/tests/test_datasources/`. Follow the pattern in existing tests: mock the driver's connection pool, test `connect()` success and failure, and cover your validator's blocked patterns.

Run with:

```bash
.venv/bin/pytest backend/tests/ -v
```

---

## Checklist

- [ ] `adapters/mysql.py` — implements all abstract methods from `BaseDataSource`
- [ ] `validators/mysql_validator.py` — extends `BaseSQLValidator`
- [ ] `adapters/__init__.py` — imports new module to trigger registration
- [ ] `requirements.txt` — async driver added
- [ ] Tests pass: `.venv/bin/pytest backend/tests/ -v`
- [ ] `GET /api/v1/datasources` returns the new `source_type` in its list
- [ ] Connection form shows the new adapter in the source type dropdown
- [ ] `POST /api/v1/connections/test` successfully connects and returns `server_version`
- [ ] Schema introspection returns tables and columns for the target database
- [ ] Validated SELECT query executes and returns `QueryResult`
