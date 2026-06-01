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
| `introspect(privacy)` | Queries system tables (`INFORMATION_SCHEMA`) to build a `DataSourceSchema`. Must respect all `PrivacySettings` exclusions throughout. |
| `get_sample_values(schema, table, column, limit)` | Returns up to `limit` distinct non-null column values as strings. Only called for non-sensitive columns. |
| `execute_query(query, timeout, max_rows)` | Runs validated SQL read-only; returns `QueryResult`. Set `truncated=True` if `len(rows) >= max_rows`. |
| `validate_query(query)` | Delegates to the validator: `return self._validator.validate(query)`. |
| `format_schema_for_llm(schema, privacy)` | Formats `DataSourceSchema` as `CREATE TABLE` DDL strings for the LLM system prompt. |
| `get_system_prompt_additions()` | Returns a short string of dialect-specific SQL hints shown to the LLM. |
| `get_config_schema()` | Returns the JSON schema for the connection form (see below). |

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
