# Data Flow: Question to Results

This document traces the complete lifecycle of a chat request — from the user typing a question to receiving query results — through every layer of the system.

> **Code cross-reference:** each step below links to the exact function / class that implements it. File paths are relative to `backend/app/`.

---

## Complete Call Chain (quick reference)

```
routers/chat.py: process_message()
  └─ services/chat_service.py: ChatService.stream_message()
       ├─ pipeline._build_provider()                # decrypt & instantiate LLM provider
       ├─ schema_pruning._resolve_schema()          # schema load + privacy + table embeddings
       │    ├─ UserSchemaCache (DB read)
       │    ├─ adapter.introspect()                  # if cache miss
       │    ├─ _schema_from_dict()                   # deserialise stored JSON
       │    ├─ _apply_privacy_to_schema()            # strip excluded tables/schemas
       │    └─ cache.compute_embedding_async()        # build per-table embeddings
       ├─ schema_pruning._select_relevant_tables()   # schema pruning (cosine similarity + domain boost)
       │    ├─ QueryCache.compute_embedding_async()
       │    └─ QueryCache.cosine_similarity()
       ├─ schema_pruning._filter_semantic_to_schema()  # drop pruned tables from semantic model
       ├─ schema_pruning._filter_semantic_by_relevance() # token-score narrowing + domain token boost
       ├─ pipeline._generate_query()                 # cache → LLM → validate → store
       │    ├─ QueryCache.lookup()                   # exact + semantic similarity lookup
       │    ├─ ExampleLibrary.find_similar_examples() # few-shot: dialect-filtered, similarity ≥ 0.4
       │    ├─ pipeline._compress_prompt()           # 5-level fallback sizing
       │    │    ├─ pipeline._compact_semantic_model() # strips table DDL; keeps metrics/segments/time_exprs
       │    │    └─ PromptBuilder.build_system_prompt()
       │    │         ├─ IntentClassifier.classify()
       │    │         ├─ IntentClassifier.get_intent_prompt_hint()
       │    │         ├─ PromptBuilder._entity_context()       # focused tables + LIMIT + NER entity hints
       │    │         │    ├─ extract_entity_candidates()      # quoted strings + capitalized phrases
       │    │         │    └─ resolve_entities()               # match against column sample_values (privacy-gated)
       │    │         ├─ datasource.get_system_prompt_additions()
       │    │         ├─ datasource.format_schema_for_llm()
       │    │         └─ SemanticFormatter.format_for_prompt()
       │    ├─ pipeline._load_history()              # prior session turns
       │    ├─ provider.generate_response()          # LLM API call (streamed)
       │    ├─ schema_utils._validate_columns_against_schema() # hallucinated table/column guard
       │    ├─ correction._attempt_sql_correction()  # self-correction (up to 2 retries)
       │    ├─ adapter.validate_query()              # read-only + dialect safety
       │    ├─ schema_utils._check_query_complexity() # CROSS JOIN / large-table guard
       │    ├─ correction._attempt_complexity_correction() # self-correction for complexity
       │    └─ QueryCache.store()                    # persist to query_cache
       ├─ execution._execute_auto_query()            # run query on user's DB (auto_execute mode)
       │    ├─ correction._attempt_sql_execution_correction() # runtime error self-correction
       │    └─ correction._attempt_zero_result_correction()   # 0-row result re-examination
       ├─ execution._mask_sensitive_result_columns() # redact excluded/sensitive columns
       ├─ execution._inject_row_filter()             # enforce row-level security filter
       ├─ chat_service._get_or_create_session()      # create/load ChatSession
       ├─ chat_service._save_user_message()          # persist user ChatMessage
       └─ chat_service._save_assistant_message()     # persist assistant ChatMessage
```

SSE events are emitted throughout via `services/sse_utils.py: format_sse_event()` and consumed on the frontend by `useStreamChat.ts`.

---

## Overview

