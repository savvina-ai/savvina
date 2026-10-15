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

Seven tables: one header row per connection (`semantic_models`), one row per table (`semantic_tables`), column (`semantic_columns`), metric (`semantic_metrics`) and relationship (`semantic_relationships`), one row per global item (`semantic_global_items`: segment, derived column or common join), and the append-only `semantic_model_history`. Every row except history carries:

- `user_edited_fields` — per-field provenance: the names of the fields a person has written. A regenerate refreshes every field not listed and leaves the listed ones alone.
- `version` — optimistic lock, checked only for table-row edits: `UPDATE … WHERE id = :id AND version = :version`, and a row that moved on makes the store raise `SemanticStoreConflict`, which the router returns as `409`. Column rows and the rewritten list sections carry a `version` but are not checked against it; they rely on the connection row lock that the PUT, DELETE, suggestion-apply and generation routes hold. The header row's `version` is additionally bumped by every write that changes content (`save_model`, `begin_generation`, a changing `reconcile_tables`, any non-echo `apply_user_patch`), which is what the chat path's per-process model cache (`store.load_model_cached`, keyed by header id and version) checks on every turn; `advance_progress` bumps nothing, so a cached model may carry a stale progress figure, which chat never reads.
- `updated_at` and `updated_by`.

`semantic_model_history` is written by the store on every change, not by a database trigger: table and column edits per row, a changed `notes` or `is_user_reviewed` as one `model` row, and each rewritten list section (metrics, segments, derived columns, joins, relationships) as one row. Generation writes history too, with source `generation`: `begin_generation` writes one `model` row (an `insert` when it creates the header, otherwise an `update`); `reconcile_tables` writes one `insert` row per table or column row it creates and one `update` row per table or column that actually changed; the globals finalisation (`save_model`) writes one `model` row only, with no per-row entries for pruned tables or replaced metrics; `delete_model` writes one `model` delete row. An edit that echoes what is stored writes nothing. The history table has no foreign key to the connection, so the audit trail survives deleting the connection.

Each history row names its entity twice: `entity_id`, the row the change was made to as it existed then, and `entity_key`. For a table or column change `entity_key` is the table key or `table_key.column_name`, and for a header change it is empty. An edit to metrics, relationships, segments, derived columns or common joins is recorded as one row per section, with the header's id and the section's field name (`business_metrics`, `relationships`, …) as its key. `save_model` is a full replace: it deletes every table, column, metric, relationship and global-item row and inserts the rebuilt set with new ids (only the header keeps its id). Keeping ids would need a per-row reconcile that this path deliberately does not do. So a table or column `entity_id` identifies the row only until the next globals finalisation (or a Restart), and the key that follows a table or column across rebuilds is `(entity_type, entity_key)` within a connection. `entity_key` is fitted like any other value, so a truncated key can collide; that is the accepted limit of the key.

`semantic/store.py` is the only module that reads or writes these tables; `tests/test_semantic/test_blob_is_unread.py` proves nothing in `app/` reads the legacy `connections.semantic_model` column.

**Read order.** Metrics, relationships, segments, derived columns and common joins carry an `ordinal` and are read back in that order. Tables and columns are ordered by Python string order on `table_key` and `column_name` (`store._assemble_model`), not the generator's order and not the database collation, so the order is the same on SQLite and PostgreSQL and on every path (`load_model` and the model `save_model` returns). The formatter renders tables in that order, so it is prompt content: on a PostgreSQL database with a non-C collation the table order in prompts changed once when this replaced a SQL `ORDER BY`.

**Values are fitted to PostgreSQL's limits** (`_fit_values`) before they are compared or stored. NUL characters are stripped (also inside JSON values), and a bounded text value longer than its column is truncated with a logged warning. Identity values (table key, column name, metric or item name, relationship endpoints) are never truncated: one that does not fit raises `ValueError`, which the routes return as `400`. A segment, derived-column or common-join name is stored as unbounded text but bounded by the unique index it sits in (2000 UTF-8 bytes, `store._MAX_INDEXED_TEXT_BYTES`; a common join's name is its `join_pattern`); the same cap applies in the `0003` backfill, which skips such an item. The globals route drops a generated metric, segment, derived column or join whose name the store would reject (`store.name_rejection`) rather than fail the whole finalisation.

**Migrations.** `0002` creates the seven tables. `0003` backfills them from each connection's `semantic_model` blob:

