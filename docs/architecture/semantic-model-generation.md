# Semantic Model Generation

This document traces the complete lifecycle of auto-generating a `SemanticModel` — from triggering generation via the API through to the final model stored on the `Connection` row.

> **Code cross-reference:** all file paths are relative to `backend/app/`.

---

## What is a SemanticModel?

**File:** `semantic/models.py`

`SemanticModel` is a Pydantic model (stored as JSON on `connections.semantic_model`) that enriches the raw database schema with business meaning. It is injected into the LLM system prompt via `SemanticFormatter` on every chat turn.

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

## Two Generation Paths

### Path A — Phased (used by the frontend)

The frontend drives three sequential API calls. Each call commits partial results to the DB so the user sees incremental progress and the browser can refresh without losing work.

```
POST /api/v1/connections/{id}/semantic/generate/init
POST /api/v1/connections/{id}/semantic/generate/batch?batch_idx=N  (× batch_count)
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
  └─ SemanticModelGenerator.prepare_generation(schema)
       └─ _build_relationship_graph(schema)               # FK edges from DataSourceSchema.relationships

routers/semantic.py: generate_semantic_batch(batch_idx)  (× batch_count)
  ├─ _load_schema_for_connection()
  ├─ _resolve_provider(provider, db)                      # instantiate LLM provider
  └─ SemanticModelGenerator.generate_table_batch(schema, provider, model, batch_idx, edges)
       ├─ _schema_to_ddl(batch_schema_obj)                # compact CREATE TABLE DDL string
       ├─ _format_relationships_for_prompt(batch_edges)   # FK list for prompt
       ├─ provider.generate_structured(                   # LLM JSON call → TablesBatchResponse
       │    schema_type=TablesBatchResponse, ...)
       └─ _enrich_fiscal_table_annotations(tables, schema) # stamp audit timestamp descriptions

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
       ├─ gen.build_time_expressions(dialect)             # dialect-specific SQL time snippets
       ├─ gen.compute_schema_hash(schema)                 # SHA-256 for drift detection
       └─ gen._auto_generate_notes(schema)                # fiscal-period table guidance notes
```

---

## Step-by-Step Detail

### Phase 0 — Schema Loading

**Router:** `routers/semantic.py` → `_load_schema_for_connection(connection_id, current_user, db)`  
**Functions:** `_schema_from_dict()`, `_apply_privacy_to_schema()` (from `services/chat_service.py`)  
**Model:** `models/user_schema_cache.py` → `UserSchemaCache`

All three phases begin the same way: load the schema JSON from `UserSchemaCache` for this user + connection and deserialise it. If no cached schema exists the endpoint returns HTTP 400 — the user must run a schema refresh first. Privacy settings are applied via `_apply_privacy_to_schema()` to strip excluded schemas/tables before any LLM call.

### Phase 1 — Init: Batch Plan

**Endpoint:** `POST /{connection_id}/semantic/generate/init`  
**Class:** `semantic/generator.py` → `SemanticModelGenerator`  
**Method:** `SemanticModelGenerator.prepare_generation(schema)` → `dict`

`prepare_generation()` computes — without any LLM call — how many batches are needed:
- `_build_relationship_graph(schema)` reads `DataSourceSchema.relationships` (already populated by `introspect()` from `information_schema`) to produce `list[RelationshipEdge]`. Zero DB queries.
- Batch count = `ceil(len(schema.tables) / _BATCH_SIZE)` where `_BATCH_SIZE = 4`

The router writes an initial partial `SemanticModel` to DB with `generation_status = "tables_partial"` and `generation_progress = {tables_done: 0, tables_total: N, batch_size: 4}`. Returns `GenerateInitResponse` with the batch count.

### Phase 2 — Batch: Table Annotation

**Endpoint:** `POST /{connection_id}/semantic/generate/batch?batch_idx=N`  
**Method:** `SemanticModelGenerator.generate_table_batch(schema, provider, model, batch_idx, relationship_edges)`

For each batch of up to 4 tables:

1. **`_schema_to_ddl(batch_schema_obj)`** — builds a compact `CREATE TABLE` DDL string for the batch tables. Column lines include native type, NOT NULL, PRIMARY KEY, and inline `--` comments from column descriptions. Sample values are omitted here (this is a semantic DDL, not the full schema). Total DDL capped at `_MAX_DDL_CHARS = 20,000` characters.

