# Semantic Model Generation

This document traces the complete lifecycle of auto-generating a `SemanticModel` — from triggering generation via the API through to the final model persisted in the semantic store tables.

> **Code cross-reference:** all file paths are relative to `backend/app/`.

---

## What is a SemanticModel?

**File:** `semantic/models.py`

`SemanticModel` is a Pydantic model (persisted as rows in the `semantic_models`, `semantic_tables`, `semantic_columns`, `semantic_metrics`, `semantic_relationships` and `semantic_global_items` tables, assembled by `semantic/store.py`; `connections.semantic_model` is a legacy column that nothing reads) that enriches the raw database schema with business meaning. It is injected into the LLM system prompt via `SemanticFormatter` on every chat turn.

Key dataclasses and enums in `semantic/models.py`:

| Type | Purpose |
|---|---|
| `SemanticModel` | Root object — tables, metrics, joins, derived columns, segments, relationships |
| `TableSemantic` | Per-table: display name, description, grain, default filters, columns, hierarchy, optional `domain` tag |
| `ColumnSemantic` | Per-column: display name, semantic_type, cardinality, value_mappings, is_sensitive |
| `BusinessMetric` | Named KPI with SQL definition, filters, format hint, metric_type |
| `SimpleMetric / RatioMetric / DerivedMetric / CumulativeMetric / ConversionMetric` | Subtypes of `BusinessMetric` discriminated by `metric_type` |
| `DerivedColumn` | Calculated expression (margin %, duration, etc.) |
| `CommonJoin` | Reusable JOIN pattern between two tables |
| `RelationshipEdge` | FK-derived edge with join SQL and nullability hint |
| `Segment` | Named, reusable WHERE-clause fragment |
| `SemanticType` | Enum: `identifier`, `monetary`, `percentage`, `timestamp`, `categorical`, … |
| `CardinalityClass` | Enum: `unique`, `high`, `medium`, `low` |
| `RelationshipType` | Enum: `one_to_many`, `many_to_one`, `many_to_many`, `one_to_one` |
| `GenerationStatus` | Enum: `idle`, `tables_partial`, `complete` (tracks phased generation progress) |

---

## Storage

**Files:** `semantic/store.py`, `models/semantic_store.py`