- Each connection is backfilled inside its own SAVEPOINT, so one malformed blob (or a NUL inside a JSON payload) fails only that connection; it is logged and skipped, and the upgrade continues. A final summary line reports `N/M connections backfilled; failed: …` (logged as an error when any failed).
- `user_edited_fields` is seeded empty: nothing in a blob distinguishes LLM output from a hand edit, so the first regenerate treats every migrated value as regenerable.
- It refuses to run in offline `--sql` mode, because the backfill reads data and cannot be rendered as static SQL.
- `connections.semantic_model` is left in place and unread, and is dropped by a later migration (one release on). `store.delete_model` clears it (rows or no rows) and both `DELETE /semantic` and deleting a connection go through that, so the later migration finds no connection with a blob but no `semantic_models` row. `connections.semantic_model_updated_at` is unwritten since the store and no longer served by the connections API; it is dropped by the same migration.
- Downgrading past `0003` is a no-op; roll forward instead.

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
  ├─ _lock_generation()                                   # connection row lock (FOR UPDATE)
  └─ store.pending_table_keys() / store.begin_generation() # work list + header stamp; no table/column row touched

routers/semantic.py: generate_semantic_batch(body.table_keys)  (one call per batch)
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)                      # instantiate LLM provider
  ├─ edges_in_schema(_build_relationship_graph(schema))   # FK edges from DataSourceSchema.relationships
  ├─ _run_llm_phase(...)                                  # 180 s timeout; 504 / 429 / 422 / 500
  │    └─ SemanticModelGenerator.generate_tables(schema, provider, model, table_keys, edges)
  │         ├─ _schema_to_ddl(batch_schema_obj)           # compact CREATE TABLE DDL string
  │         ├─ _format_relationships_for_prompt(batch_edges) # FK list for prompt
  │         ├─ provider.generate_structured(              # LLM JSON call → TablesBatchResponse
  │         │    schema_type=TablesBatchResponse, ...)
  │         ├─ _ground_tables_to_schema(tables, batch_schema) # remap near-miss keys, drop invented ones
  │         └─ _enrich_fiscal_table_annotations(tables, schema) # stamp audit timestamp descriptions
  ├─ _lock_generation()                                   # connection row lock; re-check status and run id
  └─ store.reconcile_tables() + store.advance_progress()  # upsert skipping user-edited fields; recount progress

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
       ├─ drop_dangling_edits(marks, protected, schema, privacy) # hand edits naming gone columns
       ├─ prune_to_schema() / edges_in_schema()           # drop what left the database, keep what privacy hides
       ├─ (warning naming every unannotated table)
       ├─ gen.build_time_expressions(dialect)             # dialect-specific SQL time snippets
       ├─ gen.compute_schema_hash(schema)                 # SHA-256 for drift detection
       ├─ gen._auto_generate_notes(schema)                # fiscal-period table guidance notes
       └─ store.save_model(..., provenance=..., protected=...)  # full row rebuild; returns the stored model
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

The router takes the connection row lock (`FOR UPDATE`) and stamps the header row (`store.begin_generation`): `generation_status = "tables_partial"`, `generation_progress = {tables_done, tables_total, batch_size}`, `schema_hash` from the cached schema, and a fresh `generation_run_id` (kept, not minted, on `?resume=true`). **Nothing is blanked** — init is never destructive and touches no table or column row; a run reconciles onto the rows that already exist.

`GenerateInitResponse` carries `tables_total`, `batch_count`, `batch_size`, `resumed` and `pending_table_keys`, the exact work list: every table on a fresh run, or only the tables the interrupted run had not stamped on a resume (`store.pending_table_keys` compares each table row's `generation_run_id` with the header's). The frontend slices that list into batches itself; the server keeps nothing positional. `batch_count` is the number of batches the list makes (at least 1). A resume is refused with `400` when there is no `tables_partial` model and with `409` when the schema hash no longer matches. A `tables_partial` model backfilled by migration 0003 from a blob-era interrupted run has neither a schema hash nor a run id; `resume=true` on such a model runs a fresh, non-destructive run (every table pending, hand edits kept) and answers `resumed: false`.

An interrupted run (a provider rate limit, a timeout, a closed tab) leaves the model `tables_partial`, and the page shows a banner with three actions, in order of how much they keep:

| Action | What it does |
|---|---|
| **Resume** | `init?resume=true`, then only the pending tables, then Phase 3 |
| **Finish with what's generated** | Phase 3 alone over the tables already stored. Disabled when no tables exist, since the globals call would have nothing to work from |
| **Restart from scratch** | After a confirmation dialog: `DELETE /semantic`, then a fresh init. The only action that discards work, hand edits included |

