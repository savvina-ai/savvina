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
- `version` — optimistic lock, checked only for table-row edits: `UPDATE … WHERE id = :id AND version = :version`, and a row that moved on makes the store raise `SemanticStoreConflict`, which the router returns as `409`. Column rows and the rewritten list sections carry a `version` but are not checked against it; they rely on the connection row lock that the PUT, DELETE, suggestion-apply and generation routes hold.
- `updated_at` and `updated_by`.

`semantic_model_history` is written by the store on every change, not by a database trigger: table and column edits per row, a changed `notes` or `is_user_reviewed` as one `model` row, and each rewritten list section (metrics, segments, derived columns, joins, relationships) as one row. Generation writes history too, with source `generation`: `begin_generation` writes one `model` row; `reconcile_tables` writes one row per table or column that actually changed; the globals finalisation (`save_model`) writes one `model` row only, with no per-row entries for pruned tables or replaced metrics; `delete_model` writes one `model` delete row. An edit that echoes what is stored writes nothing. The history table has no foreign key to the connection, so the audit trail survives deleting the connection.

`semantic/store.py` is the only module that reads or writes these tables; `tests/test_semantic/test_blob_is_unread.py` proves nothing in `app/` reads the legacy `connections.semantic_model` column.

**Read order.** Metrics, relationships, segments, derived columns and common joins carry an `ordinal` and are read back in that order. Tables and columns are read in alphabetical key order, not the generator's order.

**Values are fitted to PostgreSQL's limits** (`_fit_values`) before they are compared or stored. NUL characters are stripped (also inside JSON values), and a bounded text value longer than its column is truncated with a logged warning. Identity values (table key, column name, metric or item name, relationship endpoints) are never truncated: one that does not fit raises `ValueError`, which the routes return as `400`. A segment, derived-column or common-join name is stored as unbounded text but bounded by the unique index it sits in (2000 UTF-8 bytes, `store._MAX_INDEXED_TEXT_BYTES`; a common join's name is its `join_pattern`); the same cap applies in the `0003` backfill, which skips such an item. The globals route drops a generated metric, segment, derived column or join whose name the store would reject (`store.name_rejection`) rather than fail the whole finalisation.

**Migrations.** `0002` creates the seven tables. `0003` backfills them from each connection's `semantic_model` blob:

- Each connection is backfilled inside its own SAVEPOINT, so one malformed blob (or a NUL inside a JSON payload) fails only that connection; it is logged and skipped, and the upgrade continues. A final summary line reports `N/M connections backfilled; failed: …` (logged as an error when any failed).
- `user_edited_fields` is seeded empty: nothing in a blob distinguishes LLM output from a hand edit, so the first regenerate treats every migrated value as regenerable.
- It refuses to run in offline `--sql` mode, because the backfill reads data and cannot be rendered as static SQL.
- `connections.semantic_model` is left in place and unread, and is dropped by a later migration (one release on). `DELETE /semantic` and deleting a connection both clear it, so that later migration finds no connection with a blob but no `semantic_models` row.
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
  │    └─ _apply_privacy_to_schema()                      # strip excluded tables/schemas
  ├─ SemanticModelGenerator.prepare_generation(schema)
  │    └─ _build_relationship_graph(schema)               # FK edges from DataSourceSchema.relationships
  ├─ _lock_generation()                                   # connection row lock (FOR UPDATE)
  └─ store.pending_table_keys() / store.begin_generation() # work list + header stamp; no table/column row touched

routers/semantic.py: generate_semantic_batch(body.table_keys)  (one call per batch)
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)                      # instantiate LLM provider
  ├─ SemanticModelGenerator.generate_tables(schema, provider, model, table_keys, edges)
  │    ├─ _schema_to_ddl(batch_schema_obj)                # compact CREATE TABLE DDL string
  │    ├─ _format_relationships_for_prompt(batch_edges)   # FK list for prompt
  │    ├─ provider.generate_structured(                   # LLM JSON call → TablesBatchResponse
  │    │    schema_type=TablesBatchResponse, ...)
  │    ├─ _ground_tables_to_schema(tables, batch_schema)  # remap near-miss keys, drop invented ones
  │    └─ _enrich_fiscal_table_annotations(tables, schema) # stamp audit timestamp descriptions
  ├─ _lock_generation()                                   # connection row lock; re-check status and run id
  └─ store.reconcile_tables() + store.advance_progress()  # upsert skipping user-edited fields; recount progress

