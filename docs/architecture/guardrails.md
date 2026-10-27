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
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` — only when `BEHIND_TLS_PROXY=true` (or the request reached uvicorn itself over TLS); the container speaks plain HTTP, and the header is never derived from `X-Forwarded-Proto`, so a plain-HTTP install can't be HSTS-pinned by a client-supplied header — see [HTTPS Behind a Reverse Proxy](../getting-started/02_configuration.md#https-behind-a-reverse-proxy), which also governs the refresh cookie's `Secure` flag
- `Content-Security-Policy: default-src 'none'` on `/api/` routes; a self-only policy on frontend assets, with no third-party hosts (fonts are self-hosted through `@fontsource`, so the browser makes no request outside the deployment). `frontend/nginx.conf` sends the same policy for the static frontend
- `Cache-Control: no-store` on all `/api/v1/auth` responses

---

## 2. Rate Limiting (SlowAPI)

Per-endpoint limits enforced by SlowAPI. Every limit is keyed on **client IP**, with one exception: `POST /api/v1/chat` is keyed on the authenticated user ID taken from the Bearer token, falling back to IP if the token is missing or expired. Connection ID is not part of any rate-limit key.

| Endpoint | Limit | Keyed on |
|---|---|---|
| `POST /api/v1/chat` | 20 / minute | User ID (IP fallback) |
| `POST /api/v1/connections/test` | 5 / minute | IP |
| `POST /api/v1/chat/sort/{message_id}` | 30 / minute | IP |
| `GET /api/v1/export/messages/{id}/csv` and `/xlsx` | 30 / minute | IP |
| `POST /api/v1/export/report` | 10 / minute | IP |
| `GET /api/v1/public/share/*` | 60 / minute | IP |
| Auth endpoints (login, register, refresh, password reset, `PUT /auth/me`) | `AUTH_RATE_LIMIT`, default 10 / minute | IP |

Because the default key is the client IP, **which IP that resolves to depends on `TRUSTED_PROXIES`** — see [Rate Limiting and Proxy Trust](../getting-started/02_configuration.md#rate-limiting-and-proxy-trust). Behind a proxy that is not in a trusted range, every caller shares one bucket.

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

Every generated query passes through a two-layer validator before execution, then runs inside a database-enforced read-only transaction (Layer 3). This applies to LLM-generated queries, user-edited queries, and sorted re-executions.

### Layer 1 — BaseSQLValidator (shared across all SQL dialects)
- **Statement count**: rejects any input containing more than one SQL statement (`; \w` pattern)
- **Statement type**: accepts only `SELECT` and `WITH` (CTEs); rejects `INSERT`, `UPDATE`, `DELETE`, and all DDL
- **Blocked keywords**: whole-word scan for `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `REVOKE`, `MERGE`, `REPLACE`, `CALL`, `EXECUTE`, `EXEC`
- **Writing keywords**: `INTO` is rejected wherever sqlparse tokenises it as a keyword (`SELECT … INTO` creates a table on PostgreSQL; MySQL's `INTO @var` writes a variable). The same word inside a comment, string literal or quoted identifier is allowed.
- **Dangerous patterns**: `INTO OUTFILE`, `INTO DUMPFILE`, multi-statement semicolons
- **Auto-LIMIT injection**: appends `LIMIT {default_limit}` if no LIMIT clause is present, capping result rows

### Layer 2 — Dialect-specific validators
Additional checks layered on top of the base:

| Dialect | Blocked functions / patterns |
|---|---|
| **PostgreSQL** | `pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_export`, `lo_import`, `dblink`, `dblink_exec`; `COPY … TO`, `SET ROLE`, `SET SESSION` |
| **MySQL** | `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `GET_LOCK`, `RELEASE_LOCK`, `MASTER_POS_WAIT`; `LOAD DATA`, `SET GLOBAL`, `SET SESSION`, `FLUSH` |

### Layer 3 — Database-enforced read-only transaction (`datasources/adapters/`)
The text validator is not the last line. Every statement runs inside a read-only transaction the adapter opens itself: asyncpg `transaction(readonly=True)` on PostgreSQL, and on MySQL/MariaDB `SET SESSION TRANSACTION READ ONLY` before each statement, restored to `READ WRITE` afterwards so the pooled connection is clean. A write the keyword scan cannot see, such as `nextval()`, `setval()` or a function with side effects, is refused by the engine (SQLSTATE `25006` on PostgreSQL, errno 1792 on MySQL). A dedicated read-only database role remains recommended as a further layer.

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
- **Column values** (sample values, MySQL ENUM/SET member lists, entity-resolution hints): sent only when `PrivacySettings.column_values_allowed()` is true, that is `include_sample_values` is on (default off; absent settings are not consent) and the column is not sensitive. ENUM/SET member lists are withheld at introspection and again on every read of a cached schema. Semantic `value_mappings` are not gated by this rule: they are curated semantic metadata, sent on every chat turn unless the semantic model marks the column `is_sensitive` or the privacy settings hide it
- **Row counts and column comments**: individually toggled via `include_row_counts` / `include_column_comments` settings

### Result-level masking (after execution)
`_mask_sensitive_result_columns()` (`services/result_masking.py`) runs on every query result before it is returned to the client or persisted to `results_json`, and the streaming path masks every `row_batch` SSE event and the final payload with the same plan (`_plan_result_mask`, built once per executed statement and again after each self-correction):
- The query is classified (`_classify_select_columns`): a sensitive or excluded column, an alias over one (`email AS contact`) and an expression over one (`lower(email) AS contact`) are redacted as `[REDACTED]`. Unaliased expressions are masked by position, whatever name the driver gives them
- Only statistical aggregates are exempt (`COUNT`, `SUM`, `AVG`, `STDDEV`/`VARIANCE`, `CORR`/`COVAR`/`REGR`, `PERCENTILE_CONT`, `BOOL_AND`/`BOOL_OR`, `EVERY`), and ranking window functions (`RANK`, `ROW_NUMBER`, …). An aggregate with `OVER` is not exempt. Value-returning aggregates (`MIN`, `MAX`, `STRING_AGG`, `ARRAY_AGG`, `GROUP_CONCAT`, `LISTAGG`, `PERCENTILE_DISC`, `MEDIAN`, `BIT_AND`/`BIT_OR`, JSON aggregates) are masked
- The classifier is scope-aware: a sensitive column renamed inside a CTE, a derived table (column alias lists included) or a nested or scalar subquery stays masked under its new name, and a set-operation position is masked when any branch masks it
- Name matching (a sensitive pattern, or a bare name from **excluded_columns**) applies as well, to every output except an exempt one: `COUNT(email) AS email` is a statistic and is not masked for its name. A `SELECT *` or `alias.*` over a real table relies on name matching
- When the classifier cannot parse a shape, the outermost SELECT list is still classified. When parsing fails outright and the SQL names a sensitive or excluded column, every column is masked, and so is every column when a name the classifier expected is missing from the result
- Accepted limits: `SUM`/`AVG` grouped by a unique key, or restricted by `FILTER (WHERE …)` to one row, reveal single values; JSON path keys (`data->>'email'`) are not recognised as columns

Applied at all five result-return paths: streaming auto-execute, non-streaming auto-execute, execute-pending, edit-and-execute, and re-sort. Stored results are masked again with the connection's current settings on every read (`services/stored_results.py`): session history, share links, and CSV/XLSX/PDF exports.

### Row filter injection
Connections can define a `row_filter_sql` clause (e.g., `tenant_id = 42`) that is automatically injected as a subquery wrapper around every executed query, enforcing row-level isolation regardless of what SQL the LLM generates. The wrap is `SELECT * FROM (\n<query>\n) AS _sift_q WHERE <filter>`, with the trailing row limit moved outside it: `LIMIT n`, `LIMIT n OFFSET m`, PostgreSQL `OFFSET m LIMIT n`, MySQL `LIMIT m, n` (`services/row_filter.py:_split_trailing_limit`).

The filter wraps every statement sent to the database, including self-corrected retries, the zero-result re-run, approved, edited and re-sorted queries and cache hits (wrapped exactly once). The SQL stored with the message, kept as a verified example, shown in the UI and sent to the LLM in correction prompts is the model's clean query; SQL stored by older versions in the wrapped form is unwrapped (`unwrap_row_filter`) before it is shown, sent to the LLM or run again.

The filter is applied to the query's output, so the query must return every column the filter names. One that does not (`COUNT(*)`, most aggregates, a MySQL join whose output has duplicate column names) fails at the database rather than running unfiltered: fail-closed by design. A query the wrap cannot safely enclose is rejected before execution (`services/row_filter_scan.py`): unbalanced parentheses, a backslash in a string literal, a MySQL executable comment `/*! … */`, an unterminated string or comment, a PostgreSQL dollar-quote tag holding a non-ASCII character (PostgreSQL takes every byte >= 0x80 as a tag character), or a MySQL `$…$` pair with only identifier characters between the signs (a later MySQL may lex it as a dollar-quoted string).

`row_filter_sql` is set in `privacy_settings` through `POST /api/v1/connections`, `PUT /api/v1/connections/{id}/privacy` and the Privacy Settings form; `""` clears it. Every write validates it against the connection's dialect (`services/row_filter.py:validate_row_filter`): no `;`, no comment start, no `$`, no leading `WHERE`, balanced parentheses per the engine's lexing (failing closed on a backslash or an unterminated quote), at most 1000 characters. Textual and fail-closed; a wrong column name surfaces at the database.

### Correction prompts never carry database error text
When a query fails at execution, the correction prompt receives Savvina's own description (`services/error_feedback.py`): an error category derived from the engine's documented code (`datasources/errors.py`, `BaseDataSource.classify_error`), a fixed hint keyed by that category (`services/correction.py`) and, for the categories whose messages quote names rather than values (unknown name, ambiguous name, grouping, type mismatch, permission, syntax), the names the error refers to that the LLM already has: names it wrote in its SQL or, for an unknown or ambiguous name, names in the schema it was shown. Every other category lists no names. The engine's message, which can quote stored values, is never sent to the LLM.

The error the user sees (`redact_error_for_user`) keeps the engine's message for the name-quoting categories above. For every other category it keeps only the first line, with every quoted literal (and a PostgreSQL key detail `=(…)`, and the unquoted name after `operator does not exist:`) replaced by `[REDACTED]`. The raw text goes to the server log only.

PostgreSQL reports a stored value it failed to look up or parse under the same SQLSTATEs as a mistake in the query (`ssn::regclass` → 42P01 `relation "123456789" does not exist`; `to_tsquery(col)` → 42601). `PostgreSQLDataSource._classify_one` therefore classifies a name-quoting SQLSTATE as DATA when the error has no position in the query (permission errors excepted: most have none), or when it carries an error context or an internal query (`::regtype` parses the stored text and points into it). The measured table is in a comment next to `_may_quote_a_value`.

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