```
User types: "Show me the top 10 customers by revenue"
                        │
                        ▼
    ┌──────────────────────────────────┐
    │  Frontend: POST /api/v1/chat     │
    │  {connection_id, session_id,     │
    │   provider, message, options}    │
    │  → text/event-stream (SSE)       │
    └──────────────┬───────────────────┘
                   │ HTTP — response is text/event-stream (SSE)
                   ▼
    ┌──────────────────────────────────┐
    │  ChatRouter.process_message()    │
    │  returns StreamingResponse       │
    │  → event_generator() async gen  │
    │  → delegates to ChatService      │
    │  → yields typed SSE events as   │
    │    pipeline progresses           │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  1. Load Connection from DB      │
    │  2. Decrypt config (Fernet)      │
    │  3. Load privacy settings        │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  4. Get Schema                   │
    │     • If cached → use it         │
    │     • If not → introspect DB     │
    │       → save to schema_cache     │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  5. Intent Classification        │
    │     intent_classifier.py         │
    │     Pure regex — no LLM call     │
    │     aggregation / trend /        │
    │     ranking / filtering / etc.   │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  6. Schema Pruning               │
    │     (if schema_pruning_enabled)  │
    │     • Embed all table names      │
    │     • Cosine similarity vs       │
    │       question embedding         │
    │     • Keep top-k tables (def 15) │
    │     • Always keep ≥3 tables      │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  7. Load Semantic Model          │
    │     (if exists on connection)    │
    │     Filtered to pruned tables    │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  8. Query Cache Lookup           │
    │     a. Normalize question        │
    │     b. Try exact match           │
    │     c. Try semantic similarity   │
    │        (cosine ≥ 0.87 threshold) │
    │     Temporal queries bypass cache│
    │                                  │
    │  HIT ───────────────────────────►│ → skip to step 13
    │  MISS ──────────────────────────►│
    └──────────────┬───────────────────┘
                   │ (cache miss)
                   ▼
    ┌──────────────────────────────────┐
    │  9. Find Few-Shot Examples       │
    │     • Cosine similarity vs       │
    │       verified_examples table    │
    │     • Filter by query_dialect    │
    │       (no cross-dialect mixing)  │
    │     • Drop score < 0.4           │
    │       (irrelevant examples hurt) │
    │     • Return top-3 remaining     │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  10. Build & Compress Prompt     │
    │      PromptBuilder assembles:    │
    │      a. Base instructions        │
    │      b. Intent pattern hint      │
    │      c. Query focus (tables,     │
    │         LIMIT, numeric filters)  │
    │      d. Dialect-specific hints   │
    │      e. Schema DDL (privacy +    │
    │         pruning filtered)        │
    │      f. Semantic context         │
    │      g. Few-shot examples        │
    │      h. Output format rules      │
    │         (REASONING → QUERY →     │
    │          EXPLANATION)            │
    │      i. Safety rules             │
    │                                  │
    │      _compress_prompt() tries    │
    │      4 fallback levels if over   │
    │      token budget:               │
    │      full → no few-shot →        │
    │      no semantic → minimal schema│
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  11. Load Conversation History   │
    │      (prior turns in session)    │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  12. Call LLM Provider (SSE)     │
    │      • Build provider instance   │
    │        (decrypt API key)         │
    │      • Send system prompt +      │
    │        history + question        │
    │      • Parse response:           │
    │        REASONING: (skipped)      │
    │        QUERY: ```sql ... ```     │
    │        EXPLANATION: ...          │
    │      • Store in query cache      │
    │      • Yield SSE phase events    │
    │        as pipeline progresses    │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  13. Validate Query              │
    │      BaseSQLValidator:           │
    │      • Only SELECT / WITH        │
    │      • No blocked keywords       │
    │      • No dangerous patterns     │
    │      • Add LIMIT if missing      │
    │      Dialect-specific validator: │
    │      • PG: no pg_sleep, COPY TO  │
    │      • MySQL: no SLEEP, LOAD DATA│
    │                                  │
    │  Invalid → yield error SSE event │
    └──────────────┬───────────────────┘
                   │ (valid)
                   ▼
    ┌──────────────────────────────────┐
    │  14. Apply Execution Mode        │
    │                                  │
    │  generate_only →                 │
    │    status = "query_only"         │
    │    (no execution)                │
    │                                  │
    │  review_first →                  │
    │    status = "pending_approval"   │
    │    (no execution, wait for user) │
    │                                  │
    │  auto_execute →                  │
    │    Connect to user's DB          │
    │    SET statement_timeout         │
    │    Execute query                 │
    │    Collect results               │
    │    status = "executed"/"cached"  │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  15. Persist to PostgreSQL       │
    │      • Create session (if new)   │
    │      • Save user message         │
    │      • Save assistant message    │
    │        (query, results, status,  │
    │         cache_hit, timing)       │
    └──────────────┬───────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  16. Yield final SSE event       │
    │      {session_id, message_id,    │
    │       query, explanation,        │
    │       results, status,           │
    │       cache_hit, error}          │
    │      Stream closes               │
    └──────────────┬───────────────────┘
                   │ SSE stream closes
                   ▼
    useStreamChat.ts processes events,
    updates Zustand store, renders UI
```