routers/semantic.py: generate_semantic_globals()
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)
  └─ SemanticModelGenerator.generate_globals(schema, provider, model, all_tables, edges)
       ├─ _build_tables_summary(schema, all_tables)       # compact table+column+hint summary
       ├─ _format_relationships_for_prompt(pruned_edges)
       ├─ provider.generate_structured(                   # LLM JSON call → GlobalSectionsResponse
       │    schema_type=GlobalSectionsResponse, ...)
       ├─ _detect_ratio_pairs(schema)                     # auto-detect num/denom column pairs
       └─ _validate_metrics_time_columns(metrics, schema) # warn if time patterns on fiscal tables
  └─ (router assembles final SemanticModel):
       ├─ prune_to_schema() / edges_in_schema()           # drop what left the database, keep what privacy hides
       ├─ gen.build_time_expressions(dialect)             # dialect-specific SQL time snippets
       ├─ gen.compute_schema_hash(schema)                 # SHA-256 for drift detection
       ├─ gen._auto_generate_notes(schema)                # fiscal-period table guidance notes
       └─ store.save_model(..., provenance=...)           # full row rebuild, per-field provenance restored
```

---

## Step-by-Step Detail

### Phase 0 — Schema Loading

**Router:** `routers/semantic.py` → `_load_schema_for_connection(connection_id, current_user, db)`  
**Functions:** `_schema_from_dict()`, `_apply_privacy_to_schema()` (from `services/schema_utils.py`)  
**Model:** `models/user_schema_cache.py` → `UserSchemaCache`

All three phases begin the same way: load the schema JSON from `UserSchemaCache` for this user + connection and deserialise it. If no cached schema exists, the schema is introspected live and cached; a failure there returns HTTP 400. Privacy settings are applied via `_apply_privacy_to_schema()` to strip excluded schemas/tables before any LLM call.

### Phase 1 — Init: Batch Plan

**Endpoint:** `POST /{connection_id}/semantic/generate/init`  
**Class:** `semantic/generator.py` → `SemanticModelGenerator`  
**Method:** `SemanticModelGenerator.prepare_generation(schema)` → `dict`

`prepare_generation()` computes — without any LLM call — the table total and the relationship graph:
- `_build_relationship_graph(schema)` reads `DataSourceSchema.relationships` (already populated by `introspect()` from `information_schema`) to produce `list[RelationshipEdge]`. Zero DB queries.
- Batch size is `GENERATION_BATCH_SIZE = 4`.

The router takes the connection row lock (`FOR UPDATE`) and stamps the header row (`store.begin_generation`): `generation_status = "tables_partial"`, `generation_progress = {tables_done, tables_total, batch_size}`, `schema_hash` from the cached schema, and a fresh `generation_run_id` (kept, not minted, on `?resume=true`). **Nothing is blanked** — init is never destructive and touches no table or column row; a run reconciles onto the rows that already exist.

`GenerateInitResponse` carries `tables_total`, `batch_count`, `batch_size`, `resumed` and `pending_table_keys`, the exact work list: every table on a fresh run, or only the tables the interrupted run had not stamped on a resume (`store.pending_table_keys` compares each table row's `generation_run_id` with the header's). The frontend slices that list into batches itself; the server keeps nothing positional. `batch_count` is the number of batches the list makes (at least 1). A resume is refused with `400` when there is no `tables_partial` model and with `409` when the schema hash no longer matches.

An interrupted run (a provider rate limit, a timeout, a closed tab) leaves the model `tables_partial`, and the page shows a banner with three actions, in order of how much they keep:

| Action | What it does |
|---|---|
| **Resume** | `init?resume=true`, then only the pending tables, then Phase 3 |
| **Finish with what's generated** | Phase 3 alone over the tables already stored. Disabled when no tables exist, since the globals call would have nothing to work from |
| **Restart from scratch** | After a confirmation dialog: `DELETE /semantic`, then a fresh init. The only action that discards work, hand edits included |

The banner is not shown while a run is in flight, and **Save Changes** is disabled for the duration of any run: a draft saved mid-run would overwrite the regenerated values and mark them user-edited. A failed run discards the page's local draft and refetches the model, so a stale draft cannot be saved over what the run already wrote.

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

The batch is named by `{"table_keys": [...]}` in the body (never by position); an unknown key is a `400`. Its output goes through `store.reconcile_tables`: an existing row takes the new value on every field not in its `user_edited_fields`, a missing row is inserted, `is_sensitive` is never cleared by generation, nothing is ever deleted, and every named table is stamped with the run id. A table the LLM returned no annotation for is not stamped, so it stays pending and a resume retries it. `generation_progress.tables_done` is recomputed from the stamped rows. The response (`GenerateBatchResponse`: `connection_id`, `table_keys`, `tables_done`, `tables_total`, `batch_size`) is progress only, not the model.

Before the LLM call the route answers `400` when the model is not `tables_partial` (init was not called) or carries no run id (a blob-era interrupted run backfilled by migration 0003; `/generate/init`, with or without `resume=true`, mints one), `409` when the schema hash no longer matches the one init stamped, and `400` for an unknown table key. It then takes the connection row lock after the LLM call and re-checks state before writing, answering `409` and discarding the batch when the status is no longer `tables_partial` (globals or a delete landed meanwhile), or when a fresh init restarted the run while the batch was in flight (stamping those tables with the new run id would make a later resume skip tables it never generated).

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

### Phase 3 Finalisation (router)

After `generate_globals()` returns, `routers/semantic.py: generate_semantic_globals()` re-reads the model under the connection row lock (`409` if it was deleted or its status moved on meanwhile) and assembles the final model:

- **Hand-authored items are carried through.** A metric, segment, derived column or common join with provenance replaces any generated item of the same name (or, for joins, the same `join_pattern`); the generator's set is added around it. A generated item whose name the store would reject (`store.name_rejection`: a metric name over 512 characters, or a segment, derived-column or join name over the index's byte limit) is dropped with a warning.
- **Pruning.** `prune_to_schema` drops tables and columns that left the database and clears references to them (primary timestamp/date column, partition and cluster columns, hierarchy levels; a hierarchy left with no levels is dropped). `edges_in_schema` drops relationships whose endpoints left the database, bounded by the live schema rather than the annotated tables, so a hand-written edge to a real but unannotated table survives. `carry_edited_relationships` then keeps hand-edited relationships through the regenerated set. **What the connection's privacy settings hide is kept, not pruned**: an excluded table, or a column in `excluded_columns`, is absent from the schema because it is hidden rather than gone, so it is carried through together with references to it. `default_filters` are free SQL and are not rewritten.
- Near-miss spellings in stored keys (for example rows migrated from a blob with an LLM's `public.Orders`) are dropped like any absent key, with a warning naming the key they would have matched, because a remap would keep the value but lose the user-edit marks keyed by the old spelling.

| Method | What it produces |
|---|---|
| `gen.build_time_expressions(dialect)` | Dict of `label → SQL expr` for common time periods (`this_month`, `rolling_30`, etc.); dialect-specific (PostgreSQL vs MySQL) |
| `gen.compute_schema_hash(schema)` | SHA-256 of sorted table+column structure for cheap drift detection later |
| `gen._auto_generate_notes(schema)` | Cross-table domain notes for fiscal-period tables — warns that `fiscal_year`/`fiscal_quarter` are the correct time axes, not `created_at` |

The final model is written through `store.save_model` (a full row rebuild that restores per-field provenance and keeps user-authored metrics, segments, derived columns, joins and relationships) with `generation_status = "complete"`, `generation_progress = None`, `generation_run_id = None` and `is_user_reviewed = False` (review state is reset on purpose). The connection's query cache, schema cache and table-embedding cache are purged (once, here — not per batch) and the response is the model as stored, which can differ from what was assembled because the store fits values and de-duplicates name-keyed items.

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
| `DELETE /{id}/semantic` | `routers/semantic.py: delete_semantic_model()` | Deletes every semantic row (`store.delete_model`) and clears the legacy `connections.semantic_model` blob |

`apply_user_patch` marks every field a person changes in that row's `user_edited_fields`, which is what makes the next regenerate skip it. The frontend PUTs the whole draft on every save, so each incoming value is compared with what is stored and only a field that actually differs is written, marked and audited. In the patch, a `null` top-level section is "not supplied" (the stored value is kept) and `[]` is an explicit clear. Inside a table, `null` also means "not supplied" for `display_name` and `default_filters`, and inside a column for `display_name`, `value_mappings`, `is_sensitive`, `is_non_additive` and `semantic_type`; for every other nullable field (`description`, `grain`, `currency`, …) `null` clears the value. Metrics, joins, segments, derived columns and relationships are replaced as whole sections when supplied, but provenance is per item: only an item that is new or whose payload changed is marked. Applying a suggestion (`POST …/suggestions/{id}/apply`) goes through the same patch path, so it is also remembered as a hand edit.

### Regenerate and hand edits

A regenerate refreshes the rest of the model but leaves hand-edited fields alone: `reconcile_tables` skips each field in `user_edited_fields`, and the globals step carries hand-authored metrics, segments, derived columns, joins and relationships through. Only **Restart from scratch** discards them.

---

## Chat Path

`services/chat_service.py` loads the model with `semantic_store.load_model`; a failed read costs that turn its business context, not the turn itself. `services/schema_pruning.py: _filter_semantic_to_schema()` then narrows it to the privacy-filtered schema the LLM will see. Because generation keeps annotations for columns the privacy settings hide, narrowing by table alone would leak a hidden column's name, description or join into the prompt. It therefore uses the strict `prune_to_schema` and `edges_in_schema` (no `privacy` argument): hidden columns are dropped, along with references to them (timestamp roles, partition and cluster columns, hierarchy levels) and relationships with a hidden endpoint. Un-hiding a column restores its stored annotations with no regeneration.
