# Guardrails

This document describes all safety and security guardrails in place across the Savvina stack, ordered from the network edge inward to the query result.

---

## 1. Network Edge — Middleware Stack

The middleware chain runs on every request before any auth or DB touch:

```
ScannerGuard → RequestID → CORS → OriginCheck → SecurityHeaders → SlowAPI → route handler
```

### ScannerGuardMiddleware (`main.py`)
Immediately rejects requests matching known exploit-scanner paths and payloads:
- Path patterns: `/cgi-bin/`, `.php`, `/wp-admin`, `/wp-login`, `/struts`, `/jndi:`, `/actuator`, `/console`, `/solr`, `/jenkins`, `/owa/auth`
- Query string patterns: `${jndi:` (Log4Shell)

Returns `400` before any application code runs. This keeps exploit noise out of logs and eliminates any chance of accidentally triggering route handlers on malformed paths.

### OriginCheckMiddleware (`main.py`)
Defense-in-depth CSRF guard. On mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`), if an `Origin` header is present, it must match the configured `cors_origins` allowlist. Requests without an `Origin` header (curl, server-to-server) are allowed through.

### SecurityHeadersMiddleware (`main.py`)
Appends OWASP-recommended response headers on every reply:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `Content-Security-Policy: default-src 'none'` on `/api/` routes; a tighter self-only policy on frontend assets
- `Cache-Control: no-store` on all `/api/v1/auth` responses

---

## 2. Rate Limiting (SlowAPI)

Per-endpoint limits enforced by SlowAPI keyed on user ID + connection ID where applicable:

| Endpoint | Limit |
|---|---|
| `POST /api/v1/chat` | 20 requests / minute |
| `POST /api/v1/connections/test` | 5 requests / minute |
| Auth endpoints (login, register, refresh, password reset) | Configurable via `AUTH_RATE_LIMIT` setting |

Exceeded limits return `429 Too Many Requests`.

---

## 3. Authentication & Authorisation

### JWT Validation (`auth/dependencies.py`)
Every protected route goes through the dependency chain:
`get_current_user → get_current_active_user → get_user_org_membership → require_role()`

Access tokens carry `{"sub": user_id, "org": org_id, "role": role, "type": "access"}` and are validated on every request. Expired or tampered tokens return `401`.

### Org Isolation
Every DB query on resource routers filters by `org_id` from the validated token. A bare `db.get(Model, id)` is never used in route handlers. Routes with `{org_id}` in the URL call `_check_org_match()` as the first line before any DB access.

### Refresh Token Security
Tokens are stored as `sha256(raw_token)`. Reuse of a revoked token triggers revocation of **all** tokens for that user. Rotation uses `SELECT FOR UPDATE` to prevent concurrent refresh races.

### Credential Encryption
Connection credentials (username, password) are stored Fernet-encrypted in `UserCredential`. They are never logged. The plaintext is merged into the connection config only at query time, in memory.

---

## 4. SQL Query Validation (`datasources/validators/`)

Every generated query passes through a two-layer validator before execution. This applies to LLM-generated queries, user-edited queries, and sorted re-executions.

### Layer 1 — BaseSQLValidator (shared across all SQL dialects)
- **Statement count**: rejects any input containing more than one SQL statement (`; \w` pattern)
- **Statement type**: accepts only `SELECT` and `WITH` (CTEs); rejects `INSERT`, `UPDATE`, `DELETE`, and all DDL
- **Blocked keywords**: whole-word scan for `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `REVOKE`, `MERGE`, `REPLACE`, `CALL`, `EXECUTE`, `EXEC`
- **Dangerous patterns**: `INTO OUTFILE`, `INTO DUMPFILE`, multi-statement semicolons
- **Auto-LIMIT injection**: appends `LIMIT {default_limit}` if no LIMIT clause is present, capping result rows

### Layer 2 — Dialect-specific validators
Additional checks layered on top of the base:

| Dialect | Blocked functions / patterns |
|---|---|
| **PostgreSQL** | `pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_export`, `lo_import`, `dblink`, `dblink_exec`; `COPY … TO`, `SET ROLE`, `SET SESSION` |
| **MySQL** | `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `GET_LOCK`, `RELEASE_LOCK`, `MASTER_POS_WAIT`; `LOAD DATA`, `SET GLOBAL`, `SET SESSION`, `FLUSH` |

---

## 5. Query Complexity Limits (`chat_service.py`)

Applied immediately after the read-only validator, before any DB connection is opened.

### CROSS JOIN rejection
Any query containing `CROSS JOIN` (case-insensitive) is rejected. Cartesian products are almost never intentional in NL-to-SQL and can produce result sets too large to handle.

Applies to: LLM-generated queries, user-edited queries, re-sorted queries.

### Large-table full-scan rejection
When schema metadata is available (LLM-generated query path), queries are checked against `LARGE_TABLE_ROW_THRESHOLD = 1_000_000` rows. If a referenced table exceeds this threshold **and** the query has no `WHERE` clause, execution is blocked with an informative error message.

The auto-LIMIT injected by the validator caps the *result set*, but a missing WHERE clause still forces a full sequential scan on the underlying table, which can be extremely expensive.

Applies to: LLM-generated queries only (schema is available at that point).

---

## 6. Schema Privacy Filtering (`chat_service.py`, `datasources/models.py`)

Privacy settings are configured per connection and applied before schema data reaches the LLM prompt or query results.

### Schema-level filtering (before LLM)
- **Excluded schemas/tables**: stripped from the schema entirely — the LLM never sees them
- **Excluded columns**: stripped from table definitions — the LLM cannot reference them
- **Sensitive columns** (pattern-matched against names like `email`, `ssn`, `password`, `token`, `credit_card`, etc.): kept in the schema but annotated as `[SENSITIVE]`; sample values are omitted
- **Row counts and column comments**: individually toggled via `include_row_counts` / `include_column_comments` settings

### Result-level masking (after execution)
`_mask_sensitive_result_columns()` runs on every query result before it is returned to the client or persisted to `results_json`:
- Columns matching any **sensitive name pattern** have their values replaced with `[REDACTED]`
- Columns listed in **excluded_columns** (matched by bare column name) have their values replaced with `[REDACTED]`, preventing `SELECT *` from surfacing explicitly excluded columns even if the LLM generates such a query

Applied at all five result-return paths: auto-execute, error-correction retry, execute-pending, edit-and-execute, and re-sort.

### Row filter injection
Connections can define a `row_filter_sql` clause (e.g., `tenant_id = 42`) that is automatically injected as a subquery wrapper around every executed query, enforcing row-level isolation regardless of what SQL the LLM generates.

---

## 7. Column-Schema Validation (`chat_service.py`)

After generation, the query's column references are checked against the live schema. Any column that does not exist in the schema (or is excluded by privacy settings) causes the query to be rejected. If this check fails on a fresh LLM response, a self-correction LLM call is attempted before surfacing the error to the user.

---

## 8. Execution Mode Gate (`chat_service.py`)

The connection's `execution_mode` setting controls whether a validated query is actually run:

| Mode | Behaviour |
|---|---|
| `auto_execute` | Query runs immediately after validation |
| `review_first` | Query is held in `pending_approval` state; a human must confirm before execution |
| `generate_only` | Query is returned to the user for inspection; execution is never triggered |

---

## 9. Execution Limits

| Control | Default | Config key |
|---|---|---|
| Max result rows | 1,000 | `DEFAULT_ROW_LIMIT` |
| Query timeout | 30 s | `DEFAULT_QUERY_TIMEOUT` |
| Statement-level timeout (PostgreSQL) | same as above | set via `statement_timeout` |
| Statement-level timeout (MySQL) | same as above | set via `MAX_EXECUTION_TIME` |

Client requests cannot raise the row limit above the server-configured maximum.

---

## 10. Audit Logging

Every executed query is written to the audit log with the user ID, org ID, connection ID, table names referenced, execution time, and byte-scan count. This provides an after-the-fact record for access review without blocking the request path.