2. **`_format_relationships_for_prompt(batch_edges)`** — renders FK edges as `from.col → to.col (JOIN: ...)` lines for the prompt.

3. **`provider.generate_structured(system_prompt, user_message, schema_type=TablesBatchResponse, temperature=0.0)`** — LLM JSON call. The provider is expected to return a `TablesBatchResponse` (a Pydantic model with `tables: dict[str, TableSemantic]`). `temperature=0.0` is used to maximise determinism.

   Prompt template: `_BATCH_TABLES_PROMPT` in `semantic/generator.py` — asks for per-table and per-column: `display_name`, `description`, `grain`, `semantic_type`, `default_aggregation`, `value_mappings`, `is_sensitive`, `currency`, `unit`, `default_filters`, `hierarchies`.

4. **`_enrich_fiscal_table_annotations(result.tables, schema)`** — stamps explicit descriptions on `created_at` / `updated_at` columns of fiscal-period tables (tables with ≥2 `fiscal_year` / `fiscal_quarter`-style columns) to prevent the LLM from treating them as business time axes in future queries.

The merged table dict is written back to the partial `SemanticModel` on `Connection.semantic_model` and `generation_progress.tables_done` is incremented.

### Phase 3 — Globals: Cross-Table Sections

**Endpoint:** `POST /{connection_id}/semantic/generate/globals`  
**Method:** `SemanticModelGenerator.generate_globals(schema, provider, model, all_tables, relationship_edges)`

Generates cross-table business KPIs, derived columns, join patterns, and named segments in a single LLM call:

1. **`_build_tables_summary(schema, all_tables)`** — compact one-line-per-table summary: `schema.table (col:type, ...) [primary_date=col] [primary_ts=col]`. Including types lets the LLM distinguish fiscal integer dimensions from timestamp audit columns.

2. **`_format_relationships_for_prompt(pruned_edges)`** — FK edges pruned to only tables that were actually generated.

3. **`provider.generate_structured(schema_type=GlobalSectionsResponse, temperature=0.0)`** — LLM JSON call. Returns `GlobalSectionsResponse` with:
   - `business_metrics: list[BusinessMetric]` — KPIs with SQL definitions and `metric_type` discrimination (`simple`, `ratio`, `cumulative`, `conversion`, `derived`)
   - `derived_columns: list[DerivedColumn]` — calculated expressions (margins, rates)
   - `common_joins: list[CommonJoin]` — reusable JOIN patterns
   - `segments: list[Segment]` — reusable WHERE-clause fragments

   Prompt template: `_GLOBAL_SECTIONS_PROMPT` in `semantic/generator.py`.

4. **`_detect_ratio_pairs(schema)`** — purely structural scan for column name pairs matching `_RATIO_NUMERATOR_KEYWORDS` (spent, actual, cost, …) vs `_RATIO_DENOMINATOR_KEYWORDS` (budget, limit, quota, …). Auto-generates `DerivedColumn` hints for utilisation ratios not already covered by LLM output.

5. **`_validate_metrics_time_columns(metrics, schema)`** — scans metric definitions for time-series SQL patterns (`date_trunc`, `interval`, `lag`, etc.) applied to fiscal-period tables; emits `generation_warnings` strings.

### Phase 3 Finalisation (router)

After `generate_globals()` returns, `routers/semantic.py: generate_semantic_globals()` assembles the final model:

| Method | What it produces |
|---|---|
| `gen.build_time_expressions(dialect)` | Dict of `label → SQL expr` for common time periods (`this_month`, `rolling_30`, etc.); dialect-specific (PostgreSQL vs MySQL) |
| `gen.compute_schema_hash(schema)` | SHA-256 of sorted table+column structure for cheap drift detection later |
| `gen._auto_generate_notes(schema)` | Cross-table domain notes for fiscal-period tables — warns that `fiscal_year`/`fiscal_quarter` are the correct time axes, not `created_at` |

The final model is written to `connections.semantic_model` with `generation_status = "complete"` and `generation_progress = None`.

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
| `PUT /{id}/semantic` | `routers/semantic.py: update_semantic_model()` | Deep-merges user edits into existing model (per-column merge preserves v2 metadata) |
| `DELETE /{id}/semantic` | `routers/semantic.py: delete_semantic_model()` | Clears `connections.semantic_model` (admin only) |