Eight tables: one header row per connection (`semantic_models`), one row per table (`semantic_tables`), column (`semantic_columns`), metric (`semantic_metrics`) and relationship (`semantic_relationships`), one row per global item (`semantic_global_items`: segment, derived column or common join), the append-only `semantic_model_history`, and the drift baseline `semantic_known_columns`, one row per table key (see [Known-columns baseline](#known-columns-baseline)). Every row except history and the baseline carries:

- `user_edited_fields` — per-field provenance: the names of the fields a person has written. A regenerate refreshes every field not listed and leaves the listed ones alone.
- `version` — optimistic lock, checked only for table-row edits: `UPDATE … WHERE id = :id AND version = :version`, and a row that moved on makes the store raise `SemanticStoreConflict`, which the router returns as `409`. Column rows and the rewritten list sections carry a `version` but are not checked against it; they rely on the connection row lock that the PUT, DELETE, suggestion-apply and generation routes hold. The header row's `version` is additionally bumped by every write that changes content (`save_model`, `begin_generation`, a changing `reconcile_tables`, any non-echo `apply_user_patch`), which is what the chat path's per-process model cache (`store.load_model_cached`, keyed by header id and version) checks on every turn; `advance_progress` bumps nothing, so a cached model may carry a stale progress figure, which chat never reads.
- `updated_at` and `updated_by`.

`semantic_model_history` is written by the store on every change, not by a database trigger: table and column edits per row, a changed `notes` or `is_user_reviewed` as one `model` row, and each rewritten list section (metrics, segments, derived columns, joins, relationships) as one row. Generation writes history too, with source `generation`: `begin_generation` writes one `model` row (an `insert` when it creates the header, otherwise an `update`); `reconcile_tables` writes one `insert` row per table or column row it creates and one `update` row per table or column that actually changed; the globals finalisation (`save_model`) writes one `model` row only, with no per-row entries for pruned tables or replaced metrics; an Apply Drift saves through `save_model` the same way (source `drift`) and also appends one `drift_apply` row holding its change summary (`record_drift_apply`), which the drift history route reads; `delete_model` writes one `model` delete row. An edit that echoes what is stored writes nothing. The history table has no foreign key to the connection, so the audit trail survives deleting the connection.

Each history row names its entity twice: `entity_id`, the row the change was made to as it existed then, and `entity_key`. For a table or column change `entity_key` is the table key or `table_key.column_name`, and for a header change it is empty. An edit to metrics, relationships, segments, derived columns or common joins is recorded as one row per section, with the header's id and the section's field name (`business_metrics`, `relationships`, …) as its key. `save_model` is a full replace: it deletes every table, column, metric, relationship and global-item row and inserts the rebuilt set with new ids (only the header keeps its id). Keeping ids would need a per-row reconcile that this path deliberately does not do. So a table or column `entity_id` identifies the row only until the next `save_model` (a globals finalisation, a Restart or an Apply Drift), and the key that follows a table or column across rebuilds is `(entity_type, entity_key)` within a connection. `entity_key` is fitted like any other value, so a truncated key can collide; that is the accepted limit of the key.

`semantic/store.py` is the only module that reads or writes these tables; `tests/test_semantic/test_blob_is_unread.py` proves nothing in `app/` reads the legacy `connections.semantic_model` column.

**Read order.** Metrics, relationships, segments, derived columns and common joins carry an `ordinal` and are read back in that order. Tables and columns are ordered by Python string order on `table_key` and `column_name` (`store._assemble_model`), not the generator's order and not the database collation, so the order is the same on SQLite and PostgreSQL and on every path (`load_model` and the model `save_model` returns). The formatter renders tables in that order, so it is prompt content: on a PostgreSQL database with a non-C collation the table order in prompts changed once when this replaced a SQL `ORDER BY`.

**Values are fitted to PostgreSQL's limits** (`_fit_values`) before they are compared or stored. NUL characters are stripped (also inside JSON values), and a bounded text value longer than its column is truncated with a logged warning. Identity values (table key, column name, metric or item name, relationship endpoints) are never truncated: one that does not fit raises `ValueError`, which the routes return as `400`. A segment, derived-column or common-join name is stored as unbounded text but bounded by the unique index it sits in (2000 UTF-8 bytes, `store._MAX_INDEXED_TEXT_BYTES`; a common join's name is its `join_pattern`); the same cap applies in the `0003` backfill, which skips such an item. The globals route drops a generated metric, segment, derived column or join whose name the store would reject (`store.name_rejection`) rather than fail the whole finalisation.

**Migrations.** `0002` creates seven of these tables, all but `semantic_known_columns`. `0003` backfills them from each connection's `semantic_model` blob:

- Each connection is backfilled inside its own SAVEPOINT, so one malformed blob (or a NUL inside a JSON payload) fails only that connection; it is logged and skipped, and the upgrade continues. A final summary line reports `N/M connections backfilled; failed: …` (logged as an error when any failed).
- `user_edited_fields` is seeded empty: nothing in a blob distinguishes LLM output from a hand edit, so the first regenerate treats every migrated value as regenerable.
- It refuses to run in offline `--sql` mode, because the backfill reads data and cannot be rendered as static SQL.
- `connections.semantic_model` is left in place and unread, and is dropped by a later migration (one release on). `store.delete_model` clears it (rows or no rows) and both `DELETE /semantic` and deleting a connection go through that, so the later migration finds no connection with a blob but no `semantic_models` row. `connections.semantic_model_updated_at` is unwritten since the store and no longer served by the connections API; it is dropped by the same migration.
- Downgrading past `0003` is a no-op; roll forward instead.

`0004` adds `semantic_known_columns`, empty: a table with no row is baselined at first sight (see [Known-columns baseline](#known-columns-baseline)).

---

## Two Generation Paths

### Path A — Phased (used by the frontend)

The frontend drives three sequential API calls. Each call commits partial results to the DB so the user sees incremental progress and the browser can refresh without losing work. Batches are named by table key, never by position.

```
POST /api/v1/connections/{id}/semantic/generate/init[?resume=true]
POST /api/v1/connections/{id}/semantic/generate/batch   {"table_keys": [...]}  (1–4 keys, one call per batch)
POST /api/v1/connections/{id}/semantic/generate/globals
```

### Path B — Full (internal / programmatic)

`SemanticModelGenerator.generate()` runs the complete pipeline in a single async call. Internally it calls `_generate_batched()` which is the same batch + globals strategy as Path A, but without inter-call checkpointing.

---

## Complete Call Chain

```
routers/semantic.py: generate_semantic_init()
  └─ _load_schema_for_connection()
  │    ├─ get_connection_or_404()                         # DB load, org-scoped
  │    ├─ UserSchemaCache (DB read)                       # load cached schema JSON
  │    ├─ _schema_from_dict()                             # deserialise to DataSourceSchema
  │    └─ _apply_privacy_to_schema()                      # strip excluded schemas/tables/columns (+ their FKs)
  ├─ table_key(t) for each table / gen.compute_schema_hash(schema)  # work list + hash; no LLM call
  ├─ _lock_generation()                                   # connection row lock (FOR NO KEY UPDATE)
  └─ store.pending_table_keys() / store.begin_generation() # work list + header stamp; no table/column row touched

routers/semantic.py: generate_semantic_batch(body.table_keys)  (one call per batch)
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)                      # instantiate LLM provider
  ├─ edges_in_schema(_build_relationship_graph(schema))   # FK edges from DataSourceSchema.relationships
  ├─ _run_llm_phase(...)                                  # 180 s timeout; 504 / 429 / 503 / 422 / 500
  │    └─ SemanticModelGenerator.generate_tables(schema, provider, model, table_keys, edges)
  │         ├─ _schema_to_ddl(batch_schema_obj)           # compact CREATE TABLE DDL string
  │         ├─ _format_relationships_for_prompt(batch_edges) # FK list for prompt
  │         ├─ provider.generate_structured(              # LLM JSON call → TablesBatchResponse
  │         │    schema_type=TablesBatchResponse, ...)
  │         ├─ _ground_tables_to_schema(tables, batch_schema) # remap near-miss keys, drop invented ones
  │         └─ _enrich_fiscal_table_annotations(tables, schema) # stamp audit timestamp descriptions
  ├─ _lock_generation()                                   # connection row lock; re-check status and run id
  └─ store.reconcile_tables() + store.update_known_columns() + _invalidate_connection_caches(keep_schema=True)
     + store.advance_progress()                           # upsert skipping user-edited fields; baseline the answered
                                                          #   tables' live columns + the real names of stored hidden
                                                          #   keys (store.load_column_names); when the LLM answered
                                                          #   for a table, purge the cached answers and table
                                                          #   embeddings; recount
                                                          #   progress

routers/semantic.py: generate_semantic_globals()
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)
  └─ _run_llm_phase(...)                                  # same mapping as the batch route
       └─ SemanticModelGenerator.generate_globals(schema, provider, model, all_tables, edges)
            ├─ _build_tables_summary(schema, all_tables)  # compact table+column+hint summary
            ├─ _format_relationships_for_prompt(pruned_edges)
            ├─ provider.generate_structured(              # LLM JSON call → GlobalSectionsResponse;
            │    schema_type=GlobalSectionsResponse, ...) #   a failure raises ValueError
            ├─ _detect_ratio_pairs(schema)                # auto-detect num/denom column pairs
            └─ _validate_metrics_time_columns(metrics, schema) # warn if time patterns on fiscal tables
  └─ (router assembles final SemanticModel, after the row lock):
       ├─ store.load_model_with_provenance()              # model + marks + protected values, one pass
       ├─ store.load_header()                             # 409 if a fresh init restarted the run
       ├─ store.load_known_columns()                      # the baseline (effective: each stored live table's entry completed)
       ├─ compute_drift(...).renamed_columns              # near-miss keys of seen live columns not stored by name
       ├─ rename_columns(tables, relationships, marks, protected, renames) # keys, references, edges, marks
       ├─ drop_dangling_edits(marks, protected, schema, privacy) # hand edits naming gone columns
       ├─ prune_to_schema() / edges_in_schema()           # drop what left the database, keep what privacy hides
       ├─ (warning naming every unannotated table)
       ├─ gen.build_time_expressions(dialect)             # dialect-specific SQL time snippets
       ├─ gen.compute_schema_hash(schema)                 # SHA-256 for drift detection
       ├─ gen._auto_generate_notes(schema)                # fiscal-period table guidance notes
       ├─ store.save_model(..., provenance=..., protected=...)  # full row rebuild; returns the stored model
       └─ store.update_known_columns(..., prune=True)     # baseline in step with the stored tables
```

---

## Step-by-Step Detail

### Phase 0 — Schema Loading

**Router:** `routers/semantic.py` → `_load_schema_for_connection(connection_id, current_user, db)`  
**Functions:** `_schema_from_dict()`, `_apply_privacy_to_schema()` (from `services/schema_utils.py`)  
**Model:** `models/user_schema_cache.py` → `UserSchemaCache`

All three phases begin the same way: load the schema JSON from `UserSchemaCache` for this user + connection and deserialise it. If no cached schema exists, the schema is introspected live and cached; a failure there returns HTTP 400. Privacy settings are applied via `_apply_privacy_to_schema()` to strip excluded schemas/tables, `excluded_columns` (exact `schema.table.column`) and the foreign keys on those columns before any LLM call.

### Phase 1 — Init: Batch Plan

**Endpoint:** `POST /{connection_id}/semantic/generate/init`  
**Router:** `routers/semantic.py` → `generate_semantic_init()`

Init makes no LLM call. It takes the table keys from the privacy-filtered schema (`datasources.models.table_key`), hashes the schema (`SemanticModelGenerator.compute_schema_hash`) and sizes the batches with `GENERATION_BATCH_SIZE = 4`. The relationship graph is not built here: each LLM phase builds it when it needs it, with `_build_relationship_graph(schema)`, which reads `DataSourceSchema.relationships` (already populated by `introspect()` from `information_schema`) and makes no DB query.

The router takes the connection row lock (`FOR NO KEY UPDATE`) and stamps the header row (`store.begin_generation`): `generation_status = "tables_partial"`, `generation_progress = {tables_done, tables_total, batch_size}`, `schema_hash` from the cached schema, and a fresh `generation_run_id` (kept, not minted, on `?resume=true`). **Nothing is blanked** — init is never destructive and touches no table or column row; a run reconciles onto the rows that already exist.

`GenerateInitResponse` carries `tables_total`, `batch_count`, `batch_size`, `resumed` and `pending_table_keys`, the exact work list: every table on a fresh run, or only the tables the interrupted run had not stamped on a resume (`store.pending_table_keys` compares each table row's `generation_run_id` with the header's). The frontend slices that list into batches itself; the server keeps nothing positional. `batch_count` is the number of batches the list makes (at least 1). A resume is refused with `400` when there is no `tables_partial` model and with `409` when the schema hash no longer matches. A `tables_partial` model backfilled by migration 0003 from a blob-era interrupted run has neither a schema hash nor a run id; `resume=true` on such a model runs a fresh, non-destructive run (every table pending, hand edits kept) and answers `resumed: false`.

An interrupted run (a provider rate limit, a timeout, a closed tab) leaves the model `tables_partial`, and the page shows a banner with three actions, in order of how much they keep:

| Action | What it does |
|---|---|
| **Resume** | `init?resume=true`, then only the pending tables, then Phase 3 |
| **Finish with what's generated** | Phase 3 alone over the tables already stored. Disabled when no tables exist, since the globals call would have nothing to work from |
| **Restart from scratch** | After a confirmation dialog: `DELETE /semantic`, then a fresh init. The only action that discards work, hand edits included |

The banner is not shown while a run is in flight. A run drops the page's local draft when it starts, so starting one — Regenerate, Resume, Finish or Restart — is blocked while the draft has unsaved edits ("Save your changes first — starting a run discards unsaved edits"); applying a suggestion is blocked the same way. For the duration of a run the editors are read-only ("Editing is paused while generation runs."), and **Save Changes** and applying a suggestion are disabled: a draft saved mid-run would overwrite the regenerated values and mark them user-edited. The draft is re-seeded from the run's result, or after a failed run from a refetch of the model, so a stale draft cannot be saved over what the run already wrote.

The page retries a call that is throttled (`429`), finds the provider unavailable (`503`) or loses its connection, with exponential backoff, up to four attempts; any other failure stops the run, and the remaining batches are not sent. A table a batch reports in `omitted_table_keys` is sent once more, as its own batch; a second omission is left for Phase 3 to report.

### Phase 2 — Batch: Table Annotation

**Endpoint:** `POST /{connection_id}/semantic/generate/batch` with body `{"table_keys": [...]}` (1 to 4 `schema.table` keys)  
**Method:** `SemanticModelGenerator.generate_tables(schema, provider, model, table_keys, relationship_edges)`

For each batch of up to 4 tables, named by key:

1. **`_schema_to_ddl(batch_schema_obj)`** — builds a compact `CREATE TABLE` DDL string for the batch tables. Column lines include native type, NOT NULL, PRIMARY KEY, and inline `--` comments from column descriptions. Sample values are omitted here (this is a semantic DDL, not the full schema). Total DDL capped at `_MAX_DDL_CHARS = 20,000` characters.

2. **`_format_relationships_for_prompt(batch_edges)`** — renders FK edges as `from.col → to.col (JOIN: ...)` lines for the prompt.

3. **`provider.generate_structured(system_prompt, user_message, schema_type=TablesBatchResponse, temperature=0.0)`** — LLM JSON call. The provider is expected to return a `TablesBatchResponse` (a Pydantic model with `tables: dict[str, TableSemantic]`). `temperature=0.0` is used to maximise determinism.

   Prompt template: `_BATCH_TABLES_PROMPT` in `semantic/generator.py` — asks for per-table and per-column: `display_name`, `description`, `grain`, `semantic_type`, `default_aggregation`, `value_mappings`, `is_sensitive`, `currency`, `unit`, `default_filters`, `hierarchies`.

4. **`_ground_tables_to_schema(result.tables, batch_schema)`** — keeps the output grounded in the batch's real tables, because the store persists whatever it is given. A table or column key that is not real is mapped to the real key it matches (quote-stripped, then case-insensitive, and for tables also as a bare table name) provided the match is unique; otherwise it is dropped as invented. Columns the LLM omitted are **not** backfilled: `reconcile_tables` overwrites every unedited field of each column a batch names, so a default column for one the LLM merely skipped would wipe the earlier description and value mappings.

5. **`_enrich_fiscal_table_annotations(result.tables, schema)`** — stamps explicit descriptions on `created_at` / `updated_at` columns of fiscal-period tables (tables with ≥2 `fiscal_year` / `fiscal_quarter`-style columns) to prevent the LLM from treating them as business time axes in future queries.

The batch is named by `{"table_keys": [...]}` in the body (never by position); an unknown key is a `400`. Its output goes through `store.reconcile_tables`: an existing row takes the new value on every field not in its `user_edited_fields`, a missing row is inserted, `is_sensitive` is never cleared by generation, nothing is ever deleted, and every named table is stamped with the run id. A table the LLM returned no annotation for is not stamped, so it stays pending and a resume retries it. In the same transaction each table the LLM answered for gets its baseline entry replaced with its live columns (`store.update_known_columns`; see [Known-columns baseline](#known-columns-baseline)), so a column the LLM left out is not reported as new later. When the LLM answered for at least one table, the same transaction purges the connection's cached query answers and table embeddings, as every other model write does, so the chat stops serving SQL written against the previous annotations; the schema cache, which every batch of the run reads, is kept (`_invalidate_connection_caches(keep_schema=True)`). `generation_progress.tables_done` is recomputed from the stamped rows. The response (`GenerateBatchResponse`: `connection_id`, `table_keys`, `tables_done`, `tables_total`, `batch_size`, `omitted_table_keys`) is progress only, not the model. `omitted_table_keys` names the tables of the batch the LLM returned nothing for (also logged as a warning). They are not stamped and stay pending; the page re-queues them once, and Phase 3 adds a generation warning naming every table still without an annotation (a **Finish with what's generated** that skips tables always produces one).

Before the LLM call the route answers `400` when the model is not `tables_partial` (init was not called) or carries no run id (a blob-era interrupted run backfilled by migration 0003; `/generate/init`, with or without `resume=true`, mints one), `409` when the schema hash no longer matches the one init stamped, and `400` for an unknown table key. It then takes the connection row lock after the LLM call and re-checks state before writing, answering `409` and discarding the batch when the status is no longer `tables_partial` (globals or a delete landed meanwhile), or when a fresh init restarted the run while the batch was in flight (stamping those tables with the new run id would make a later resume skip tables it never generated).

Both LLM phases run their call through `_run_llm_phase`, under a 180-second timeout. A timeout is `504`, logged as a warning. A provider throttle — the `[RATE_LIMIT]` marker the providers put in the message — is `429`, logged as a warning without a traceback; the per-IP rate limiter also answers `429`. An unavailable provider — the `[PROVIDER_UNAVAILABLE]` marker the providers put on a `408`, `409` or `5xx` reply or a failed or timed-out connection (these calls run with the SDKs' own retries off) — is `503`, logged the same way. `429` and `503` are what the page's retry-with-backoff keys on. Any other `ValueError` (the generator wraps every provider error in one) is `422`, and anything else `500`, both logged with the traceback. Every log line names the provider. The providers built on the OpenAI client (every provider except Claude) report an exhausted balance (a `429` with code `insufficient_quota`) as `[QUOTA_EXCEEDED]`, which is a `422` and is not retried, since waiting does not fix it. Gemini's daily and per-minute quotas cannot be told apart, so both stay `[RATE_LIMIT]` and are retried. The Claude provider maps a `429` to `[RATE_LIMIT]`, and an overloaded `529` like any other `5xx`. One case on the OpenAI client is not retried: a `5xx` inside the text-mode fallback that runs after a `400` is an untagged provider error, a `422`.

### Phase 3 — Globals: Cross-Table Sections

**Endpoint:** `POST /{connection_id}/semantic/generate/globals`  
**Method:** `SemanticModelGenerator.generate_globals(schema, provider, model, all_tables, relationship_edges)`

Generates cross-table business KPIs, derived columns, join patterns, and named segments in a single LLM call:

1. **`_build_tables_summary(schema, all_tables)`** — compact one-line-per-table summary: `schema.table (col:type, ...) [primary_date=col] [primary_ts=col]`. Including types lets the LLM distinguish fiscal integer dimensions from timestamp audit columns.

2. **`_format_relationships_for_prompt(pruned_edges)`** — FK edges pruned to only tables that were actually generated.

   The `all_tables` the router passes in is `prune_to_schema(stored_tables, schema)` called *without* the privacy settings, so tables and columns that left the database, or that the connection's privacy settings hide, never reach the LLM (the stored rows for hidden items are kept; see Phase 3 Finalisation).

3. **`provider.generate_structured(schema_type=GlobalSectionsResponse, temperature=0.0)`** — LLM JSON call. Returns `GlobalSectionsResponse` with:
   - `business_metrics: list[BusinessMetric]` — KPIs with SQL definitions and `metric_type` discrimination (`simple`, `ratio`, `cumulative`, `conversion`, `derived`)
   - `derived_columns: list[DerivedColumn]` — calculated expressions (margins, rates)
   - `common_joins: list[CommonJoin]` — reusable JOIN patterns
   - `segments: list[Segment]` — reusable WHERE-clause fragments

   Prompt template: `_GLOBAL_SECTIONS_PROMPT` in `semantic/generator.py`.

4. **`_detect_ratio_pairs(schema)`** — purely structural scan for column name pairs matching `_RATIO_NUMERATOR_KEYWORDS` (spent, actual, cost, …) vs `_RATIO_DENOMINATOR_KEYWORDS` (budget, limit, quota, …). Auto-generates `DerivedColumn` hints for utilisation ratios not already covered by LLM output.

5. **`_validate_metrics_time_columns(metrics, schema)`** — scans metric definitions for time-series SQL patterns (`date_trunc`, `interval`, `lag`, etc.) applied to fiscal-period tables; emits `generation_warnings` strings.

`generate_globals` raises `ValueError` on an LLM failure, like `generate_tables`, so a failed globals call is answered like a failed batch (`422`, `429` on a throttle, `503` on a provider outage, `504` on a timeout). Nothing is written, so a model a run left `tables_partial` stays that way, with **Finish with what's generated** and **Resume** available, rather than being completed without metrics.

### Phase 3 Finalisation (router)

After `generate_globals()` returns, `routers/semantic.py: generate_semantic_globals()` takes the connection row lock and reads the model, its provenance marks and the stored values of the marked fields in one pass over the rows (`store.load_model_with_provenance`; `409` if the model was deleted or its status moved on meanwhile, or if a fresh init restarted the run while the LLM call was in flight — the header is `tables_partial` with a run id other than the one read before the call; a re-finalise of a `complete` model has no run id before or after and is not affected), then assembles the final model:

- **Hand-authored items are carried through.** A metric, segment, derived column or common join with provenance replaces any generated item of the same name (or, for joins, the same `join_pattern`); the generator's set is added around it. A generated item whose name the store would reject (`store.name_rejection`: a metric name over 512 characters, or a segment, derived-column or join name over the index's byte limit) is dropped with a warning. Only what exists is carried: deleting a generated item (a metric, segment, derived column, join or foreign-key relationship) leaves no provenance mark, so the generator's set may add it back; renaming one carries the renamed copy, and the original name may come back beside it. Remembering deletions needs tombstones, which the store does not keep.
- **Pruning.** `prune_to_schema` drops tables and columns that left the database and clears references to them (primary timestamp/date column, partition and cluster columns, hierarchy levels; a hierarchy left with no levels is dropped). `edges_in_schema` drops relationships whose endpoints left the database, bounded by the live schema rather than the annotated tables, so a hand-written edge to a real but unannotated table survives. `carry_edited_relationships` then keeps hand-edited relationships through the regenerated set. **What the connection's privacy settings hide is kept, not pruned**: an excluded table, or a column in `excluded_columns`, is absent from the schema because it is hidden rather than gone, so it is carried through together with references to it. For relationships only the edges a user has marked (edited) are carried through, by `carry_edited_relationships`; an unmarked foreign-key edge to a privacy-hidden endpoint is not regenerated while the endpoint is hidden (the fresh edges come from the narrowed schema) and returns on the next globals run after it is un-hidden. **So is a table the connection's role cannot read**: `information_schema` lists only what the role holds a privilege on, so the PostgreSQL adapter also reads `pg_class` (if it can; a failed read logs a warning and leaves the list empty) and records each table that exists but is not visible in `DataSourceSchema.unreadable_tables`. Globals passes `keep_unreadable=True` to `prune_to_schema` and `edges_in_schema`, so connecting with a narrower role cannot delete those annotations; the LLM-prompt calls leave the flag off, since nothing over such a table could run. The MySQL adapter cannot tell an unreadable table from a dropped one and leaves the list empty. `default_filters` are free SQL and are not rewritten.
- **Renamed column keys.** Before the dangling-edit trim and the prune, the near-miss keys `compute_drift` reports in `renamed_columns` (read through `_effective_known_columns`, as Check Drift reads them) are renamed by `rename_columns`: a key such as a blob-era `"status"` or `Status` of a live column the baseline has seen, which the table does not store under its real name, gets that name, with its annotation, its marks and protected values, its table's references and its relationship endpoints. A column this run annotated under its real name is stored, so its near-miss key is not renamed: the prune below drops it as a duplicate.
- **Dangling hand edits.** Before the save, `drop_dangling_edits` removes from the hand edits the names of columns the table no longer has (a column the privacy settings hide is not gone; a table absent from the schema is left to `prune_to_schema`). Partition and cluster columns and hierarchy levels keep their live entries and their edit mark (a hierarchy left with no levels is dropped). A primary timestamp or date column naming a gone column, or a list left empty, is cleared and unmarked, so the pruned generated value wins and a later regenerate may fill it again. Each changed field adds a note to the model's `generation_warnings` and is logged.
- **Unannotated tables.** A table of the schema with no stored row after pruning is one the LLM returned nothing for (twice, after the page's re-queue), one a **Finish with what's generated** never reached, or one created since the run started. The model is completed regardless, with one warning naming them: "N table(s) have no annotation (…): the LLM returned nothing for them, the run was finished before reaching them, or they are new since the run started. Regenerate to annotate them." (up to five keys are listed).
- Near-miss spellings in stored keys that are not renamed (for example rows migrated from a blob with an LLM's `public.Orders`, or a column key duplicating a stored column) are dropped like any absent key, with a warning naming the key they would have matched: the prune never remaps by itself, because a remap would keep the value but lose the user-edit marks keyed by the old spelling. The renamed column keys above are rekeyed marks and all before the prune runs.

| Method | What it produces |
|---|---|
| `gen.build_time_expressions(dialect)` | Dict of `label → SQL expr` for common time periods (`this_month`, `rolling_30`, etc.); dialect-specific (PostgreSQL vs MySQL) |
| `gen.compute_schema_hash(schema)` | SHA-256 of sorted table+column structure for cheap drift detection later |
| `gen._auto_generate_notes(schema)` | Cross-table domain notes for fiscal-period tables — warns that `fiscal_year`/`fiscal_quarter` are the correct time axes, not `created_at` |

The final model is written through `store.save_model` (a full row rebuild that restores per-field provenance and keeps user-authored metrics, segments, derived columns, joins and relationships) with `generation_status = "complete"`, `generation_progress = None`, `generation_run_id = None` and `is_user_reviewed = False` (review state is reset on purpose). The model's `generation_warnings` are the generator's warnings, then the dangling-edit notes, then the unannotated-table warning. In the save's transaction the baseline is brought in step with the stored tables, and the rows of tables the save removed are pruned (`store.update_known_columns(prune=True)`; see [Known-columns baseline](#known-columns-baseline)). The connection's query cache, schema cache and table-embedding cache are purged (each batch the LLM answered for purged the query and embedding caches too; the schema cache, which the batches read, is purged only here) and the response is the model `save_model` returns, which is what a later `GET` returns. It can differ from what was assembled because the store fits values and de-duplicates name-keyed items.

Because regenerating never blanks the model, this is where a full run removes tables and columns that left the database — the same `prune_to_schema` pass `POST /{connection_id}/semantic/apply-drift` runs without the LLM (see **Apply Drift** below).

---

## Post-Processing (Path B — Full generation)

When `SemanticModelGenerator.generate()` is called directly (not via phased endpoints), additional enrichment runs after `_generate_batched()`:

| Method | What it does |
|---|---|
| `_infer_temporal_columns(table_key, columns)` | Per-table: selects `primary_timestamp_column` and `primary_date_column` by name+type priority list; suppresses audit timestamps on fiscal-period tables |
| `_fingerprint_column(datasource, schema, table, col)` | Reads `pg_stats` (PostgreSQL only) for `n_distinct`, `null_frac`, `common_vals`; computes `cardinality` + `semantic_type` without scanning user data rows |
| `_classify_cardinality(n_distinct)` | Maps PostgreSQL `n_distinct` → `CardinalityClass` (unique / high / medium / low) |
| `_infer_semantic_type(name, data_type, n_distinct, ...)` | Local inference: PK → `identifier`, name patterns → `monetary`/`email`/`foreign_key`/etc., type → `timestamp`/`date`/`boolean_flag`, cardinality → `categorical` |
| `_populate_schema_metadata(tables, schema)` | Backfills `base_sql` (view definitions), `AggregationType.COUNT_DISTINCT_APPROX` for high-cardinality ID/FK columns |

---

## Drift Detection

**Diff:** `semantic/drift.compute_drift(schema, stored_model, privacy, known_columns)` → `SchemaDrift` (`new_table_keys`, `removed_table_keys`, `removed_columns`, `renamed_columns`, `added_columns`, `dropped_columns`, `unreadable_table_keys`)  
**Method:** `SemanticModelGenerator.detect_drift(current_schema, stored_model, privacy, drift=...)` → `list[str]`  
**Endpoint:** `GET /{connection_id}/semantic/drift`

The route first re-introspects the source live through `refresh_user_schema_cache` (the helper behind the explicit schema Refresh), stores the result as the caller's schema cache, and flushes table embeddings and cached query answers only when the caller had no cached schema or the structural hash (`compute_schema_hash`: table and column names, types, primary keys) of its cached one differed from the live one (every model write except an apply or a generation batch clears all users' schema caches, so the first check after a write usually flushes). If the source cannot be reached it falls back to the cached schema and reports `schema_source: "cache"`. The comparison itself is purely structural — no LLM, no statistical queries:
- Tables removed from the DB since generation → warning per missing table
- Columns removed → warning per missing column
- Tables added since generation → "N new tables added" summary
- Columns added to a table the model holds → "N new column(s) added" summary. A live column is new when it is in neither the stored table nor the table's baseline (see [Known-columns baseline](#known-columns-baseline)). `compute_drift` reports none for a table with no baseline entry; the routes read the baseline through `_effective_known_columns` (below), which gives every stored live table one. A near-miss key (quoted or case-only, `resolve_real_key`) of a live column the baseline has seen and the table does not store under its real name is a rename (`renamed_columns`), worded as one: neither a removal nor a new column. Of two such keys for one column the first in sort order is renamed and the other is worded as a duplicate. A near-miss key of a column the baseline never saw is a removal, and its column is new; a removed key the baseline knows is a genuine drop (`dropped_columns`), named verbatim
- Metric references to now-absent tables → warning per broken metric

The route passes the connection's privacy settings, so a table or column the policy hides is not reported as removed, and a metric naming a hidden table is not reported as broken — including a bare table name under an excluded schema, which is resolved through the qualified keys the stored model holds. A stored table in the schema's `unreadable_tables` is not reported as removed either (PostgreSQL only, and only when `pg_class` was readable: the MySQL adapter leaves the list empty, and so does PostgreSQL when the `pg_class` read fails, so there such a table is reported, and applied, as removed); one warning names those tables instead, because a missing grant is not something a regenerate can fix.

Returns human-readable warning strings; the frontend displays them as a `DriftReport`. The route builds the `SchemaDrift` once, passes it to `detect_drift`, and sets the report's `has_actionable_changes` to `not drift.is_empty` (a table added or removed, or a column added, removed or renamed). Apply-drift runs the same `compute_drift` on its own live introspection, so the apply acts on every change the check flags as actionable; an unreadable table or a metric naming a missing table is reported but not actionable. A near-miss stored key of a live column (quoted, or differing only in letter case) is one such change: `compute_drift` and the prune share one membership rule, `_prune_table`'s, so the key is not present. The apply renames it when it is in `renamed_columns` (above) and otherwise removes it; the check reports it as a rename; as a duplicate when the model also stores the real name (or another key is renamed to it); or, when the baseline never saw the real name, as a near-miss key of a column the model has not seen, which the apply removes, with the real name reported as a new column. A removed key the baseline knows is a genuine drop instead. `policy_hides_table` / `policy_hides_column`, the privacy rules behind "hidden is not removed", live in `semantic/drift.py` too.

The diff reads the baseline through `_effective_known_columns`, which completes the entry of each stored, live table to what a write records for it (`_baseline_entries`): a table with no entry gets its live columns, so a legacy table reports no new column and a near-miss key of one of its live columns is a rename; an entry gains the real names of the table's stored keys the policy hides and its stored live columns, so a baseline written before hidden names were recorded catches up, and un-hiding such a column renames its key instead of reporting the column new. On a live check outside a run (`tables_partial`) the route persists what the read completed: it commits the schema refresh first, then takes the connection row lock, re-reads the header (a run started meanwhile writes the baseline itself, so the write is skipped) and the baseline (a batch or an apply may have moved it; a row is replaced whole), writes `_baseline_changes` of the completed read with `update_known_columns`, and commits at once, so the lock is released before the diff. The refresh is committed before the lock as Apply Drift does: it writes the user's schema-cache row and may delete query and embedding cache rows, which every writer that takes the lock and then purges caches deletes too, so holding them while waiting for the lock could deadlock. The check can therefore answer `409` (the connection was deleted) or `503` (the lock wait timed out).

### Apply Drift

**Endpoint:** `POST /{connection_id}/semantic/apply-drift` (`routers/semantic.py: apply_drift`), 5 requests/minute.

Syncs the model to a schema change without re-annotating unchanged tables. The route:

1. Loads the model and its provenance (`load_model_with_provenance`; 404 if there is none) and refuses a `tables_partial` model with 409: pruning under a run would race its batches.
2. Introspects **live** through `refresh_user_schema_cache` — never the cache, since acting on a stale schema could delete what still exists (an unreachable source is a 400) — and narrows it with the privacy policy.
3. Runs `compute_drift` and commits the refreshed schema cache right away, so it is not held open through the LLM calls and a later rollback does not lose it. An empty diff returns the model unchanged with an empty summary.
4. For **new tables**: the first `_DRIFT_MAX_NEW_TABLES` (3 × `GENERATION_BATCH_SIZE` = 12) are sent to `generate_tables` in `GENERATION_BATCH_SIZE` batches — three batches and the globals pass are four LLM phases of up to 180 s each, which keeps one request inside the proxy's 900 s — and the rest are named in a warning (`_deferred_tables_warning`: "Run Check Drift and apply again") for the next apply. The order is `_new_tables_in_send_order`: sorted keys, except that — only when the cap applies — the tables the newest logged apply reported in `tables_not_annotated` go last, so tables the LLM keeps skipping cannot starve the rest. Then, if the LLM annotated at least one, one `generate_globals` over the strictly pruned existing tables plus the new ones. No LLM call otherwise; LLM failures map to 422 / 429 / 500 / 503 / 504 through `_run_llm_phase`, as in generation, and the model is not written (the schema cache already is).
5. For **new columns** of held tables (`drift.added_columns`): `_column_tables_in_send_order` sorts the tables and puts last those the newest logged apply reported in `column_tables_failed`, and `_plan_column_batches` packs them into the batches the new tables left of `_DRIFT_MAX_BATCHES` (3), each holding at most `GENERATION_BATCH_SIZE` tables, `_DRIFT_MAX_COLUMNS_PER_BATCH` (25) columns and `_MAX_DDL_CHARS` of DDL (`table_ddl_sizes`; past it `_schema_to_ddl` drops tables from the prompt). A table that alone breaks a cap still goes, by itself: cut to its first 25 columns by name, or with its DDL left out of a prompt that still names the columns. Deferred tables and cut columns are named in warnings (`_deferred_columns_warning`, `_cut_columns_warning`). Each batch is one `generate_new_columns` call (`_NEW_COLUMNS_PROMPT`): the tables' DDL and stored descriptions as context, only the named columns asked for; the answer is grounded to the schema, given the fiscal audit-timestamp rule `generate_tables` applies and filtered to the requested names, and a table with no requested column annotated is absent from it. The phase is not fatal. A failed call ends it with a warning, the batches after it are not tried, and the rest of the drift, including every batch answered before, is applied; a table a batch returned nothing for is failed too (an empty answer is a soft failure, not evidence the columns need no annotation), and the phase goes on. The exception is an apply with nothing else to do (no new or removed table and no removed or renamed column), when no batch has answered yet: a `429` or `503` (`_TRANSIENT_STATUSES`) propagates, so the client can retry. The provider is resolved up front when there are new tables or the apply is column-only (an unresolvable one is a `400`); beside removals or renames alone a resolution failure fails this phase like a failed call, and the removals and renames are applied. A renamed column is never sent: it needs no LLM call. New columns trigger no globals pass.
6. Under the connection row lock: re-reads the model and its provenance, re-checks the header (a run that started meanwhile wins — 409), drops the new columns another writer stored meanwhile (and those of a table the model no longer holds), recomputes `compute_drift` against that re-read model and the baseline as the lock sees it (a table a PUT added meanwhile that the database no longer has leaves with its items; a drift another tab already applied is empty, and the route returns the current model unchanged without writing, purging or logging), then assembles the result in `_drift_applied_model` (a table another run or apply stored while the LLM calls ran is left as stored, neither overwritten nor treated as new): renames the drift's `renamed_columns` (`rename_columns`, with their annotations, marks, protected values, table references and relationship endpoints), trims hand edits naming gone columns (`drop_dangling_edits`), prunes with `prune_to_schema(..., keep_unreadable=True)`, adds the annotated new columns to their tables additively (`_with_new_columns`: a column the table holds is never replaced, and no table-level field is derived from them, so a new timestamp column does not become `primary_timestamp_column`, and hierarchies and partition columns are left alone), drops the metrics, joins and derived columns that name a removed table by its stored key, drops a segment whose every `applicable_tables` entry is removed (otherwise only the removed names leave its list; the provenance marks of every removed metric, join, derived column and segment go with it, so a same-named item the LLM adds is a fresh, unmarked one), merges the LLM's items **additively** and only when they name a newly annotated table (`names_any_table` over a metric's `related_tables`, a join's `tables`, a derived column's `base_tables`, a segment's `applicable_tables`; a segment with no tables is not added), so an item the user deleted over unchanged tables is not re-added (an existing name keeps its payload), and rebuilds relationships (`_rebuild_relationships`, shared with globals) — edges to a gone endpoint leave, marked (hand-edited) edges are carried over the catalog's (`carry_edited_relationships`), a catalog edge the model does not have is added only when it touches a newly annotated table or one of the columns `_drift_new_columns` returns: the new columns (`added_columns`) intersected with those this apply records as seen (annotated, or left out of a table the LLM answered for). A new column is not recorded when its batch failed or returned nothing for its table, when it was not tried (a batch after a failed one, or no provider could be resolved), or when it was deferred or cut; its edge then arrives with the apply that later records it: once, so a later deletion sticks. A renamed column is not new, so it brings no deleted edge back. Any other missing edge is one the user deleted and stays deleted (the same rule as for LLM items; the cost is that a foreign key added later between two existing columns waits for a Regenerate, since a deletion leaves no mark to tell it from "never had it"). An edge that is alive only because privacy hides or the role cannot read an endpoint is kept.
7. Saves through `save_model(source="drift")` with the provenance and protected values, so every surviving hand edit is restored; resets `is_user_reviewed` (and stamps `generated_at` / `generation_model`) only when new tables or new columns were annotated; replaces `generation_warnings` with the apply's own (`_validate_metrics_time_columns` over the metrics the apply actually added, then the dangling-edit notes, then the warnings naming the new tables and columns left for a later apply, filtered through the post-lock drift, then the new columns that were not annotated; the globals pass's own warnings are dropped because they may name metrics that were not added); purges the connection's query and embedding caches; the schema cache, which holds this apply's own live introspection, is kept (`_invalidate_connection_caches(keep_schema=True)`), so the page's column lists read the applied schema. The baseline is written in the save's transaction (`update_known_columns(prune=True)`; see [Known-columns baseline](#known-columns-baseline)). When the apply changes nothing its summary can name (the LLM returned nothing for the only new tables or columns, or a column batch failed, and no hand edit was trimmed), nothing is saved or purged and `schema_hash` and `time_expressions` are not refreshed, but an apply that skipped tables or marked `column_tables_failed` is still logged (the next apply reads that entry in `_new_tables_in_send_order` and `_column_tables_in_send_order` to retry them last), and a baseline that moved (for example, columns an answered batch left out are seen now) is committed; the summary still lists `tables_not_annotated` and `column_tables_failed`.
8. Answers with `{semantic_model, summary}`: the stored model and `drift.summarize_model_change(before, after)` — a by-name diff of the model read under the lock against the one stored, so every cascade is named — plus `tables_not_annotated` (tables sent to the LLM in this apply that it returned nothing for, so still missing; the next check reports them again — tables past the cap are in the warning instead) and `column_tables_failed` (tables whose new-column batch failed or returned nothing for them, plus those the previous apply marked that this one got no answer for, so that one table that always fails cannot starve the rest). `columns_added` lists the columns added to each surviving table, and `columns_renamed` the keys renamed in it, each to its live name (passed in as `drift.renamed_columns` and kept where the stored model shows the rename; a relationship on a renamed column is compared under its new name, so it is neither removed nor added). `_drift_applied_model` also reports which global items it took out (`removed_items`), so `summarize_model_change` lists an item removed with its table and replaced by a same-named LLM item under both `*_removed` and `*_added`. The summary is logged as one `drift_apply` row in `semantic_model_history` (`store.record_drift_apply`), in the save's transaction. `GET .../drift/history` (`store.list_drift_applies`, 1–50 entries, default 10) serves the log, skipping an entry whose stored summary no longer validates; the page shows it as **Drift history**.

`_drift_applied_model` is a pure helper with no session (the dry run passes `log=False`, so a preview logs none of the drop warnings). **`dry_run=true`** stops before the first LLM call (once steps 4 and 5 have planned what to send) and runs `_drift_applied_model` on the unlocked read from step 1, with no new tables, no annotated columns and no LLM items, and with the columns the apply would send standing in for the ones it would record; it then returns `summarize_model_change` of that against the stored model with `dry_run: true`, in `tables_added` the names of the new tables the apply would send to the LLM (the capped list; a warning names the rest), in `columns_added` the new columns it would ask for, and in `columns_renamed` the keys it would rename. The preview is therefore computed by the code that applies it, and its `relationships_added` shows the catalog edges on those new columns between held tables. It cannot include what only the LLM or the catalog rebuild adds for new tables (their relationships and cross-table items), and its `warnings` are the notes on hand edits the apply would trim plus the warnings naming the new tables and columns it would defer or cut. The page shows it (`DriftSummaryPanel`) with **Apply these changes** / **Cancel** before anything is written. A preview writes no semantic rows and is never logged, but like Check Drift it refreshes the caller's schema cache and, when the caller had no cached schema or its structural hash differed, flushes the connection's table embeddings and cached query answers (`refresh_user_schema_cache` with its default `flush="changed"`).

What differs from a full Regenerate: unchanged tables are never re-annotated, generated metrics, joins, derived columns and segments are added only over the new tables and beside the existing ones, rather than replacing the unmarked ones, and the user sees what will change, what did change, and the most recent applies (the last 10 on the page). A new column is annotated on its own and added beside the stored ones; it changes no table-level field and feeds no globals pass, so metrics over it wait for a Regenerate.

### Known-columns baseline

**Files:** `semantic/store.py` (`load_known_columns`, `update_known_columns`, `load_column_names`), `models/semantic_store.py` (`SemanticKnownColumnsRow`)

"The stored table lacks a live column" does not prove the column is new: the LLM may have omitted it (generation never backfills an omitted column) or the user may have deleted its annotation. So the store records, per table, the live column names the model has seen, and a column is *new* only when it is in neither the stored table nor that baseline.

- **Where.** `semantic_known_columns`, one row per `(connection_id, table_key)` holding that table's sorted live column names (migration `0004`). It sits outside the rows `save_model` rebuilds and is not part of `SemanticModel`: no version bump, no history row, no cache purge. It is a table of per-table rows, not one JSON value on the header, so every write touches only the tables it names. The rows go with the header (`ON DELETE CASCADE`). A table with no row has no baseline yet.
- **Who writes it.** Each generation batch replaces the entries of the tables the LLM answered for with their live columns (those the privacy policy leaves visible), whether or not it annotated every column, plus the stored hidden names below (it reads the reconciled tables' column keys with `store.load_column_names`); a table the batch got nothing for stays pending, and its entry is left as it was. The globals finalisation keeps, for each stored live table, the columns it had seen that are still live (a table with no entry is seeded with its live columns) and prunes the rows of tables the saved model no longer holds. Apply Drift writes it in its save's transaction; an apply that saves nothing writes it with its log entry, or, with no log entry, commits it alone only when it changed. A new table it annotated gets its live columns (replacing any stale row), a held table keeps what it had seen that is still live plus the new columns a batch answered for, and `prune` drops the rows of removed tables. A live Check Drift outside a run writes what its read completed (`_effective_known_columns`, re-read under the lock): a table with no entry gets its live columns and stored hidden names, and an entry lacking a stored hidden name or a stored live column gains it; Apply Drift and the finalisation complete entries in memory the same way and persist them with their own write. `save_model` and user edits (`PUT`) never touch it.
- **Superset of what is stored.** Every reader and writer builds its entries with `_baseline_entries`, which adds what the table stores (`_held_column_names`): every live column it stores, so a column added through `PUT` is seen once a live Check Drift, the finalisation or an apply writes, and the real name (quote-stripped) of every stored key the privacy policy hides (`policy_hides_column`: the key or its quote-stripped form, matched exactly; a case-only `SSN` of a hidden `ssn` is not hidden, so the prune drops it and nothing records it). The gap: a new column added through `PUT` and deleted again before that is reported as new.
- **Leaving the database.** The finalisation and the apply intersect each entry with the live columns, so a column that leaves the database leaves the baseline with its annotation, and one that comes back is new again. A column the privacy policy hides is absent from the live columns, so it leaves the entry too unless the model stores it: one it stores stays seen under its real name, so once un-hidden it is not new, and a near-miss key of it (a blob-era `'"ssn"'`) is renamed; an entry from before hidden names were recorded gains them at the next check, apply or finalisation, while the column is still hidden; one it never annotated is reported once un-hidden and annotated by the next apply. A hidden or unreadable table is not in the live schema, so its entry is not rewritten, and the prune keeps it because the model still holds the table.
- **Answered or not.** A column left out of a table the LLM answered for is recorded as seen (named in a warning; a Regenerate annotates it), so drift clears. A table whose batch failed, or that the batch returned nothing for, keeps its columns new: it is named in `column_tables_failed`, retried, and sent last. Columns that were not tried or were deferred stay new too, and their table keeps the mark the previous apply gave it, if any; cut columns stay new as well, but a table this apply answered for loses its mark.
- **Renames.** A live column held only under a near-miss key (quoted or case-only) is renamed only when the baseline has seen it (`_column_renames`, the rule behind `compute_drift`'s `renamed_columns`, which the apply and the finalisation both act on). It is then stored under its live name, so both writers keep it seen with no special case. A near-miss key of a column the baseline never saw is a removal, and the column is new.

---

## How the SemanticModel Reaches the LLM Prompt

**File:** `semantic/formatter.py` → `SemanticFormatter`  
**Method:** `SemanticFormatter.format_for_prompt(model, include_time_exprs)` → `str`

Called by `PromptBuilder.build_system_prompt()` (step 5 of the prompt assembly). Renders the full model as structured text:

```
SEMANTIC CONTEXT:

NOTES (apply to all queries against this database):
  • <fiscal-period guidance from model.notes>

Table "public.orders" → Orders
  Description: Completed customer purchase records.
  Grain: one row per order
  Default filters: status != 'deleted'
  Columns:
    - status (Order Status): Active='A', Deleted='D'  [SQL values: 'A' | 'D']
    - total_amount (Total Amount) (currency: EUR) [aggregate: sum]: Revenue amount
  Aggregate with: total_amount (EUR)
  Group by:       status
  Date columns:   created_at [primary timestamp]
  Identifiers:    id, customer_id  [do not aggregate]

RELATIONSHIPS:
  FROM orders LEFT JOIN customers ON orders.customer_id = customers.id [foreign key]

Business Metrics:
  - Total Revenue = SUM(public.orders.total_amount) [currency_eur]
      Sum of all completed order amounts

DERIVED CALCULATIONS (always use these exact expressions):
  "Gross Margin %" = (base_price - cost_price) / NULLIF(base_price, 0) * 100

NAMED FILTERS (use these exact WHERE clauses — do not rewrite them):
  "active_customers": status = 'active' AND deleted_at IS NULL

TIME EXPRESSIONS (use these for date-based questions):
  "this_month" = DATE_TRUNC('month', CURRENT_DATE)
```

`include_time_exprs` is set to `False` for non-temporal questions (`has_temporal_reference()` check in `PromptBuilder`) to avoid emitting all 15 time-expression snippets on every turn.

`SemanticFormatter._append_column_summary()` groups columns by semantic type into `Aggregate with`, `Group by`, `Date columns`, and `Identifiers` blocks — giving the LLM a pre-computed column-role summary rather than requiring it to infer roles from raw types.

---

## Model Update

| Endpoint | File | What it does |
|---|---|---|
| `PUT /{id}/semantic` | `routers/semantic.py: update_semantic_model()` | Applies the patch through `store.apply_user_patch`: per-field provenance, optimistic lock, `409` on conflict |
| `DELETE /{id}/semantic` | `routers/semantic.py: delete_semantic_model()` | Deletes every semantic row and the legacy `connections.semantic_model` blob through `store.delete_model` |

`apply_user_patch` marks every field a person changes in that row's `user_edited_fields`, which is what makes the next regenerate skip it. The frontend PUTs the whole draft on every save, so each incoming value is compared with what is stored and only a field that actually differs is written, marked and audited. A patch that names a table or column the model does not have must supply its `display_name` (it is required on `TableSemantic` and `ColumnSemantic`); otherwise the store raises `ValueError` before writing anything, including the header a first edit would create, and the route answers `400`. In the patch, a `null` top-level section is "not supplied" (the stored value is kept) and `[]` is an explicit clear. Inside a table, `null` also means "not supplied" for `display_name` and `default_filters`, and inside a column for `display_name`, `value_mappings`, `is_sensitive`, `is_non_additive` and `semantic_type`; for every other nullable field (`description`, `grain`, `currency`, …) `null` clears the value. Metrics, joins, segments, derived columns and relationships are replaced as whole sections when supplied, but provenance is per item: only an item that is new or whose payload changed is marked. Applying a suggestion (`POST …/suggestions/{id}/apply`) goes through the same patch path, so it is also remembered as a hand edit. The route reads the suggestion under the connection row lock and refuses one already applied with `409`, so a double click or a second tab cannot apply it twice over a later edit.

### Regenerate and hand edits

A regenerate refreshes the rest of the model but leaves hand-edited fields alone: `reconcile_tables` skips each field in `user_edited_fields`, and the globals step carries hand-authored metrics, segments, derived columns, joins and relationships through. Only **Restart from scratch** discards them. Edits to an item are kept; deleting a generated item is not remembered (no tombstone is stored), so a regenerate may add it back, and renaming one may bring the original name back beside the renamed copy.

---

## Chat Path

`services/chat_service.py` loads the model with `semantic_store.load_model_cached`, which costs one SELECT of the header's id and `version` per turn and reassembles the model only when they moved (a per-process cache of up to 32 connections; each worker process keeps its own). Only the cache-miss rebuild runs inside a SAVEPOINT, so on PostgreSQL a failed rebuild cannot abort the turn's transaction: the store raises `SemanticModelUnavailable` (chained to the real error), the one failure the chat stage catches, and it costs the turn its business context, not the turn itself. A cache hit costs exactly one statement and opens no SAVEPOINT; a failure of that key read is not confined, so the store raises it as itself and the turn fails at the read with the real error (rather than continuing on a transaction PostgreSQL has already aborted and failing later at some unrelated statement). A session whose open transaction has written that connection's model through the store bypasses the cache, so nothing uncommitted is ever cached; other pending changes in the session do not. The cached instance is shared across turns, so everything downstream only reads it or builds `model_copy` copies.

Generation keeps annotations for the tables and columns the privacy settings hide, so the model is narrowed before it reaches the prompt, in three steps:

1. **Free text: `services/semantic_privacy.py: without_hidden_identifiers(raw, full_schema, privacy)`.** Business metrics, segments, derived columns and notes are free SQL or prose and cannot be narrowed structurally, so this drops what names a hidden identifier. A name is hidden when the privacy settings hide it. A stored table absent from the privacy-filtered *full* schema (not the relevance-narrowed one, from which a table may be absent merely because the question does not need it) contributes names only when its schema or the table is excluded: its key, its bare name when no visible table shares it, and its stored columns. A stored column a visible table lacks contributes names only when `excluded_columns` lists it: `schema.table.column`, `table.column` and the bare name, each only when no visible table or column has that form. `excluded_tables` and `excluded_columns` seed the same names, so an excluded item the model never annotated is caught too. A table or column that is merely gone (dropped or renamed in the database, with no exclusion) hides nothing: step 2 already takes it out of the tables and relationships, and a metric or note that still mentions it is stale rather than secret, so it stays in the prompt until it is edited or removed (a generated item may also be replaced by the next regenerate). With no privacy settings nothing is hidden. The exclusions are compared case-insensitively here, so a case-only difference between a stored spelling and an exclusion still hides it. An excluded schema matches as the prefix of a dotted reference written with no whitespace at the dot (`hr.payroll`, `"hr"."payroll"`; "Total sales. Includes refunds" is not a reference to schema `sales`), unless the schema name is also a visible table's bare name. Matching is case-insensitive and by whole identifier (Unicode word characters, with quotes and brackets as separators; a name with other characters, such as a space or hyphen, is matched as a substring). It drops each business metric, segment, derived column and common join that mentions a hidden name in any string except the fixed-vocabulary fields (`metric_type`, `aggregation`, `format_hint`, `calculation`) — common joins are not rendered today, so that is precautionary — drops matching `notes` and per-table `default_filters` entries, and clears a table's `base_sql` when it mentions one. Each drop is logged at debug level with the identifier that caused it. It returns the input itself when there are no privacy settings, nothing is hidden or nothing matches, and is memoised on the model instance, the schema's tables and columns, and the privacy exclusions. **Limit:** the descriptions of *visible* tables and columns are prose and are not scanned, so a hand-written description of a visible column that mentions a hidden one reaches the prompt.
2. **Structure: `services/schema_pruning.py: _filter_semantic_to_schema()`** narrows the model to the schema the LLM will see (privacy-filtered and possibly narrowed by relevance). Narrowing by table alone would leak a hidden column's name, description or join into the prompt, so it uses the strict `prune_to_schema` and `edges_in_schema` (no `privacy` argument): hidden columns are dropped, along with references to them (timestamp roles, partition and cluster columns, hierarchy levels) and relationships with a hidden endpoint; a common join naming a table outside the schema is dropped too. An edge dropped although both its tables are visible (a stale hand edit, or a hidden column) is logged once per edge per process, across connections. The result is memoised on the model instance and the narrowed schema's tables and columns, so a repeated question shape costs a lookup instead of a copy per table.
3. **Relevance: `_filter_semantic_by_relevance()`** keeps the tables most relevant to the question.

Un-hiding a column restores its stored annotations with no regeneration.
