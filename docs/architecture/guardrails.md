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
| **PostgreSQL** | `pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_export`, `lo_import`, `dblink`, `dblink_exec`, `set_config`; `COPY … TO`, `SET ROLE`, `SET SESSION` |
| **MySQL** | `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `GET_LOCK`, `RELEASE_LOCK`, `MASTER_POS_WAIT`, `SOURCE_POS_WAIT`; user-variable assignment `:=`; `LOAD DATA`, `SET GLOBAL`, `SET SESSION`, `SET STATEMENT`, `SET_VAR(`, `FLUSH`; a statement that starts with `SET` |

`set_config` and `:=` are refused because a value stored in a setting or a user variable can be read back in the same statement under another column (`current_setting('x')`, `@v`), which result masking cannot follow. The `:=` check is on the raw text, so the two characters inside a string literal are refused too. MariaDB's `SET STATEMENT … FOR …` and MySQL's `SET_VAR` optimizer hint set a variable for one statement: either could switch `sql_mode` (`ANSI_QUOTES`, `NO_BACKSLASH_ESCAPES`) away from the mode the adapter read at connect, or lift the statement time limit, so both are refused, also on the raw text, with a comment (`/* … */`, `-- …`, `# …`) accepted as the gap between words and the opener of an executable comment (`/*!`, `/*M!`, with its version) removed first, since the server runs its content. A statement whose first word after comments is `SET` is refused as well, checked on the same text (so a `SET` inside `/*! … */` or after a `#` comment counts) and on sqlparse's first token: the base type check lets `SET sql_mode = (SELECT …)` through because the text mentions `SELECT`, and a session `SET` would change the mode for every later query on the pooled connection.

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
- **Sensitive columns** (pattern-matched against names like `email`, `ssn`, `password`, `token`, `credit_card`, etc., or listed by exact name in `sensitive_columns`): kept in the schema but annotated as `[SENSITIVE]`; sample values are omitted, and so is the column's comment (it can spell out the values), in `_apply_privacy_to_schema` and again when `format_schema_for_llm` renders
- **Column values** (sample values, MySQL ENUM/SET member lists, entity-resolution hints): sent only when `PrivacySettings.column_values_allowed()` is true, that is `include_sample_values` is on (default off; absent settings are not consent) and the column is not sensitive. ENUM/SET member lists are withheld at introspection and again on every read of a cached schema. Semantic `value_mappings` are not gated by this rule: they are curated semantic metadata, sent on every chat turn unless the semantic model marks the column `is_sensitive` or the privacy settings hide it
- **Row counts and column comments**: individually toggled via `include_row_counts` / `include_column_comments` settings

### Result-level masking (after execution)
`_mask_sensitive_result_columns()` (`services/result_masking.py`) runs on every query result before it is returned to the client or persisted to `results_json`, and the streaming path masks every `row_batch` SSE event and the final payload with the same plan (`_plan_result_mask`, built once per executed statement and again after each self-correction):
- The query is classified (`_classify_select_columns`): a sensitive or excluded column, an alias over one (`email AS contact`) and an expression over one (`lower(email) AS contact`) are redacted as `[REDACTED]`. Unaliased expressions are masked by position, whatever name the driver gives them
- Only statistical aggregates are exempt (`COUNT`, `SUM`, `AVG`, `STDDEV`/`VARIANCE`, `CORR`/`COVAR`/`REGR`, `PERCENTILE_CONT`, `BOOL_AND`/`BOOL_OR`, `EVERY`), and ranking window functions (`RANK`, `ROW_NUMBER`, …). An aggregate with `OVER` is not exempt. Value-returning aggregates (`MIN`, `MAX`, `STRING_AGG`, `ARRAY_AGG`, `GROUP_CONCAT`, `LISTAGG`, `PERCENTILE_DISC`, `MEDIAN`, `BIT_AND`/`BIT_OR`, JSON aggregates) are masked
- The classifier is scope-aware: a sensitive column renamed inside a CTE, a derived table (column alias lists included) or a nested or scalar subquery stays masked under its new name, and a set-operation position is masked when any branch masks it
- Name matching (a sensitive pattern, an exact name from **sensitive_columns**, or a bare name from **excluded_columns**) applies as well, to every output except an exempt one: `COUNT(email) AS email` is a statistic and is not masked for its name. A `SELECT *` or `alias.*` over a real table relies on name matching
- When the classifier cannot parse a shape, the outermost SELECT list is still classified. When parsing fails outright and the SQL names a sensitive or excluded column, every column is masked. When the outer SELECT list is fully classified, each result column is judged on its own position (the lineage, as engine-side dynamic data masking has it); the predicted output names are not consulted, since the row-filter rewrite returns a function column under sqlglot's name (`strpos` → `position`) and PostgreSQL truncates long aliases. Without positions, a predicted masked name the driver did not return masks every column. Before classification the SQL is rebuilt from the connection dialect's own tokens (sqlglot), so every string, quoted identifier and comment ends where the engine ends it; a MySQL executable comment (`/*! ... */`), an unreadable statement or an unknown dialect masks every column. Statistical aggregates are exempt as a whole call, alone or wrapped in a cast or `ROUND(..., n)`.
- Masking changes what is displayed, not what is read: predicates, joins and ordering on a sensitive column run on the real values, so free-form queries can narrow a masked value down (the inference limit every dynamic-data-masking implementation documents). The opt-in strict sensitive-column mode (below) refuses most such queries before they run; it is not a complete answer (see the user guide). Row filters and excluded columns, not masking, are the controls against a user who is trying to extract a value.
- Accepted limits: `SUM`/`AVG` grouped by a unique key, or restricted by `FILTER (WHERE …)` to one row, reveal single values; JSON path keys (`data->>'email'`) are not recognised as columns

Applied at all five result-return paths: streaming auto-execute, non-streaming auto-execute, execute-pending, edit-and-execute, and re-sort. Stored results are masked again with the connection's current settings on every read (`services/stored_results.py`): session history, share links, and CSV/XLSX/PDF exports. Every result also carries `row_filter_fingerprint` (HMAC-SHA256 under the encryption key over a domain label, `savvina.row_filter_fingerprint.v1` (NUL-terminated), followed by the canonical row-filter map; 16 hex characters, `""` for none; `services/row_filter.py:row_filter_fingerprint`), stamped on every stored result (auto-execute on both paths, approve and edit; a re-sort is returned, not stored, and carries none); `stored_results._remask` serves a result whose fingerprint differs from the connection's current map with no rows and `stale_row_filter: true` ("same map or nothing": a result without the field counts as computed with no filters; a result whose stored fingerprint is neither a string nor null is stale and served with the field cleared; while the connection is in the row-filter migration state every stored result is stale), and the share and export routers answer 410 for a single such result; a PDF report section carries the notice in place of the table and embeds no chart.

### Strict sensitive columns (`services/sensitive_guard.py`)
With `PrivacySettings.strict_sensitive_columns` on, `check_strict_sensitive` rejects a query that uses a sensitive column (a pattern, an entry of `sensitive_columns`, or the bare name of an excluded column) anywhere but as a plain item of its SELECT list: in a `WHERE`, `JOIN`, `GROUP BY`, `HAVING` or `ORDER BY`, inside an expression, under another name, by position, or through a whole-row reference; also a subquery used as a value whose SELECT list carries a sensitive column or a star, a set operation whose later branch puts a sensitive column under the first branch's output name (that name is then sensitive everywhere), a star in a later branch, and a position at or past a star; also any set operation but `UNION ALL` (a `UNION` that removes duplicates, any `INTERSECT` or `EXCEPT`, wherever it stands) whose branch carries a sensitive column, a laundered alias or a star, since keeping or dropping rows reveals whether values match. A construct the check cannot follow (a natural join, `USING`, a star outside a projection item, a set-operation operand that is not a SELECT such as `TABLE users`, a VALUES list in a set operation, an alias of a scalar subquery that projects a star used outside the SELECT list, an unaliased scalar subquery item that reads a sensitive column below the outermost query, which MySQL names after its SQL text) and a query sqlglot cannot read as one plain statement are rejected as well, as is a query nested too deeply for the parser or the check (a `RecursionError`). It runs in `row_filter._executable` on the clean query, after the migration check and before the row-filter rewrite, so it covers every executed statement. It raises `SensitiveColumnRejectedError`, a `QueryRejectedError` like `RowFilterRejectedError`: the text is ours and is shown verbatim, the failure is classified `REJECTED` (this application's own refusal: the correction prompt carries the rejection text and its `llm_hint` instead of an engine description). The mode is off by default and is not a guarantee: it makes narrowing a masked value down much harder, but the columns that must never be learned belong in **excluded_columns**. See the module docstring and the [user guide](../user-guide/04_privacy-controls.md#strict-sensitive-columns) for the full list of refused shapes.

### Row filter rewriting
Connections can define per-table row filters (`privacy_settings.row_filters`, `{"schema.table": "<condition>"}`). Every executed statement is rewritten by `services/row_filter_rewrite.py:apply_row_filters`: the statement is parsed with sqlglot, every `Table` node that matches a key becomes `(SELECT * FROM <table> WHERE (<condition>) OFFSET 0) AS <alias or table name>` (`LIMIT 18446744073709551615` on MySQL, in `_filtered`), and the tree is regenerated. The fence per dialect is an explicit map (`_FENCE`); a dialect without an entry is refused, never fenced by guess. The filter applies to what the query reads, so aggregates, joins and CTEs are covered; tables not listed are unfiltered. The fence stops both engines from merging the derived table or pushing the outer query's predicates into it, so the condition is evaluated first: without it a predicate the model wrote runs on hidden rows and a data-dependent error (verified: `1/0` on PostgreSQL, a bad regular expression or JSON path on MySQL) leaks one bit per query into the correction prompt. Cost: the model's predicates cannot use the base table's indexes unless the condition does; on MySQL every filtered reference is materialized in full per query (an outer LIMIT does not reduce the work), and on PostgreSQL a correlated subquery over a filtered table rescans the filtered rows per outer row. MariaDB unverified; the timing channel stays open.

Matching is exact after dialect normalisation (PostgreSQL folds unquoted identifiers to lowercase, MySQL keeps them). A reference that matches a key only case-insensitively, or a bare name present in two filtered schemas, is rejected rather than run unfiltered. Also rejected before sending (`RowFilterRejectedError`): a statement sqlglot cannot parse (a `ParseError` or a `TokenError`), more than one statement, a CTE named like a filtered table (inside a non-recursive CTE body the bare name is the base table, and the rewriter does not track that scope), a join nested inside a filtered table reference, a construct the generator cannot write back (`unsupported_level=RAISE`), an unknown dialect, a malformed key, and shapes that read rows without a table reference (`_reject_unfilterable`: a sqlglot `Describe`/`Command` node, the `*_to_xml*`/`ts_stat`/`ts_rewrite`/`crosstab*`/`connectby`/`dblink*` functions, the value-statistics views `pg_stats*`/`pg_statistic*` under `pg_catalog` and MySQL `information_schema.column_statistics`). The output is generated without comments or optimizer hints, and the PostgreSQL adapter pins `standard_conforming_strings = on` in every read-only transaction so the engine reads string literals as sqlglot does. Residual risk: a function outside the list that runs SQL from a string; native row-level security (Phase 3) is the complete answer. The rejection text is ours and is shown verbatim, and the correction prompt carries it too (`REJECTED`, see below), with an `llm_hint` appended when the model can act on it (ambiguity, casing, a CTE named like a filtered table, a parse failure, more than one statement, a nested join, an uninspectable or unwritable construct, a listed function or statistics view, excessive nesting); a refusal caused by the settings (migration, a malformed key or condition, an unsupported dialect or one with no fence) has none. On a MySQL server whose `sql_mode` has `ANSI_QUOTES`, a query that contains a double quote is refused before it runs when strict mode or any row filter is on (`row_filter._executable`): the checks and the rewrite parse the query with `"…"` as a string, which the server reads as a name. On one whose `sql_mode` has `NO_BACKSLASH_ESCAPES`, a query that contains a backslash is refused whatever the settings, first thing in `_executable` (before the `privacy is None` return): result masking, the strict check and the rewriter read `\'` as an escaped quote where the server ends the string, and masking parses every executed statement. The MySQL adapter's `connect` reads both modes from `@@sql_mode` (decoded when the driver returns bytes) into the instance flags `double_quote_is_string_literal` and `backslash_is_escape`; `_executable` takes both as required keywords, read fail-safe off the adapter (`result_masking._double_quote_is_literal`, `_backslash_is_escape`: anything but a literal `True` reads `False`) after `connect` on every path, so a fresh adapter's class defaults are never what the check sees. The rewritten statement is generated with `''` for a quote inside a string, so the rewrite adds no `\'`. It does read a backslash in a string as an escape, so one before an ordinary letter is dropped (`'dom\user'` is written `'domuser'`), and it writes a tab, a carriage return or a line break inside a string as a backslash escape (`\t`, `\r`, `\n`), which under `NO_BACKSLASH_ESCAPES` the server reads as written; the user guide advises keeping all of these out of row-filter conditions.

When strict sensitive-column mode is on, its check runs first, on the clean query, before this rewrite. The rewrite applies to every statement sent to the database, including self-corrected retries, the zero-result re-run, approved, edited and re-sorted queries and cache hits. The SQL stored with the message, kept as a verified example, shown in the UI and sent to the LLM is the model's clean query; SQL stored by older versions under the whole-query wrap is unwrapped (`unwrap_row_filter`) before it is shown, sent to the LLM or run again.

A connection that still holds the legacy `row_filter_sql` and no `row_filters` is in the migration state (`PrivacySettings.row_filter_needs_migration`): `chat_service._stage_load_connection` refuses the turn before any LLM call, `_executable` refuses every other execution, and `PUT /privacy` refuses an empty map unless `clear_legacy_row_filter` is sent.

`row_filters` is validated on every write (`services/row_filter.py:validate_row_filters`): key shape (`schema.table`, bare or dialect-quoted parts), the textual condition rules (no `;`, no comment start, no `$`, no leading `WHERE`, at most 1000 characters) and a sqlglot parse of the condition as a single expression. Unquoted key parts are folded as the engine folds them (PostgreSQL lowercases; `row_filter._key_part`).

Save-time verification: the cached schema is not consulted (it is written with privacy applied, so a filter on an excluded table would be refused); instead the connections router plans every new or changed pair on the live database. The statement is the one the runtime would send for `SELECT * FROM "s"."t"` (`_check_statement` → `apply_row_filters` → `BaseDataSource.explain`): `EXPLAIN SELECT * FROM (SELECT * FROM "s"."t" WHERE (cond) OFFSET 0) AS "t"` on PostgreSQL, with the MySQL fence on MySQL, so what is planned is the condition as sqlglot writes it back inside the fenced derived table. A pair the rewriter refuses is 422 naming the key, and so is a pair the rewriter does not match (the statement comes back unchanged), which is refused rather than saved unchecked. On MySQL the plan runs with `sql_notes` off, so the note that quotes the rewritten statement, condition literals included, is never raised and never reaches the logs. The save is refused on an engine error (422 with the text redacted by category, as `redact_error_for_user` does for execution errors), on an unreachable database, on an engine timeout, and when the plan does not return within the query timeout plus five seconds (`asyncio.wait_for`; 422, nothing stored). A disconnect that fails afterwards is logged and does not change the verdict. Unchanged pairs are not re-planned, so a column dropped or renamed after the save is not re-checked. `EXPLAIN` returns no row; while planning PostgreSQL may evaluate constant immutable and stable expressions (`1/0` fails), MySQL may evaluate constant subqueries and stored functions declared `DETERMINISTIC` and read one row by unique key; volatile functions are not evaluated. On MySQL a type mismatch (`id = 'abc'`) is only a warning and passes. Engine-side bounds: PostgreSQL runs the plan under `statement_timeout` through the extended protocol (one statement); MariaDB sets `max_statement_time`; MySQL applies no timeout to an `EXPLAIN`, which is why the router bounds the wait itself; when that bound cancels a plan the MySQL adapter skips the session resets and drops the connection, so the cancellation is reported as the timeout.

Prompt hints: a filtered table loses its data-derived sample values and row count in `_apply_privacy_to_schema` (`_withhold_row_filtered_hints`; `ColumnInfo.sample_source`: `"stats"` or absent withheld, `"type"` kept), so it has no `(≈N rows)` header, no entity-resolution hint and no large-table full-scan rejection (section 5). The key is compared case-insensitively with the cached table, and in the row-filter migration state every table loses them.

### Correction prompts never carry database error text
When a query fails at execution, the correction prompt receives Savvina's own description (`services/error_feedback.py`): an error category derived from the engine's documented code (`datasources/errors.py`, `BaseDataSource.classify_error`), a fixed hint keyed by that category (`services/correction.py`) and, for the categories whose messages quote names rather than values (unknown name, ambiguous name, grouping, type mismatch, permission, syntax), the names the error refers to that the LLM already has: names it wrote in its SQL or, for an unknown or ambiguous name, names in the schema it was shown. Every other category lists no names. The engine's message, which can quote stored values, is never sent to the LLM. The exception is Savvina's own refusals (`REJECTED`: a row filter that cannot be applied, the strict sensitive-column rule, the ANSI_QUOTES double-quote check, the NO_BACKSLASH_ESCAPES backslash check): their own text, which names only columns and tables from the query or the settings and never a value, and their `llm_hint` are sent, with no fixed category hint. For a refusal the prompt says the query was refused before it ran and labels the description `REFUSAL:` instead of `EXECUTION ERROR:`; the `llm_hint` carries only the advice, since the text is already there. A refusal without an `llm_hint` (a row filter that needs migration, a malformed key, a dialect with no fence: nothing the model can change) ends the correction loop at once in both the streaming and the non-streaming path, with no LLM call, and the user sees the refusal text. `REJECTED` is reserved for these refusals: an adapter's `classify_error` answer of `REJECTED` is read as `OTHER` (`execution._classify`).

The error the user sees (`redact_error_for_user`) keeps the engine's message for the name-quoting categories above. For every other category it keeps only the first line (for a pymysql `(errno, 'message')` line, the first line of the unwrapped message; text that does not unwrap stays as written), with every quoted literal (and a PostgreSQL key detail `=(…)`, and the unquoted name after `operator does not exist:`) replaced by `[REDACTED]`. The raw text is not logged; the server log holds the same redacted text. The MySQL adapter's cursors skip aiomysql's `SHOW WARNINGS` read, which would pass each warning to `warnings.warn` and so to stderr: a MySQL warning quotes stored values (1292 `Truncated incorrect DOUBLE value: '…'`, one per row).

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
