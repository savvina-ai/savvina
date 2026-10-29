# API Reference

The Savvina AI backend exposes a REST API at `http://localhost:8000`. The interactive Swagger UI is available at `http://localhost:8000/docs`.

All endpoints are prefixed with `/api/v1`. Request and response bodies use JSON.

---

## Health

### `GET /health`

Returns the application health status.

**Response `200`:**
```json
{
  "status": "ok",
  "app": "Savvina AI"
}
```

---

## Authentication

Authentication endpoints are under `/api/v1/auth`. All other endpoints require a valid Bearer access token in the `Authorization` header.

### `GET /api/v1/auth/setup-status`

Returns whether this is a fresh deployment (no users yet). Public — no token required. **Rate limited:** 10 req/min.

**Response `200`:** `{"needs_setup": true}`

---

### `POST /api/v1/auth/register`

**First-boot only.** Creates the initial admin account and organisation on a fresh deployment. Returns `403` if any users already exist. **Rate limited:** 10 req/min.

**Request body:**
```json
{
  "email": "admin@example.com",
  "password": "MyP@ssw0rd!",
  "display_name": "Alice",
  "org_name": "Acme Corp"
}
```

`org_name` and `display_name` are optional. The created user always receives `role: "admin"`.

**Response `201`:** `LoginResponse` (access token + refresh token + user)

**Response `403`:** `{"detail": "Setup already complete. Please log in."}`

---

### `POST /api/v1/auth/login`

Authenticate with email + password. **Rate limited:** 10 req/min.

**Request body:** `{"email": "user@example.com", "password": "MyP@ssw0rd!"}`

**Response `200`:** `LoginResponse`

---

### `POST /api/v1/auth/refresh`

Rotate refresh token — issues a new pair, revokes the old one. Uses `SELECT FOR UPDATE` to prevent concurrent refresh races. **Rate limited:** 10 req/min.

**Request body:** `{"refresh_token": "<token>"}`

**Response `200`:** `TokenPairResponse` | **`401`:** Invalid/expired | **`409`:** Already rotated

---

### `POST /api/v1/auth/logout`

Revoke a specific refresh token. Requires valid access token.

**Request body:** `{"refresh_token": "<token>"}` | **Response `204`**

---

### `POST /api/v1/auth/logout-all`

Revoke all active refresh tokens for the current user (all devices). **Response `204`**

---

### `POST /api/v1/auth/reset-password`

Set a new password for the authenticated user without requiring the current password. Requires a valid Bearer token. All active refresh tokens are revoked on success. **Rate limited:** 10 req/min.

**Request body:** `{"password": "NewP@ssw0rd!"}` | **Response `204`** | **`401`:** Missing or invalid token | **`400`:** Password too weak

---

### `GET /api/v1/auth/me`

Return the authenticated user's own profile.

**Response `200`:** `UserResponse`

---

### `PUT /api/v1/auth/me`

Update the current user's display name and/or password. Rate limited by the `AUTH_RATE_LIMIT` setting.

Changing a password requires `current_password` as well as `new_password`; `display_name` can be changed on its own. Unlike `reset-password`, this proves possession of the existing password.

**Request body (all fields optional):**
```json
{
  "display_name": "Ada Lovelace",
  "current_password": "OldP@ssw0rd!",
  "new_password": "NewP@ssw0rd!"
}
```

**Response `200`:** `UserResponse`
**Response `400`:** Password too weak, or `current_password` incorrect

---

### `GET /api/v1/auth/sessions`

List the caller's active login sessions — refresh tokens that are neither expired nor revoked. This is how a user sees where they are signed in.

**Query parameters:** `limit` (max 200, default 50), `offset` (default 0).

**Response `200`:** `PaginatedResponse[SessionResponse]`

---

### `DELETE /api/v1/auth/sessions/{session_id}`

Revoke one session — signing out a single device without disturbing the others. Use `POST /api/v1/auth/logout-all` to revoke every session at once.

**Response `204`:** No content

---

### `DELETE /api/v1/auth/me`

Delete the caller's own account.

**This anonymises rather than hard-deletes.** The email is replaced with a random `deleted_<random>@anonymized.local` address, the password hash is overwritten with a throwaway value, and every refresh token is revoked. The whole sequence runs inside a savepoint, so a failure part-way through rolls back rather than leaving a half-anonymised account.

**Response `204`:** No content

---

## Data Sources

### `GET /api/v1/datasources`

Returns all registered data source adapter types with their configuration schemas. The frontend uses this to render the dynamic connection form.

**Response `200`:**
```json
[
  {
    "type": "postgresql",
    "display_name": "PostgreSQL",
    "icon": "🐘",
    "query_dialect": "PostgreSQL",
    "config_schema": {
      "fields": [
        {"name": "host", "type": "string", "label": "Host", "required": true},
        {"name": "port", "type": "integer", "label": "Port", "default": 5432},
        {"name": "database", "type": "string", "label": "Database", "required": true},
        {"name": "username", "type": "string", "label": "Username", "required": true},
        {"name": "password", "type": "password", "label": "Password", "required": true},
        {"name": "ssl_mode", "type": "select", "label": "SSL Mode",
         "options": ["disable","allow","prefer","require","verify-ca","verify-full"]}
      ]
    }
  }
]
```

---

## Connections

### `POST /api/v1/connections`

Create and save a new data source connection.

**Request body:**
```json
{
  "name": "My Production DB",
  "source_type": "postgresql",
  "config": {
    "host": "db.example.com",
    "port": 5432,
    "database": "myapp",
    "username": "readonly",
    "password": "secret",
    "ssl_mode": "require"
  },
  "privacy_settings": {
    "include_sample_values": true,
    "include_column_comments": true,
    "include_row_counts": true,
    "sensitive_column_patterns": ["email", "ssn", "password"],
    "excluded_schemas": [],
    "excluded_tables": [],
    "excluded_columns": [],
    "row_filters": {"public.orders": "tenant_id = 42"}
  },
  "execution_mode": "auto_execute"
}
```

**Execution mode values:** `auto_execute`, `review_first`, `generate_only`