The banner is not shown while a run is in flight. A run drops the page's local draft when it starts, so starting one — Regenerate, Resume, Finish or Restart — is blocked while the draft has unsaved edits ("Save your changes first — starting a run discards unsaved edits"); applying a suggestion is blocked the same way. For the duration of a run the editors are read-only ("Editing is paused while generation runs."), and **Save Changes** and applying a suggestion are disabled: a draft saved mid-run would overwrite the regenerated values and mark them user-edited. The draft is re-seeded from the run's result, or after a failed run from a refetch of the model, so a stale draft cannot be saved over what the run already wrote.

The page retries a call that is throttled (`429`) or loses its connection, with exponential backoff, up to four attempts; any other failure stops the run, and the remaining batches are not sent. A table a batch reports in `omitted_table_keys` is sent once more, as its own batch; a second omission is left for Phase 3 to report.

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

The batch is named by `{"table_keys": [...]}` in the body (never by position); an unknown key is a `400`. Its output goes through `store.reconcile_tables`: an existing row takes the new value on every field not in its `user_edited_fields`, a missing row is inserted, `is_sensitive` is never cleared by generation, nothing is ever deleted, and every named table is stamped with the run id. A table the LLM returned no annotation for is not stamped, so it stays pending and a resume retries it. `generation_progress.tables_done` is recomputed from the stamped rows. The response (`GenerateBatchResponse`: `connection_id`, `table_keys`, `tables_done`, `tables_total`, `batch_size`, `omitted_table_keys`) is progress only, not the model. `omitted_table_keys` names the tables of the batch the LLM returned nothing for (also logged as a warning). They are not stamped and stay pending; the page re-queues them once, and Phase 3 adds a generation warning naming every table still without an annotation (a **Finish with what's generated** that skips tables always produces one).

Before the LLM call the route answers `400` when the model is not `tables_partial` (init was not called) or carries no run id (a blob-era interrupted run backfilled by migration 0003; `/generate/init`, with or without `resume=true`, mints one), `409` when the schema hash no longer matches the one init stamped, and `400` for an unknown table key. It then takes the connection row lock after the LLM call and re-checks state before writing, answering `409` and discarding the batch when the status is no longer `tables_partial` (globals or a delete landed meanwhile), or when a fresh init restarted the run while the batch was in flight (stamping those tables with the new run id would make a later resume skip tables it never generated).

Both LLM phases run their call through `_run_llm_phase`, under a 180-second timeout. A timeout is `504`, logged as a warning. A provider throttle — the `[RATE_LIMIT]` marker the providers put in the message — is `429`, logged as a warning without a traceback; the per-IP rate limiter also answers `429`, and that status is what the page's retry-with-backoff keys on. Any other `ValueError` (the generator wraps every provider error in one) is `422`, and anything else `500`, both logged with the traceback. Every log line names the provider. The providers built on the OpenAI client (every provider except Claude) report an exhausted balance (a `429` with code `insufficient_quota`) as `[QUOTA_EXCEEDED]`, which is a `422` and is not retried, since waiting does not fix it. Gemini's daily and per-minute quotas cannot be told apart, so both stay `[RATE_LIMIT]` and are retried. The Claude provider maps only a `429` to `[RATE_LIMIT]`: an overloaded `529` or a connection error is a `422` and is not retried.

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

`generate_globals` raises `ValueError` on an LLM failure, like `generate_tables`, so a failed globals call is answered like a failed batch (`422`, `429` on a throttle, `504` on a timeout). Nothing is written, so a model a run left `tables_partial` stays that way, with **Finish with what's generated** and **Resume** available, rather than being completed without metrics.

### Phase 3 Finalisation (router)

After `generate_globals()` returns, `routers/semantic.py: generate_semantic_globals()` takes the connection row lock and reads the model, its provenance marks and the stored values of the marked fields in one pass over the rows (`store.load_model_with_provenance`; `409` if the model was deleted or its status moved on meanwhile, or if a fresh init restarted the run while the LLM call was in flight — the header is `tables_partial` with a run id other than the one read before the call; a re-finalise of a `complete` model has no run id before or after and is not affected), then assembles the final model:

- **Hand-authored items are carried through.** A metric, segment, derived column or common join with provenance replaces any generated item of the same name (or, for joins, the same `join_pattern`); the generator's set is added around it. A generated item whose name the store would reject (`store.name_rejection`: a metric name over 512 characters, or a segment, derived-column or join name over the index's byte limit) is dropped with a warning. Only what exists is carried: deleting a generated item (a metric, segment, derived column, join or foreign-key relationship) leaves no provenance mark, so the generator's set may add it back; renaming one carries the renamed copy, and the original name may come back beside it. Remembering deletions needs tombstones, which the store does not keep.
- **Pruning.** `prune_to_schema` drops tables and columns that left the database and clears references to them (primary timestamp/date column, partition and cluster columns, hierarchy levels; a hierarchy left with no levels is dropped). `edges_in_schema` drops relationships whose endpoints left the database, bounded by the live schema rather than the annotated tables, so a hand-written edge to a real but unannotated table survives. `carry_edited_relationships` then keeps hand-edited relationships through the regenerated set. **What the connection's privacy settings hide is kept, not pruned**: an excluded table, or a column in `excluded_columns`, is absent from the schema because it is hidden rather than gone, so it is carried through together with references to it. For relationships only the edges a user has marked (edited) are carried through, by `carry_edited_relationships`; an unmarked foreign-key edge to a privacy-hidden endpoint is not regenerated while the endpoint is hidden (the fresh edges come from the narrowed schema) and returns on the next globals run after it is un-hidden. **So is a table the connection's role cannot read**: `information_schema` lists only what the role holds a privilege on, so the PostgreSQL adapter also reads `pg_class` and records each table that exists but is not visible in `DataSourceSchema.unreadable_tables`. Globals passes `keep_unreadable=True` to `prune_to_schema` and `edges_in_schema`, so connecting with a narrower role cannot delete those annotations; the LLM-prompt calls leave the flag off, since nothing over such a table could run. The MySQL adapter cannot tell an unreadable table from a dropped one and leaves the list empty. `default_filters` are free SQL and are not rewritten.
- **Dangling hand edits.** Before the save, `drop_dangling_edits` removes from the hand edits the names of columns the table no longer has (a column the privacy settings hide is not gone; a table absent from the schema is left to `prune_to_schema`). Partition and cluster columns and hierarchy levels keep their live entries and their edit mark (a hierarchy left with no levels is dropped). A primary timestamp or date column naming a gone column, or a list left empty, is cleared and unmarked, so the pruned generated value wins and a later regenerate may fill it again. Each changed field adds a note to the model's `generation_warnings` and is logged.
- **Unannotated tables.** A table of the schema with no stored row after pruning is one the LLM returned nothing for (twice, after the page's re-queue), one a **Finish with what's generated** never reached, or one created since the run started. The model is completed regardless, with one warning naming them: "N table(s) have no annotation (…): the LLM returned nothing for them, the run was finished before reaching them, or they are new since the run started. Regenerate to annotate them." (up to five keys are listed).
- Near-miss spellings in stored keys (for example rows migrated from a blob with an LLM's `public.Orders`) are dropped like any absent key, with a warning naming the key they would have matched, because a remap would keep the value but lose the user-edit marks keyed by the old spelling.

| Method | What it produces |
|---|---|
| `gen.build_time_expressions(dialect)` | Dict of `label → SQL expr` for common time periods (`this_month`, `rolling_30`, etc.); dialect-specific (PostgreSQL vs MySQL) |
| `gen.compute_schema_hash(schema)` | SHA-256 of sorted table+column structure for cheap drift detection later |
| `gen._auto_generate_notes(schema)` | Cross-table domain notes for fiscal-period tables — warns that `fiscal_year`/`fiscal_quarter` are the correct time axes, not `created_at` |

The final model is written through `store.save_model` (a full row rebuild that restores per-field provenance and keeps user-authored metrics, segments, derived columns, joins and relationships) with `generation_status = "complete"`, `generation_progress = None`, `generation_run_id = None` and `is_user_reviewed = False` (review state is reset on purpose). The model's `generation_warnings` are the generator's warnings, then the dangling-edit notes, then the unannotated-table warning. The connection's query cache, schema cache and table-embedding cache are purged (once, here — not per batch) and the response is the model `save_model` returns, which is what a later `GET` returns. It can differ from what was assembled because the store fits values and de-duplicates name-keyed items.

Because regenerating never blanks the model and the community edition has no apply-drift, this is the step where tables and columns that left the database are removed.

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

**Method:** `SemanticModelGenerator.detect_drift(current_schema, stored_model)` → `list[str]`  
**Endpoint:** `GET /{connection_id}/semantic/drift`

Purely structural comparison — no LLM, no DB re-query:
- Tables removed from the DB since generation → warning per missing table
- Columns removed → warning per missing column
- Tables added since generation → "N new tables added" summary
- Metric references to now-absent tables → warning per broken metric

The route passes the connection's privacy settings, so a table or column the policy hides is not reported as removed, and a metric naming a hidden table is not reported as broken — including a bare table name under an excluded schema, which is resolved through the qualified keys the stored model holds. A stored table in the schema's `unreadable_tables` is not reported as removed either; one warning names those tables instead, because a missing grant is not something a regenerate can fix.

Returns human-readable warning strings; the frontend displays them as a `DriftReport`.

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