---

## Step-by-Step Detail

### SSE Streaming

**File:** `routers/chat.py` → `process_message()` / `event_generator()` (async generator)  
**File:** `services/sse_utils.py` → `format_sse_event()`  
**Frontend:** `src/hooks/useStreamChat.ts`

The chat endpoint returns `StreamingResponse(media_type="text/event-stream")`. The `event_generator()` async generator yields typed SSE events as each pipeline stage completes. The frontend processes these via `useStreamChat.ts`, which maps phase events to UI state updates in Zustand. This means the user sees progress (e.g. "Generating SQL…", "Executing…") rather than waiting for a single response.

### Step 1–3: Connection Loading

**File:** `services/chat_service.py` → `ChatService.stream_message()` / `ChatService.process_message()`  
**File:** `utils/encryption.py` → `decrypt_value()`  
**Model:** `models/connection.py` → `Connection`  
**Dataclass:** `datasources/models.py` → `PrivacySettings`

The frontend sends the connection's UUID. The backend:
1. Loads the `Connection` row from PostgreSQL (org-scoped)
2. Decrypts `config_encrypted` using `decrypt_value()` with the `ENCRYPTION_KEY` (Fernet)
3. Deserializes `privacy_settings` JSON into a `PrivacySettings` dataclass via `PrivacySettings.from_dict()`
4. Instantiates the adapter via `datasources/registry.py` → `create_datasource(source_type, **config)`

### Step 4: Schema Resolution

**File:** `services/schema_pruning.py` → `_resolve_schema()`  
**Model:** `models/user_schema_cache.py` → `UserSchemaCache`  
**Functions:** `_schema_from_dict()`, `_schema_to_dict()`, `_apply_privacy_to_schema()`  
**Adapter method:** `BaseDataSource.introspect(privacy)`