`row_filters` is optional, `{"schema.table": "<condition>"}`; `row_filter_sql` is no longer accepted and is dropped. Each entry is validated as described under [Row Filters](../user-guide/04_privacy-controls.md#row-filters); a 422 names the key and the reason. Every pair is then planned on the database with `EXPLAIN` before the connection is created; the statement planned is the one queries will run (`SELECT * FROM "schema"."table"` rewritten by the row-filter rewriter); a pair the rewriter refuses (a construct it cannot write back, or a statistics catalog view such as `pg_catalog.pg_stats`) is 422 `row_filters["schema.table"]: <reason>`; an engine error (a missing table or column) is 422 `row_filters["schema.table"]: rejected by the database: <text>`, an unreachable database, an engine timeout, or a plan that does not return within the query timeout plus 5 seconds 422 `row filters could not be verified against the database: <reason>`, and the connection is not created. Unquoted key parts are stored folded as the engine folds them (PostgreSQL lowercase).

**Response `201`:** `ConnectionResponse`

**Response `422`:** `Row filter …` (the filter is invalid; the detail gives the reason), `row_filters["…"]: rejected by the database: …` or `row filters could not be verified against the database: …`

---

### `GET /api/v1/connections`

List all saved connections, most recent first.

**Response `200`:** `ConnectionResponse[]`

---

### `GET /api/v1/connections/{id}`

Get a single connection with full detail including schema cache and privacy settings.

**Response `200`:** `ConnectionDetail`
**Response `404`:** Connection not found

---

### `DELETE /api/v1/connections/{id}`

Delete a connection and cascade-delete all associated sessions, messages, cache entries, examples, and semantic model (the semantic rows are deleted; their audit history is kept).

**Response `204`:** No content
**Response `404`:** Connection not found

---

### `POST /api/v1/connections/test`

Test a new connection **before saving it**. Does not create any database records.

**Request body:**
```json
{
  "source_type": "postgresql",
  "config": {
    "host": "db.example.com",
    "port": 5432,
    "database": "myapp",
    "username": "readonly",
    "password": "secret"
  }
}
```

**Response `200`:**
```json
{"success": true, "message": "Connected successfully", "server_version": "PostgreSQL 16.1"}
```

**Response `400`:** Connection failed (error detail in `detail` field)

---

### `POST /api/v1/connections/{id}/test`

Test an existing saved connection using its stored (encrypted) credentials.

**Response `200`:** Same as `POST /api/v1/connections/test`

---

### `GET /api/v1/connections/{id}/schema`

Return the cached schema for a connection. Returns `404` if no schema has been cached yet (run a refresh first).

**Response `200`:** DataSourceSchema as dict

---

### `POST /api/v1/connections/{id}/schema/refresh`

Re-introspect the data source, update the schema cache, and **invalidate the query cache** for this connection.

**Response `200`:** Updated DataSourceSchema as dict
**Response `400`:** Introspection failed

---

### `PUT /api/v1/connections/{id}/privacy`

Update privacy settings for a connection. Partial updates are supported — omit fields to leave them unchanged.

**Request body (all fields optional):**
```json
{
  "include_sample_values": false,
  "include_column_comments": true,
  "include_row_counts": true,
  "sensitive_column_patterns": ["email", "phone"],
  "excluded_schemas": ["audit"],
  "excluded_tables": ["sessions"],
  "excluded_columns": ["public.users.password_hash"],
  "row_filters": {"public.orders": "tenant_id = 42"}
}
```

`row_filters`: an object sets the map after validation, `{}` clears it, and an absent field leaves it unchanged. On a connection that still stores the legacy `row_filter_sql`, `{}` is refused (422) unless `clear_legacy_row_filter: true` is sent as well; that flag alone also removes the legacy filter. `row_filter_sql` in the body is ignored. Changing privacy settings invalidates the connection's caches; a change that only touches `row_filters` keeps the cached schema.

New or changed pairs are planned on the database with `EXPLAIN`, as the rewriter writes them (same 422s as on `POST`, and nothing is stored); unchanged pairs are not re-checked and a body without `row_filters` opens no database connection. Unquoted key parts are stored folded as the engine folds them (PostgreSQL lowercase).

The connection response carries `row_filter_needs_migration: true` while a legacy `row_filter_sql` is stored and no `row_filters` map has been saved; every query on such a connection is refused until one of the two actions above.

**Response `200`:** `ConnectionDetail`

**Response `422`:** `Row filter …` (the filter is invalid; the detail gives the reason), `row_filters["…"]: rejected by the database: …` or `row filters could not be verified against the database: …`

---

### `PUT /api/v1/connections/{id}/execution-mode`

Change the execution mode for a connection.

**Request body:**
```json
{"execution_mode": "review_first"}
```

**Response `200`:** `ConnectionDetail`

---

### `GET /api/v1/connections/{id}/config`

Return the stored connection config so the UI can pre-fill its edit form.

**Credential fields are masked**, never returned in clear text: any key matching `password`, `passwd`, `pass`, `secret`, `key`, `api_key` or `token` comes back as the literal string `**redacted**`.

**Response `200`:** `ConnectionConfigResponse` — `{"name": ..., "source_type": ..., "config": {...}}`
**Response `404`:** Connection not found

---

### `PUT /api/v1/connections/{id}/config`

Update a connection's name and/or config.

Send `**redacted**` back for any credential field you do not want to change and the stored value is kept — so the UI can round-trip the masked response from `GET` without making the user retype secrets. Sending a real value replaces it.

Changing the config clears that connection's cached schema and its query cache, since both may no longer describe the database being pointed at.

**Response `200`:** `ConnectionResponse`
**Response `404`:** Connection not found

---

## Semantic Model

### `GET /api/v1/connections/{id}/semantic`

Get the saved semantic model for a connection, assembled from the semantic store tables. Tables and columns are returned in code-point order of their keys (Python string order, the same on every database), not the order the generator produced them in.

**Response `200`:** `SemanticModel`
**Response `404`:** No semantic model saved

---

### `POST /api/v1/connections/{id}/semantic/generate/init`

Phase 1 of semantic model generation. Loads the schema (live introspection if it is not cached), takes the connection row lock, marks the run as started (`generation_status = "tables_partial"`, a fresh run id, the schema hash) and returns the work list. Makes no LLM call. Rate limited to 10/minute. Never destructive: no table or column row is touched; a run reconciles onto whatever rows already exist.

**Query parameters:**
- `provider` (string, default: `claude`) — provider type name or config UUID
- `resume` (bool, default: `false`) — continue an interrupted run instead of starting a new one. Requires a `tables_partial` model whose schema hash still matches; keeps the stored run id so the tables that run already generated stay done

**Response `200`:** `GenerateInitResponse`
```json
{
  "connection_id": "…",
  "tables_total": 12,
  "batch_count": 3,
  "batch_size": 4,
  "pending_table_keys": ["public.customers", "public.orders", "…"],
  "resumed": false
}
```
`pending_table_keys` is the exact list of tables still to send to `/generate/batch`: every table on a fresh run, only the tables the interrupted run had not reached on a resume. The client splits it into batches of `batch_size`; `batch_count` is how many that makes (at least 1).

On a `tables_partial` model that has no schema hash or run id (an interrupted run migrated from the blob era by migration 0003) there is no run to continue: `resume=true` runs a fresh, non-destructive run instead (every table pending, hand edits kept) and answers `resumed: false`.

**Response `400`:** Schema load failed, or `resume=true` and there is no interrupted (`tables_partial`) generation to resume
**Response `429`:** The per-IP limit was hit — retry with backoff
**Response `409`:** `resume=true` and the schema changed since the interrupted run — regenerate with a fresh init (`resume=false`, keeps hand edits), or delete the model first (`DELETE …/semantic`, as the UI's Restart from scratch does) to discard them; or the connection was deleted during init

---

### `POST /api/v1/connections/{id}/semantic/generate/batch`

Phase 2 — call once per batch. Sends one batch of tables to the LLM, reconciles the result onto the stored rows and returns the run's progress. Fields a person has edited by hand are skipped. Rate limited to 20/minute. The generator keeps the output grounded in the batch's real tables: a near-miss table or column key (different case, quoting) is mapped to the real one and an invented one is dropped; a column the LLM leaves out is not touched, so its earlier annotation survives. A batch the LLM answered for purges the connection's cached query answers and table embeddings, so chat does not serve SQL written against the previous annotations; the schema cache, which the run reads for every batch, is kept.

**Query parameter:** `provider` (string, default: `claude`)

**Request body:**
```json
{"table_keys": ["public.customers", "public.orders"]}
```
`table_keys` is 1 to 4 `schema.table` keys, taken from `pending_table_keys`. Batches are identified by their keys, never by position.

**Response `200`:** `GenerateBatchResponse` — progress only, not the model
```json
{
  "connection_id": "…",
  "table_keys": ["public.customers", "public.orders"],
  "tables_done": 6,
  "tables_total": 12,
  "batch_size": 4,
  "omitted_table_keys": []
}
```
`omitted_table_keys` are the tables of this batch the LLM returned nothing for. They are not marked done and stay pending; the web client sends them once more, as their own batch. `/generate/globals` names any table still without an annotation in the model's `generation_warnings`.

**Response `400`:** `/generate/init` not called first (or the interrupted run has no run id — call `/generate/init` again), a key that is not a table of the schema, or an over-long table key or column name
**Response `409`:** The schema changed since init — regenerate with a fresh `/generate/init` (`resume=false`, keeps hand edits); or the state moved on while the batch was running (generation finalised or the model deleted); or a newer `/generate/init` restarted the run — the batch's results are discarded
**Response `422`:** The LLM call failed or its response could not be used (including an exhausted provider balance, `[QUOTA_EXCEEDED]`, which waiting does not fix), or the request body failed validation (`table_keys` empty or more than 4 entries)
**Response `429`:** The provider throttled the call (the detail carries `[RATE_LIMIT]`), or the per-IP limit was hit — retry with backoff
**Response `500`:** An unexpected error
**Response `503`:** The provider was unavailable (a 5xx or a dropped connection; the detail carries `[PROVIDER_UNAVAILABLE]`): retry with backoff
**Response `504`:** The batch timed out (180 s)

---

### `POST /api/v1/connections/{id}/semantic/generate/globals`

Phase 3 — generate cross-table business metrics, common joins, derived columns and segments, and finalise the model (`generation_status = "complete"`). Rate limited to 10/minute. Call after all batches are complete, or on its own to finish an interrupted run over the tables already generated.

Tables and columns that are no longer in the database are removed here, together with relationships and other references to them. Before that, a column key stored under a near-miss spelling (quoted, or in another letter case) of a live column the model's baseline has seen is renamed to the live name, keeping its annotation, hand edits, references and relationships, unless this run annotated the column under its real name: then the key is a duplicate and is removed. What the connection's privacy settings hide is kept, not removed, and never reaches the LLM. Hand-edited metrics, segments, derived columns, joins and relationships are carried through; over-long generated names are dropped.

**Query parameter:** `provider` (string, default: `claude`)

**Response `200`:** `SemanticModel` (final, as stored — the same model a later `GET` returns)
**Response `400`:** Init or batch phases not completed, or a value the store cannot hold
**Response `409`:** The model was deleted or its state changed while globals was generating, or a newer `/generate/init` restarted the run — the globals results are discarded and the new run is left to finish
**Response `422`:** The LLM call failed or its response could not be used
**Response `429`:** The provider throttled the call, or the per-IP limit was hit — retry with backoff
**Response `500`:** An unexpected error
**Response `503`:** The provider was unavailable (a 5xx or a dropped connection; the detail carries `[PROVIDER_UNAVAILABLE]`): retry with backoff
**Response `504`:** Timed out (180 s)

An LLM failure fails this call like a batch (`422`, `429` on a throttle, or `503` on a provider outage): the model stays `tables_partial`, and the web client offers **Finish with what's generated** and **Resume**.

A completed model can carry `generation_warnings` from this call: the generator's own warnings (for example a time-series pattern in a metric over a fiscal-period table); one note per hand edit that named a column the table no longer has (a primary timestamp or date column naming it, or a list left empty, is cleared and no longer counts as a hand edit; a list with live entries keeps them and stays a hand edit); and one warning naming every table of the schema that has no annotation (the LLM returned nothing for it, the run was finished before reaching it, or it is new since the run started).

---

### `PUT /api/v1/connections/{id}/semantic`

Partially update the semantic model. Only the fields you provide are updated — unmentioned tables/metrics/joins are preserved. A top-level section that is `null` or omitted is not supplied and is left untouched; an empty list (`[]`) clears it. Inside a table, `null` also means "not supplied" for `display_name` and `default_filters`, and inside a column for `display_name`, `value_mappings`, `is_sensitive`, `is_non_additive` and `semantic_type`; for the other nullable fields (`description`, `grain`, `currency`, …) `null` clears the value.

Every field you change is recorded as hand-edited, so a later regenerate refreshes the rest of the model but leaves it alone. A value identical to what is stored is not recorded as an edit. Changes are written to the model's audit history.

**Request body (all fields optional):**
```json
{
  "tables": {
    "public.customers": {
      "display_name": "Customers",
      "description": "Master customer records",
      "default_filters": ["status != 'deleted'"],
      "columns": {
        "status": {
          "display_name": "Account Status",
          "description": "Current status of the customer account",
          "value_mappings": [
            {"raw_value": "A", "display_value": "Active"},
            {"raw_value": "D", "display_value": "Deleted"}
          ],
          "is_sensitive": false
        }
      }
    }
  },
  "business_metrics": [
    {
      "name": "Monthly Revenue",
      "definition": "SUM(orders.total_amount)",
      "description": "Total completed order value",
      "filters": ["orders.status = 'completed'"],
      "related_tables": ["orders"]
    }
  ],
  "common_joins": [
    {
      "description": "Customer orders",
      "tables": ["customers", "orders"],
      "join_pattern": "customers.id = orders.customer_id"
    }
  ],
  "is_user_reviewed": true
}
```

**Response `200`:** `SemanticModel`
**Response `400`:** A value the store cannot hold (for example an over-long table key, column name or metric name), or a table or column the model does not have, unless the patch supplies its `display_name`. Nothing is written for a rejected patch.
**Response `409`:** A concurrent edit changed a table row (optimistic lock) — reload and retry

---

### `DELETE /api/v1/connections/{id}/semantic`

Remove the semantic model from a connection: every semantic row is deleted, hand edits included. Future queries will not have business context.

**Response `204`:** No content

---

### `GET /api/v1/connections/{id}/semantic/drift`

Report schema drift since the semantic model was generated: tables and columns the model references that no longer exist, tables added since generation, columns added to a table the model holds, column keys stored under a near-miss spelling that Apply Drift renames to the live column (below), tables in the model that still exist but the connection's role cannot read (PostgreSQL only, and only when the role can read `pg_catalog.pg_class`: on MySQL, or when `pg_class` is not readable, such a table looks the same as a dropped one and is reported, and applied, as removed), and business metrics that name a missing table. Type or nullability changes are not reported.

A column counts as added only when it is in neither the stored table nor the model's baseline of the live columns it has seen (kept per table, written by each generation batch, the globals finalisation and every apply that writes), so a column the LLM left out at generation, or one deleted from the model through `PUT .../semantic`, is not reported. A live column the baseline has seen that the model holds only under a near-miss key (quoted, or differing only in letter case) is reported as a rename: the apply renames the key to the live name, keeping its annotation, hand edits and references, with no LLM call. If the baseline never saw the column, the database renamed it: the column is reported as added and the old key as removed. A table the model holds with no baseline yet (a model from before the baseline existed) is baselined at the first live check outside a generation run. A column added before that first check is not reported; a Regenerate annotates it. A live check also completes an existing entry that lacks the real name of a stored column the privacy settings hide (or a column annotated by hand), so showing that column later renames its stored key instead of reporting the column as added. The baselining is a short write under the connection row lock, taken after the refreshed schema cache is committed, and it is why the check can answer `409` or `503` below.

Each call introspects the source database live (catalog views and `pg_stats` only, no statistical queries), stores the result as the caller's schema cache, and diffs the stored model against it, so no separate schema refresh is needed. Table embeddings and cached query answers are flushed only when the caller had no cached schema or its structure (table and column names, types, primary keys) differed from the live one (every model write except an apply or a generation batch clears all users' schema caches, so the first check after such a write usually flushes). If the source cannot be reached, the check falls back to the caller's cached schema and says so in the response. Rate limited to 5 requests per minute.

**Response `200`:** `DriftReport` — `{"connection_id": ..., "warnings": [...], "warning_count": N, "checked_at": "...", "schema_source": "live" | "cache", "schema_cached_at": "..." | null, "refresh_error": "..." | null, "has_actionable_changes": true | false}`. `schema_source` is `"cache"` only on the fallback, with `schema_cached_at` and `refresh_error` saying which cache was used and why. `has_actionable_changes` is true when a table was added or removed, or a column added, removed or renamed — what `POST .../semantic/apply-drift` acts on; an unreadable table or a metric naming a missing table is reported but not actionable.
**Response `400`:** The source could not be reached and there is no cached schema to fall back to
**Response `404`:** No semantic model — generate one first
**Response `409`:** The connection was deleted while the check was completing the baseline
**Response `503`:** The baseline write waited on the connection row lock past `DB_LOCK_TIMEOUT_SECONDS` (another save or apply on the connection), with `Retry-After: 5`

---

### `POST /api/v1/connections/{id}/semantic/apply-drift`

Apply the schema changes `GET .../semantic/drift` reports to the stored model, without re-annotating the tables that did not change. It runs the same `compute_drift` as the check, on its own live introspection, so it acts on every change the check calls actionable. A stored column key whose spelling no longer matches the live column (for example a quoted `"status"` from a migrated model, or `Status`, beside a live `status`) is either renamed or removed (see **Renamed columns** below). The check reports it as a rename; as a duplicate key when the model also stores `status`; or, when the baseline never saw `status` (the database renamed the column), as a near-miss key of a column the model has not seen, which the apply removes, with `status` reported as a new column. Rate limited to 5/minute — a preview and its apply are two calls.

- **Removed tables** are dropped, with every metric (`related_tables`), join (`tables`) and derived column (`base_tables`) that names them by its stored key (`schema.table`), every segment that applies only to them, and every relationship touching them. A segment that also applies to a surviving table stays and just loses the removed tables from its `applicable_tables`; a segment with no `applicable_tables` applies everywhere and is kept.
- **Removed columns** are dropped from their table, with the references to them (`primary_timestamp_column`, `primary_date_column`, `partition_columns`, `cluster_columns`, hierarchy levels) — hand-edited ones included, each trim noted in `summary.warnings` — and the relationships that use them. Free SQL (`default_filters`, metric, join, derived-column and segment expressions) is not rewritten.
- **Renamed columns**: a near-miss key (quoted, or in another letter case) of a live column the model's baseline has seen and the table does not store under its real name is renamed to that name. It keeps its annotation, its hand edits (their marks move with it), the table's references to it (`primary_timestamp_column`, `primary_date_column`, `partition_columns`, `cluster_columns`, hierarchy levels) and the relationships on it; free SQL is not rewritten. A rename makes no LLM call and needs no provider. When two keys spell the same column, the first in sort order is renamed and the other is removed as a duplicate. A near-miss key of a column the model also stores under its real name is a duplicate and is removed; one of a column the baseline never saw is removed, and the column is new.
- **New tables** are annotated by the LLM in batches, at most 12 per apply (three batches of four, so one request and its cross-table pass stay inside the 900 s proxy timeout). Past the cap the tables are taken in sorted key order, except that those the most recent logged apply reported in `tables_not_annotated` go last, so tables the LLM keeps skipping cannot starve the others. A `summary.warnings` entry names the rest (up to five) and says to run Check Drift and apply again, and the next apply takes them. If the LLM annotated at least one table, one cross-table pass follows, and only the metrics, joins, derived columns and segments it writes that name a newly annotated table (a metric's `related_tables`, a join's `tables`, a derived column's `base_tables`, a segment's `applicable_tables`; a segment with no tables is not added) are added *alongside* the existing ones. Items over unchanged tables alone are not added, so an item you deleted is not re-added. An existing item keeps its payload against a same-named new one. An item removed with its table takes its hand-edit mark with it: a same-named item the LLM writes is stored as a fresh, unmarked item. `is_user_reviewed` is reset, and `generated_at` and `generation_model` updated, only when new tables or new columns were annotated.
- **New columns** on a table the model holds (the columns the check reports as added) are annotated by the LLM and added to their table. Only the new columns are asked for and taken (the table's DDL and description are sent as context); existing columns are never rewritten, table-level fields (primary timestamp, hierarchies, partition columns) are left alone, and no cross-table pass runs for them. They share the three batches with new tables, which go first. A new-column batch holds at most 4 tables, 25 columns and as much DDL as one prompt carries (20,000 characters); a table with more than 25 new columns gets the first 25 by name. What does not fit is not sent: a `summary.warnings` entry names it, and the next apply takes it. The tables the most recent logged apply reported in `column_tables_failed` go last. A column the LLM leaves out of a table it answered for is recorded as seen and named in a warning (a Regenerate annotates it); a batch that failed, or a table it returned nothing for, leaves its columns new for the next apply. A failure here does not fail the apply: the removals, the renames, the new tables and the batches answered before the failure are applied, the batches after it are not tried, and a warning names the columns left out. The exception is an apply whose only changes are new columns, when no batch has answered yet: a provider throttle (`429`) or outage (`503`) is returned as is, so a client can retry.
- Relationships, time expressions, notes and the schema hash are recomputed. Foreign-key relationships come from the catalog, so a relationship to a new table appears when the apply annotates that table (not for one in `tables_not_annotated`); hand-edited relationships are carried through. A catalog relationship the model does not have is added only on a newly annotated table or on a new column of a held table; one you deleted on unchanged or renamed columns is not brought back. A new column's relationship arrives with the apply that records the column as seen (annotated, or left out of a table the LLM answered for), not with one whose batch failed or returned nothing for its table, or that did not send the column. It arrives once: if you delete it after that apply, it stays deleted.

Removing and renaming are structural and make no LLM call. Hand edits on surviving tables are kept. A table that another generation run or apply stored while this apply's LLM calls ran is left as stored: it is neither overwritten nor treated as new. The schema is always introspected live — acting on a cached schema could delete what still exists — so an unreachable source is an error rather than a fallback. A table or column the privacy policy hides, and (on PostgreSQL, when the role can read `pg_catalog.pg_class`) a table the connection's role cannot read, are never treated as removed, and a relationship ending on one of them is kept; otherwise — on MySQL, or when `pg_class` is not readable — such a table looks the same as a dropped one and is reported, and applied, as removed. A column the role cannot read (a column-level grant) looks the same as a dropped one on both PostgreSQL and MySQL, because the adapters read columns from `information_schema.columns`, which lists only columns the role holds a privilege on; it is reported, and applied, as removed. A metric that names a removed table by a bare name (`orders` rather than `public.orders`) is kept and reported by the drift check instead. Each call that reaches the source stores the refreshed schema as the caller's schema cache (committed before any LLM call), as the check does. A real apply refreshes the caller's schema cache like a preview does and keeps it; only the query and embedding caches are purged.

The baseline of seen columns is written in the apply's save transaction (or, when the apply saves nothing, with its log entry, or alone when only the baseline changed):

- A new table the apply annotated gets its live columns.
- A held table keeps the seen columns that are still live (a table with no entry yet starts from its live columns), and gains the live columns it stores and the new columns a batch answered for. A renamed column was seen already and stays seen.
- A column that left the database leaves the baseline, so it is new again if it comes back. So does one the privacy policy hides, unless the model stores it: a stored hidden column stays seen under its real name, so once shown again it is not new, and a quoted key of it is renamed (a case-only key is not hidden, so it is removed). A hidden or unreadable table keeps its entry.
- The entries of tables the model no longer holds are deleted.

**Query parameters:** `provider` — provider name or id used to annotate new tables and new columns (default `claude`). `dry_run` — `true` previews: the same assembly runs on the current model with no LLM call and the semantic model is not written; the response's `semantic_model` is the stored model as it is. Like the check, a preview does refresh the caller's schema cache and, when the caller had none or its structure (table and column names, types, primary keys) differed from the live schema, flushes the connection's table embeddings and cached query answers. `summary.dry_run` is `true`, and the new tables the apply would send to the LLM (at most 12, in the order above) are listed, sorted, in `tables_added`, the new columns it would ask for in `columns_added`, and the keys it would rename in `columns_renamed`. `relationships_added` lists the catalog relationships on those new columns between tables the model holds; relationships to new tables and the LLM's items over them are only known on the real apply and are not listed. `summary.warnings` holds the notes on hand edits the apply would trim and the warnings naming the new tables and columns it would leave for a later apply. A preview is never logged.

**Response `200`:** `ApplyDriftResponse` — `{"semantic_model": SemanticModel, "summary": DriftApplySummary}`. `summary` is a by-name diff of the model before and after, so cascades are included. Every list is sorted: `tables_added`, `tables_removed`, `metrics_added`, `metrics_removed`, `joins_added`, `joins_removed` (by `join_pattern`), `derived_columns_added`, `derived_columns_removed`, `segments_added`, `segments_removed`, `relationships_added`, `relationships_removed` (labelled `from_table.from_column → to_table.to_column`). `columns_removed` maps each surviving table to its removed column names (a removed table's columns go with it), `columns_added` each surviving table to the column names the apply added to it (a new table is listed in `tables_added` instead), and `columns_renamed` each surviving table to its renamed keys, each mapped to the live name it now has (a renamed column is in neither of the other two; a relationship on it is listed under its new name, as neither removed nor added). A metric, join, derived column or segment taken out because it named a removed table is listed under `*_removed` even when the LLM writes one of the same name, which is then also listed under `*_added`. `tables_not_annotated` lists only the tables sent to the LLM in this apply that it returned nothing for: they are still missing from the model, and the next drift check reports them again. New tables past the 12-table cap were not sent and are not listed there; the deferred-tables warning in `warnings` names them. `column_tables_failed` lists the tables whose new columns this apply did not get: their batch failed or returned nothing for them, or the previous apply listed them and this one did not reach them. They still lack those columns, the next drift check reports them again, and the next apply sends them last. `warnings` are, in order: the time-column check over the metrics the apply added (a metric using an audit timestamp on a fiscal-period table); the notes on trimmed hand edits; the warnings naming the new tables and columns left for a later apply; and the warnings naming the new columns that were not annotated. A preview carries only the notes and the warnings naming what would be left for a later apply. `changed` is computed: true when any structural list (every list except `warnings`, `tables_not_annotated` and `column_tables_failed`) is non-empty, so a preview of non-empty drift is always `changed` and a summary with only skipped tables or warnings is not; `dry_run` marks a preview. With nothing to apply, the model is returned unchanged with `changed: false` and nothing is logged.
**Response `400`:** The source could not be reached, the provider cannot be resolved (checked only on a real apply, not a preview, and only when there are new tables to annotate or new columns are the only change; beside removed tables, removed columns or renamed columns, an unresolvable provider leaves the new columns unannotated, with a warning, and the removals and renames are applied), or a generated value the store cannot hold
**Response `404`:** Connection not found, or no semantic model — generate one first
**Response `409`:** A generation run is in progress or was interrupted (finish or restart it first), or one started, or the model or connection was deleted, while drift was being applied
**Response `422`:** The LLM pass for new tables failed, or the provider's quota is exhausted during it (a failed new-column batch is a warning instead)
**Response `429`:** The provider throttled the LLM pass for new tables, or a new-column batch of an apply whose only changes are new columns before any batch answered (retry later), or the per-IP rate limit was hit
**Response `500`:** The LLM pass for new tables failed unexpectedly
**Response `503`:** The provider was unavailable (a 5xx or a dropped connection; the detail carries `[PROVIDER_UNAVAILABLE]`) for the LLM pass for new tables, or for a new-column batch of an apply whose only changes are new columns before any batch answered: the model is unchanged; the route does not retry on its own, so apply again
**Response `504`:** The LLM pass for new tables timed out

On a `422`, `500`, `503` or `504`, or a provider-throttle `429`, the model is unchanged (the refreshed schema cache is already committed).

---

### `GET /api/v1/connections/{id}/semantic/drift/history`

The connection's logged Apply Drifts, newest first. Every real apply that writes (not a preview, and not a call with no drift to act on) appends its summary to the semantic audit log (`semantic_model_history`) in the same transaction as the save, so an apply is in the log exactly when it happened. An apply that changed nothing but skipped new tables the LLM returned nothing for, or listed tables in `column_tables_failed`, saves nothing and is logged on its own, because the next apply reads that entry to retry those tables last. The log is kept when the model is deleted. An entry whose stored summary can no longer be read is skipped and logged, and the next older entry takes its place, so the response holds `limit` entries whenever the log has that many.

**Query parameters:** `limit` (1–50, default 10).

**Response `200`:** `[{"applied_at": "...", "applied_by": "<user id>" | null, "summary": DriftApplySummary}]`
**Response `404`:** Connection not found

---

### `GET /api/v1/connections/{id}/semantic/suggestions`

List pending semantic-model suggestions raised from user feedback on generated queries — the improvement loop that lets the model be corrected without regenerating it wholesale.

**Query parameters:** `limit` (max 200, default 50), `offset` (default 0).

**Response `200`:** `PaginatedResponse[SemanticSuggestionResponse]`

---

### `POST /api/v1/connections/{id}/semantic/suggestions/{suggestion_id}/apply`

Apply one suggestion to the stored semantic model and return the updated model. The change is recorded as a hand edit, so a later regenerate leaves it alone.

**Response `200`:** `SemanticModel`
**Response `404`:** Connection or suggestion not found, no semantic model, or the suggestion's table or column is not in the model
**Response `409`:** The suggestion has already been applied (`Suggestion already applied`), or a concurrent edit changed the row

---

## Chat

### `POST /api/v1/chat`

Run the full NL-to-SQL pipeline: cache lookup → LLM → validate → execute (depending on execution mode). Returns a **streaming SSE response** (`text/event-stream`) — the final event carries the complete `ChatResponse` payload.

**Request body:**
```json
{
  "connection_id": "uuid",
  "session_id": null,
  "message": "How many customers signed up this month?",
  "provider": "uuid-of-provider-config",
  "options": {
    "show_query": true,
    "max_rows": 100,
    "explain_results": false
  }
}
```

**Response `200`:** `text/event-stream` — final `done` event contains `ChatResponse`

```json
{
  "session_id": "uuid",
  "message_id": "uuid",
  "query": "SELECT COUNT(*) FROM customers WHERE ...",
  "query_dialect": "PostgreSQL",
  "explanation": "This query counts customers who signed up this month...",
  "results": {
    "columns": ["count"],
    "column_types": ["int8"],
    "rows": [[1248]],
    "row_count": 1,
    "truncated": false,
    "execution_time_ms": 12.4,
    "bytes_scanned": null,
    "row_filter_fingerprint": "",
    "stale_row_filter": false
  },
  "execution_time_ms": 12.4,
  "status": "executed",
  "cache_hit": false,
  "error": null
}
```

**Status values:** `executed`, `cached`, `pending_approval`, `query_only`, `error`

`results.row_filter_fingerprint` (string or null) identifies the row filters the rows were computed under: `""` for none, `null` on a re-sort response; on results stored by earlier versions it is absent (history, session share) or `null` (single-result share JSON). `results.stale_row_filter` (boolean) is `true` only on a stored result served after the connection's row filters changed: the rows are empty (`row_count: 0`) because the result was computed under a different row filter.

---

### `POST /api/v1/chat/execute/{message_id}`

Execute a query that is in `pending_approval` status (Review First mode).

**Response `200`:** Updated `ChatResponse` with `status: executed` and results

---

### `POST /api/v1/chat/edit/{message_id}`

Execute a user-edited version of a pending query. The edited query goes through the same safety validation.

**Request body:**
```json
{"edited_query": "SELECT COUNT(*) FROM customers WHERE created_at >= NOW() - INTERVAL '30 days'"}
```

**Response `200`:** Updated `ChatResponse` with edited query and results

---

### `POST /api/v1/chat/feedback/{message_id}`

Submit feedback on a generated query.

**Request body:**
```json
{"feedback": "thumbs_up"}
```

**Feedback values:** `thumbs_up`, `thumbs_down`

- `thumbs_up` → adds the question + SQL pair to the verified example library for future few-shot prompting
- `thumbs_down` → evicts this question and semantically similar entries from the query cache

**Response `204`:** No content

---

### `DELETE /api/v1/chat/feedback/{message_id}`

Retract previously submitted feedback on a message.

- Retracting `thumbs_up` also removes the verified example that was added to the example library.
- Retracting `thumbs_down` clears the feedback field only — already-evicted cache entries are not restored.

**Response `204`:** No content

---

### `GET /api/v1/chat/sessions`

List all chat sessions, most recent first. Each session includes a `cache_hit_count` showing how many messages in that session were served from cache.

**Response `200`:** `SessionResponse[]`

---

### `GET /api/v1/chat/sessions/{id}`

Get a single session.

**Response `200`:** `SessionResponse`
**Response `404`:** Session not found

---

### `GET /api/v1/chat/sessions/{id}/history`

Get all messages in a session, ordered chronologically.

**Response `200`:** `MessageResponse[]`. A message whose result was computed under a different row filter than the connection has now comes back with empty `results_json.rows` and `results_json.stale_row_filter: true`.

---

### `DELETE /api/v1/chat/sessions/{id}`

Delete a session and all its messages.

**Response `204`:** No content

---

### `GET /api/v1/chat/cache/stats`

Get global query cache statistics across all connections.

**Response `200`:**
```json
{
  "total_entries": 142,
  "hit_count": 892,
  "miss_count": 0,
  "hit_rate": 0.0,
  "top_cached_queries": [
    {"question": "how many customers do we have?", "hit_count": 45},
    {"question": "show top 10 orders", "hit_count": 32}
  ]
}
```

Note: `miss_count` and `hit_rate` are not tracked at the database level; only `total_entries` and `hit_count` are reliable.

---

### `DELETE /api/v1/chat/cache/{connection_id}`

Clear all cached query entries for a specific connection.

**Response `204`:** No content
**Response `404`:** Connection not found

---

### `GET /api/v1/chat/examples/{connection_id}`

List all verified example pairs for a connection.

**Response `200`:**
```json
{
  "examples": [
    {
      "id": "uuid",
      "question": "How many customers do we have?",
      "query": "SELECT COUNT(*) FROM customers",
      "query_dialect": "PostgreSQL",
      "created_at": "2025-02-15T10:30:00Z"
    }
  ],
  "total": 1
}
```

---

### `POST /api/v1/chat/examples/{connection_id}`

Manually add a verified question → query pair to the example library.

**Request body:**
```json
{
  "question": "Show active customers",
  "query": "SELECT * FROM customers WHERE status = 'active' LIMIT 20"
}
```

**Response `201`:** `ExampleResponse`

---

### `PUT /api/v1/chat/examples/{example_id}`

Update the question and/or query of an existing verified example. The example's embedding is recomputed, so an edited question is matched correctly on the next lookup.

**Request body:**
```json
{
  "question": "Show customers active in the last 30 days",
  "query": "SELECT * FROM customers WHERE status = 'active' AND last_seen > now() - interval '30 days'"
}
```

**Response `200`:** `ExampleResponse`
**Response `404`:** Example not found

---

### `DELETE /api/v1/chat/examples/{example_id}`

Delete a verified example by its ID.

**Response `204`:** No content

---

### `POST /api/v1/chat/sort/{message_id}`

Re-execute the message's original query with an `ORDER BY` injected, so sorting happens in the source database rather than on the truncated result set the client already holds. Rate limited to 30/minute.

**Request body:** `SortRequest` — the column to sort on and the direction.

**Response `200`:** `QueryResultsResponse`

---

### `GET /api/v1/chat/cache/{connection_id}/entries`

List cached query entries for a connection, newest first.

**Query parameters:** `limit` (1–100, default 20), `offset` (default 0), `search` (optional substring match).

**Response `200`:** `PaginatedResponse[CacheEntryResponse]`

---

### `DELETE /api/v1/chat/cache/entries/{entry_id}`

Delete a single cache entry. Use this to evict one stale answer without flushing the whole cache for the connection.

**Response `204`:** No content

---

## Sharing

Sharing is two halves: an authenticated endpoint that mints a token, and the unauthenticated `/public` endpoints below that redeem it. **The token is the only credential** — anyone holding the URL can read the shared results without logging in, so treat a share link as public.

### `POST /api/v1/chat/messages/{message_id}/share`

Generate — or return the existing — share token for one executed message. Calling it twice returns the same token rather than rotating it.

Only messages with status `executed` or `cached` can be shared; anything else is a `400`.

**Request body (optional):**
```json
{
  "expires_in_days": 30
}
```

`expires_in_days` is clamped to 1–365. Omit the body entirely for a link that never expires.

**Response `200`:** `ShareResponse` — `{"share_token": "...", "share_expires_at": "..."}`
**Response `400`:** Only executed results can be shared
**Response `404`:** Message not found

---

### `POST /api/v1/chat/sessions/{session_id}/share`

Same as above, but shares an entire conversation thread. Any message in the session becomes readable, so check the whole thread before sharing it.

**Response `200`:** `ShareResponse`
**Response `404`:** Session not found

---

## Public (unauthenticated)

These endpoints take **no `Authorization` header**. They are the redemption side of the share tokens above, rate limited to 60/minute per IP, and they are the only routes in the API that serve data to an anonymous caller.

Every one of them returns `410 Gone` once `share_expires_at` has passed, and `404` for an unknown token — the two are distinguished deliberately, so a recipient can tell "this link expired" from "this link was never valid".

Every single-result endpoint also returns `410 Gone` with `These results were computed under a different row filter. Re-run the question.` when the connection's row filters changed after the result was stored; the session share serves such a message with empty rows and `stale_row_filter: true`. The two 410s are told apart by `detail`: `Share link has expired` for an expired link, the text above for a stale result. A stale result stays 410 until the connection's row filters return to the map it was computed under; to share it again, re-run the question and share the new message. A stored result whose fingerprint field is neither a string nor null is served as stale (the session share) or 410 (single-result endpoints), not as an error; while the connection is in the row-filter migration state every stored result is stale.

### `GET /api/v1/public/share/{token}`

Return the results behind a shared message link. Rows are truncated to the `DEFAULT_ROW_LIMIT` setting.

**Response `200`:** `PublicShareResult` — the result set plus the generated SQL
**Response `404`:** Shared result not found
**Response `410`:** Share link has expired, or result computed under a different row filter (see [Public](#public-unauthenticated))

---

### `GET /api/v1/public/share/{token}/csv`

Download the shared message's results as CSV. Not row-limited — this serves the full stored result set.

**Response `200`:** `text/csv` attachment, `shared-results.csv`
**Response `410`:** Share link has expired, or result computed under a different row filter (see [Public](#public-unauthenticated))

---

### `GET /api/v1/public/share/{token}/xlsx`

Download the shared message's results as XLSX.

**Response `200`:** `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` attachment, `shared-results.xlsx`
**Response `410`:** Share link has expired, or result computed under a different row filter (see [Public](#public-unauthenticated))

---

### `GET /api/v1/public/share/session/{token}`

Return a shared conversation. Capped at the first 200 messages, and each message's result set is truncated to 10 rows — this is a preview of the thread, not a bulk export.

**Response `200`:** `PublicSessionResult` — session title plus `PublicMessageSummary[]`
**Response `404`:** Shared session not found
**Response `410`:** Share link has expired

---

## Export

All export endpoints require authentication and only ever reach messages belonging to the caller's own sessions.

### `GET /api/v1/export/messages/{message_id}/csv`

Download one message's query results as CSV. Rate limited to 30/minute.

**Response `200`:** `text/csv` attachment, `query-<first 8 chars of id>.csv`
**Response `400`:** Message has no query results
**Response `404`:** Message not found
**Response `410`:** Result computed under a different row filter (see [Public](#public-unauthenticated))

---

### `GET /api/v1/export/messages/{message_id}/xlsx`

Same, as a formatted XLSX workbook. Rate limited to 30/minute.

**Response `200`:** XLSX attachment, `query-<first 8 chars of id>.xlsx`
**Response `410`:** Result computed under a different row filter (see [Public](#public-unauthenticated))

---

### `POST /api/v1/export/report`

Generate a PDF combining several query results into one document. Rate limited to 10/minute.

`chart_image` is a base64 data-URL PNG captured client-side from the rendered chart — the backend does not render charts itself. `heading` defaults to the first 80 characters of the message content when omitted.

**Request body:**
```json
{
  "title": "Q3 Revenue Review",
  "sections": [
    {
      "message_id": "5f2c...",
      "heading": "Revenue by region",
      "chart_image": "data:image/png;base64,iVBORw0KGgo...",
      "chart_title": "Q3 revenue, by region"
    }
  ]
}
```

`title` is 1–255 characters; `sections` holds 1–50 entries. A section whose result is stale (computed under a different row filter) carries the notice in place of the table and embeds no chart, even when `chart_image` is sent.

**Response `200`:** `application/pdf` attachment, named from a sanitised `title`
**Response `404`:** A referenced message was not found

---

## Providers

### `GET /api/v1/providers`

List all saved provider configs, plus every registered provider type that has no config yet. Each entry shows status, model, and configuration — but never the decrypted API key.

**Query parameters:** `limit` (max 200, default 50), `offset` (default 0).

**Response `200`:** `PaginatedResponse[ProviderStatusResponse]`

---

### `POST /api/v1/providers/test`

Test a provider configuration before saving it. Returns success/failure without creating any DB records. `api_key` is required for every provider except `ollama` — there is no environment-variable fallback, so a request without one is rejected.

**Request body:**
```json
{
  "provider_type": "claude",
  "api_key": "sk-ant-...",
  "model": "claude-sonnet-4-6",
  "base_url": null
}
```

**Response `200`:**
```json
{"success": true, "message": "claude is healthy"}
```

---

### `POST /api/v1/providers/models`

Fetch available model IDs from a provider's API using the supplied credentials, **before** saving a config. Returns a sorted list of model ID strings. An unreachable host or rejected credentials return `502`; other failures (an unexpected response shape, say) are logged and return an empty list.

**Request body:**
```json
{
  "provider_type": "groq",
  "api_key": "gsk_...",
  "base_url": null
}
```

`base_url` is only needed for `openai_compatible` or Ollama.

**Response `200`:** `string[]` — sorted model IDs, e.g. `["gemma2-9b-it", "llama-3.3-70b-versatile", ...]`

**Response `400`:** Unknown `provider_type`
**Response `502`:** The provider API could not be reached, or it rejected the key (HTTP 401/403)

---

### `POST /api/v1/providers/{provider_type}`

Create a new saved provider configuration.

**Request body:**
```json
{
  "api_key": "gsk_...",
  "model": "llama-3.3-70b-versatile",
  "display_name": "Groq — Free Tier",
  "base_url": null,
  "temperature": 0.0,
  "max_tokens": 4096,
  "is_active": true
}
```

`temperature` and `max_tokens` are not part of the UI form — a config created there gets `0.0` and `4096`. `max_tokens` caps each generation call (clamped to the provider's own output limit); `temperature` is stored but not currently read — every LLM call runs at `0.0`.

**Response `201`:** `ProviderStatusResponse`

---

### `GET /api/v1/providers/{config_id}`

Get a specific provider config by its UUID.

**Response `200`:** `ProviderStatusResponse`

---

### `PUT /api/v1/providers/{config_id}/config`

Update a saved provider config. Omit fields to leave them unchanged.

**Request body:** Same as POST (all fields optional)

**Response `200`:** `ProviderStatusResponse`

---

### `DELETE /api/v1/providers/{config_id}`

Delete a saved provider configuration.

**Response `204`:** No content

---

### `POST /api/v1/providers/{config_id}/models`

Fetch available models using a **saved** config's stored credentials and persist the result to `models_cache_json`. Subsequent calls to `GET /api/v1/providers` will return the cached list in `available_models` without re-fetching.

**Response `200`:** `string[]` — sorted model IDs

**Response `400`:** Unknown provider type, or the config stores no API key (every provider except `ollama` needs one)
**Response `404`:** Config not found
**Response `502`:** The provider API could not be reached, or it rejected the key (HTTP 401/403)

---

### `POST /api/v1/providers/{config_id}/test`

Run a live health check on a saved provider config using its stored credentials.

**Response `200`:**
```json
{"success": true, "message": "claude is healthy"}
```

---

## Settings

### `GET /api/v1/settings`

Return the current application settings (non-sensitive fields only). Mutable values come from the `app_settings` table, so every worker reports the same thing right after a save; the rest are the process's environment-derived values.

**Response `200`:**
```json
{
  "app_name": "Savvina AI",
  "debug": false,
  "log_level": "INFO",
  "ollama_base_url": "http://ollama:11434",
  "default_query_timeout": 30,
  "default_row_limit": 1000,
  "cache_enabled": true,
  "cache_max_age_days": 30,
  "semantic_similarity_threshold": 0.87,
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "db_pool_size": 10,
  "db_max_overflow": 20,
  "schema_pruning_enabled": true,
  "schema_pruning_top_k": 15,
  "bcrypt_rounds": 12
}
```

### `PUT /api/v1/settings`

Update application settings. Each key is persisted to `app_settings` and survives restarts; the response is the full `GET` payload re-read from the database.

**Request body (all fields optional):**
```json
{
  "cache_enabled": false,
  "default_row_limit": 500
}
```

**Accepted keys and bounds:** `default_query_timeout` (≥ 1), `default_row_limit` (≥ 1), `cache_enabled`, `cache_max_age_days` (≥ 0), `semantic_similarity_threshold` (0.0–1.0), `db_pool_size` (1–100), `db_max_overflow` (0–200), `schema_pruning_enabled`, `schema_pruning_top_k` (3–100), `bcrypt_rounds` (10–16). A value outside its bounds returns `422`; unrecognised keys are ignored rather than rejected. To restore a default, delete the row from `app_settings`.

**When a change takes effect:** reads are database-backed, so `GET /api/v1/settings` reflects the new value in every worker at once. Behaviour follows more slowly in a multi-worker deployment: `bcrypt_rounds` is read from the database per password operation and applies everywhere immediately; the rest of the keys are written through to the serving worker's cached settings and reach other workers at their next restart; `db_pool_size` and `db_max_overflow` need a restart in every worker, because the engine is built at startup.

---

## Common HTTP Status Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Created |
| 204 | Success, no content |
| 400 | Bad request (invalid input, connection failed, validation error) |
| 404 | Resource not found |
| 422 | Request body failed schema validation (FastAPI/pydantic) |
| 500 | Server error (LLM failure, unexpected exception) |
| 502 | Upstream provider unreachable or rejected the credentials (provider model-fetch endpoints) |
| 503 | Retryable. Any route can answer `503` with `Retry-After: 5` when a database row lock is held by a long operation on the same connection for longer than `DB_LOCK_TIMEOUT_SECONDS` (detail: "This connection is busy with another operation; try again shortly."). The semantic-generation routes also answer `503` when the LLM provider is unavailable. |

Errors return a JSON body: `{"detail": "Human-readable error message"}`