Schema introspection is expensive (multiple SQL queries against the user's database). The schema is cached per-user in `user_schema_caches`. `_resolve_schema()` handles the full lifecycle:

- Cache hit → `_schema_from_dict()` deserialises the stored JSON back to `DataSourceSchema`
- Cache miss → `adapter.connect()` + `adapter.introspect(privacy)` + `_schema_to_dict()` stores the result
- `_apply_privacy_to_schema()` strips excluded schemas/tables before any further use
- If `table_embeddings` are absent, builds them: `_build_table_text(table)` per table → `cache.compute_embedding_async()` → stored on `UserSchemaCache.table_embeddings`

### Step 5: Intent Classification

**File:** `services/intent_classifier.py` → `IntentClassifier`  
**Methods:** `IntentClassifier.classify(question)` → `QueryIntent`, `IntentClassifier.get_intent_prompt_hint(intent)` → `str`

Pure regex matching across 8 intent categories (`TREND`, `RANKING`, `COMPARISON`, `AGGREGATION`, `COUNT`, `SEGMENTATION`, `EXISTENCE`, `LOOKUP`). Zero latency — no LLM, no database. The resulting hint is injected as `## Query Pattern Guidance` early in the system prompt so the LLM frames schema reading around the query type. Each hint includes SQL anti-pattern warnings specific to that intent (e.g. `HAVING` vs `WHERE` for aggregation, `COUNT(*)` vs `COUNT(col)` NULL behaviour for count queries, `GROUP BY` / `SELECT` expression consistency for trend queries).

### Step 6: Schema Pruning

**File:** `services/schema_pruning.py` → `_select_relevant_tables()`  
**Uses:** `QueryCache.compute_embedding_async()`, `QueryCache.cosine_similarity()`

When `schema_pruning_enabled` is true (default), the full schema is narrowed:

1. `cache.compute_embedding_async(question)` — embed the question (384-dim vector via `BAAI/bge-small-en-v1.5`)
2. Per table: `cache.cosine_similarity(q_emb, table_emb)` against stored `UserSchemaCache.table_embeddings`
3. Tables mentioned verbatim in the question are always pinned (not subject to threshold)
4. Tables in `privacy.always_include_tables` are always included
5. **Domain-aware boost** — if ≥1 of the top-5 scored tables has a `domain` tag (`TableSemantic.domain`), the most common domain is identified as dominant; all tables sharing that domain receive a +0.15 cosine score boost (clamped at 1.0) and the list is re-sorted. This keeps business-coherent table groups together even when individual cosine scores vary.
6. Top `schema_pruning_top_k` (default 15) tables above `schema_pruning_threshold` (default 0.30) are kept
7. Falls back to full schema if fewer than 3 tables pass the threshold

### Step 7: Semantic Model Filtering

**File:** `services/schema_pruning.py` → `_filter_semantic_to_schema()`, `_filter_semantic_by_relevance()`  
**Model:** `semantic/models.py` → `SemanticModel`

Two-pass filtering of the stored `Connection.semantic_model`:

1. `_filter_semantic_to_schema()` — drops any table keys not present in the privacy-filtered schema (removes excluded tables/schemas)
2. `_filter_semantic_by_relevance()` — token-score narrowing: scores each table by name overlap + display name + column name tokens against the question; tables whose `domain` tag has tokens overlapping the question receive a +2 score bonus; keeps top-10 plus one-hop FK neighbours

### Step 8: Query Cache Lookup

**File:** `cache/query_cache.py` → `QueryCache.lookup(connection_id, question, db)`  
**Dataclass:** `cache/query_cache.py` → `CacheHit`  
**Pattern detector:** `_has_temporal_reference(question)` (used to bypass semantic similarity)

The `QueryCache` singleton (loaded once at startup via `routers/chat.py: _get_shared_cache()`) performs:

1. `_has_temporal_reference(question)` — temporal questions (e.g. "last 7 days") skip semantic similarity; exact match TTL is capped at 1 day
2. Exact match: `connection_id` + `question.lower().strip()` within `cache_max_age_days` TTL
3. Semantic match (non-temporal only): compute 384-dim embedding, load all cached embeddings for this connection from `query_cache`, compute cosine similarity, return best hit if ≥ `semantic_similarity_threshold` (default 0.87)
4. On hit: server-side `hit_count` increment + `last_hit_at` update (race-safe)
5. Post-hit guard: `_validate_columns_against_schema()` re-checks the cached query — if the cached query references tables/columns the current user cannot see, the hit is discarded and regeneration occurs

### Step 9: Few-Shot Examples

**File:** `cache/example_library.py` → `ExampleLibrary.find_similar_examples(connection_id, question, embedding, db, query_dialect, min_similarity, limit)`  
**Returns:** `list[ExampleEntry]` — question + query pairs from `verified_examples` table

Uses the question embedding (reused from step 8) to find the most relevant thumbs-up approved examples. Three filters are applied in order:

1. **Dialect match** — only examples whose `query_dialect` matches the current connection's dialect are considered (prevents MySQL syntax appearing in PostgreSQL prompts and vice versa)
2. **Similarity threshold** — examples scoring below `min_similarity` (default 0.4 cosine) are discarded; a low-scoring example actively degrades generation accuracy
3. **Top-k** — up to 3 highest-scoring examples are returned

The resulting examples are injected into the prompt under `## Example Queries`.

### Step 10: Prompt Construction & Compression

**File:** `services/prompt_builder.py` → `PromptBuilder.build_system_prompt()`  
**File:** `services/pipeline.py` → `_compress_prompt()`

`PromptBuilder.build_system_prompt()` assembles up to nine sections in order:

| # | Section | Source |
|---|---|---|
| 1 | Base instructions | `PromptBuilder._base_instructions(datasource)` |
| 2 | Query pattern guidance | `IntentClassifier.get_intent_prompt_hint(intent)` — intent-specific SQL pattern + anti-pattern hints |
| 3 | Query focus | `PromptBuilder._entity_context(user_question, schema, privacy)` — pruned table names, user-stated LIMIT, extracted numeric conditions; **Named Entity Resolution (NER)**: quoted strings and capitalised multi-word phrases are extracted from the question (`extract_entity_candidates()`), matched case-insensitively against column `sample_values` (`resolve_entities()`), and surfaced as filter hints — only when `privacy.include_sample_values` is enabled |
| 4 | Dialect additions | `datasource.get_system_prompt_additions()` |
| 5 | Schema DDL | `datasource.format_schema_for_llm(schema, privacy)` |
| 6 | Business context | `SemanticFormatter.format_for_prompt(semantic_model, include_time_exprs)` |
| 7 | Example queries | `ExampleLibrary` entries rendered inline |
| 8 | Output format | `PromptBuilder._output_format_instructions()` — instructs the LLM to emit `REASONING:` (tables/joins/filters/columns) then `QUERY:` then `EXPLANATION:` |
| 9 | Safety rules | `PromptBuilder._safety_rules()` |

`_compress_prompt()` retries if the assembled prompt + question exceeds the token budget (`budget_chars`), using 5 progressive fallback levels:

1. Full (few-shot + full semantic model + full privacy)
2. No few-shot examples
3. No few-shot + compact semantic model — `_compact_semantic_model()` strips per-table DDL, relationships, common joins, and derived columns while preserving business metrics, segments, and time expressions
4. No few-shot, no semantic model
5. No few-shot, no semantic model, minimal schema (no sample values / comments / row counts — `PrivacySettings` overridden in-memory)

### Step 11: Conversation History

**File:** `services/pipeline.py` → `_load_history(session_id, db)`  
**Model:** `models/chat.py` → `ChatMessage`

Loads the most recent `_HISTORY_TURN_LIMIT` (20) turns. Assistant turns are reconstructed in `QUERY:\n\`\`\`sql\n...\n\`\`\`\nEXPLANATION:...` format so the LLM recognises the conversation pattern. Returned as `list[dict]` with `role` / `content` keys for the provider API.

### Step 12: LLM Call

**File:** `services/pipeline.py` → `_build_provider(provider_id_or_name, db)`  
**Registry:** `providers/registry.py` → `create_provider(name, **kwargs)`  
**Base class:** `providers/base.py` → `BaseLLMProvider.generate_response()`  
**Response parsing:** `providers/base.py` → `parse_llm_response(raw, model, tokens)`

`_build_provider()` resolves by UUID (looks up `ProviderConfig`) or by `provider_type` name (most recently updated wins), decrypts the API key via `decrypt_value()`, and constructs the provider instance. The provider calls its LLM API and returns `LLMResponse(query, explanation, raw_response, model, tokens_used)`. `parse_llm_response()` extracts the query by slicing from `QUERY:` to `EXPLANATION:`, naturally skipping the preceding `REASONING:` block. Multiple fallback patterns handle partial or malformed responses.

### Step 13: Schema Validation & Self-Correction

**File:** `services/schema_utils.py` → `_validate_columns_against_schema(query, schema)`  
**File:** `services/correction.py` → `_attempt_sql_correction(original_question, failed_query, ...)`  
**File:** `services/schema_utils.py` → `_is_fallback_query(query)` (detects "no schema match" literals)

`_validate_columns_against_schema()` performs structural validation:
- Extracts CTE names to exclude from table-existence checks
- Checks every FROM/JOIN table exists in the schema
- Builds alias map and verifies `alias.column` references

On failure, `_attempt_sql_correction()` feeds the error back to the LLM (up to `_MAX_SELF_CORRECTION_ATTEMPTS` = 2 retries), re-validating after each attempt. Returns `(corrected_query, explanation)` or `(None, "")`.

### Step 14: Safety Validation & Complexity Guard

**Adapter method:** `BaseDataSource.validate_query(query)` → delegates to dialect-specific Validator  
**Base class:** `datasources/validators/base_sql_validator.py` → `BaseSQLValidator`  
**Examples:** `postgresql_validator.py`, `mysql_validator.py`  
**File:** `services/schema_utils.py` → `_check_query_complexity(sql, schema)`  
**File:** `services/correction.py` → `_attempt_complexity_correction(...)`

`BaseSQLValidator` (via `sqlparse`):
- Ensures single statement, type must be `SELECT` or CTE (`WITH`)
- Blocks DML/DDL keywords, dangerous patterns
- Auto-appends `LIMIT {default_row_limit}` if absent

`_check_query_complexity()` rejects CROSS JOIN and large-table full-scans (tables > 1M rows without WHERE). Failed complexity checks trigger `_attempt_complexity_correction()` (same retry loop pattern).

### Step 15: Execution Mode

**Options dataclass** carried from `ChatRequest.options`

| Mode | Action | Status |
|---|---|---|
| `auto_execute` | Execute now → collect results | `executed` or `cached` |
| `review_first` | Return without executing | `pending_approval` |
| `generate_only` | Return without executing | `query_only` |
| Error (invalid) | Don't execute | `error` |

For `auto_execute`:
- `adapter.connect(config_dict)` — creates connection pool
- `adapter.execute_query(query, timeout_ms, max_rows)` — runs query, returns `QueryResult`
- `adapter.disconnect()` — always via `finally`
- On runtime error: `_attempt_sql_execution_correction()` sends error + hint back to LLM (single attempt); hints for known patterns live in `_EXEC_ERROR_HINTS` dict
- **Zero-result detection** — if the query succeeds but returns 0 rows, `_attempt_zero_result_correction()` is called (skipped for `QueryIntent.EXISTENCE` queries and cache hits). It prompts the LLM to identify the most likely cause (wrong filter value, case mismatch, overly restrictive date range, faulty JOIN) and optionally return a corrected query. If a corrected query is returned and passes read-only validation, it is re-executed and replaces the original result. If the LLM confirms 0 rows is correct, its explanation is surfaced to the user.
- `_mask_sensitive_result_columns(response, privacy)` — redacts `[REDACTED]` over sensitive/excluded columns in the result
- `_inject_row_filter(sql, row_filter_sql, dialect)` — wraps query in subquery to enforce row-level security (if configured)

### Step 16: Persistence

**Functions:** `_get_or_create_session()`, `_save_user_message()`, `_save_assistant_message()`  
**Models:** `models/chat.py` → `ChatSession`, `ChatMessage`

All interactions are stored in PostgreSQL:
- `_get_or_create_session()` — loads existing `ChatSession` (org+user scoped) or creates a new one
- `_save_user_message()` — persists user `ChatMessage` with `role="user"`
- `_save_assistant_message()` — persists assistant `ChatMessage` with query, results JSON, status, cache_hit flag, token counts, execution timing, and any error

### Step 17: Final SSE Event & Cache Store

**File:** `cache/query_cache.py` → `QueryCache.store(connection_id, question, query, dialect, embedding, db)`

On success, `cache.store()` persists the `question → query` pair to `query_cache` (with the pre-computed embedding). The final SSE `DoneEvent` carries the full `ChatResponse` payload: session ID, message ID, query, explanation, results, status, cache_hit, and any error. `useStreamChat.ts` processes each event type and updates the Zustand message store.

---

## Feedback Flow (Thumbs Up/Down)

```
User clicks 👍 on message {id}
            │
            ▼
POST /api/v1/chat/feedback/{message_id}
{"feedback": "thumbs_up"}
            │
            ▼
ChatService.submit_feedback()
  1. Load assistant message
  2. Load preceding user message (the question)
  3. Compute embedding for the question
  4. Add to verified_examples table
     → used as few-shot example in future prompts
```

```
User clicks 👎 on message {id}
            │
            ▼
POST /api/v1/chat/feedback/{message_id}
{"feedback": "thumbs_down"}
            │
            ▼
ChatService.submit_feedback()
  1. Load the question text
  2. DELETE FROM query_cache
     WHERE connection_id = ? AND question_normalized = ?
     → prevents the bad query from being served from cache again
```

---

## Schema Refresh Flow

```
User clicks "Refresh Schema" for connection {id}
                        │
                        ▼
POST /api/v1/connections/{id}/schema/refresh
                        │
                        ▼
ConnectionsRouter.refresh_schema()
  1. Decrypt connection credentials
  2. Load privacy settings
  3. Create adapter instance
  4. adapter.connect() — connects to user's DB
  5. adapter.introspect(privacy) — 8 SQL queries:
     • schemas
     • tables & views
     • columns (with types)
     • primary keys
     • foreign keys
     • column comments (if privacy.include_column_comments)
     • row counts (if privacy.include_row_counts)
     • sample values (if privacy.include_sample_values, non-sensitive only)
  6. adapter.disconnect()
  7. Upsert user_schema_caches SET schema_cache = ?, schema_cached_at = ?
     (keyed by connection_id + user_id)
  8. DELETE FROM query_cache WHERE connection_id = ?
     (invalidate — cached queries may reference old columns)
  9. Return schema as JSON
```
