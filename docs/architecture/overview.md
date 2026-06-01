# Architecture Overview

Savvina AI is built around two core design principles: **adapter pattern everywhere** and **zero-change extensibility**. Every data source and every LLM provider is a plugin — adding a new one requires only a single new Python file with no changes to any existing code.

---

## High-Level Component Map

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Browser (React 18 + TypeScript)                │
│                                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ ChatPage │  │ConnectionPage│  │ SettingsPage │  │SemanticPage │ │
│  └──────────┘  └──────────────┘  └──────────────┘  └─────────────┘ │
│       │              │                  │                  │         │
│  Zustand (appStore) + TanStack Query + Axios API client              │
└─────────────────────────────────────────────────────────────────────-┘
                              │ HTTP REST
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   FastAPI Backend (Python 3.12, async)               │
│                                                                      │
│  ┌────────────┬────────────┬────────────┬───────────┬─────────────┐ │
│  │ /chat      │/connections│ /providers │ /semantic │ /datasources│ │
│  │ router     │ router     │ router     │ router    │ router      │ │
│  └─────┬──────┴─────┬──────┴─────┬──────┴─────┬─────┴─────┬───────┘ │
│        │            │            │            │           │          │
│  ┌─────▼──────┐    │     ┌──────▼──────┐    │           │          │
│  │ChatService │    │     │  Providers  │    │           │          │
│  │            │    │     │  Registry   │    │           │          │
│  │ ┌────────┐ │    │     │ ┌─────────┐ │    │           │          │
│  │ │QueryCch│ │    │     │ │Claude   │ │    │           │          │
│  │ └────────┘ │    │     │ │OpenAI   │ │    │           │          │
│  │ ┌────────┐ │    │     │ │Groq     │ │    │           │          │
│  │ │ExampLib│ │    │     │ │Gemini   │ │    │           │          │
│  │ └────────┘ │    │     │ │Cerebras │ │    │           │          │
│  │ ┌────────┐ │    │     │ │Mistral  │ │    │           │          │
│  │ │Prompt  │ │    │     │ │Ollama   │ │    │           │          │
│  │ │Builder │ │    │     │ │Compat.  │ │    │           │          │
│  │ └────────┘ │    │     │ └─────────┘ │    │           │          │
│  └────────────┘    │     └─────────────┘    │           │          │
│                    │                        │           │          │
│              ┌─────▼──────────────────────┐ │           │          │
│              │   DataSources Registry     │ │           │          │
│              │ ┌──────────────────────┐   │ │           │          │
│              │ │PostgreSQLDataSource  │   │◄┘           │          │
│              │ │  + PGValidator       │   │             │          │
│              │ ├──────────────────────┤   │             │          │
│              │ │MySQLDataSource       │   │             │          │
│              │ │  + MySQLValidator    │   │             │          │
│              │ └──────────────────────┘   │             │          │
│              └───────────────┬────────────┘             │          │
│                              │                          │          │
│              ┌───────────────▼────────────┐             │          │
│              │  SemanticModelGenerator    │◄────────────┘          │
│              └────────────────────────────┘                        │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              PostgreSQL (app database)                        │  │
│  │  connections | chat_sessions | chat_messages | provider_configs│  │
│  │  query_cache | verified_examples | users | refresh_tokens     │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
              │ asyncpg / aiomysql
              ▼
   User's Database (PostgreSQL, MySQL / MariaDB)
```

---

## Frontend

**Stack:** React 18, TypeScript, Tailwind CSS, Zustand, TanStack Query v5, Axios, shadcn/ui, lucide-react

### State Management

The Zustand store (`appStore`) holds:
- `activeConnectionId` — which database the user is currently querying
- `activeSessionId` — current chat session
- `selectedProvider` — UUID of the provider config to use
- `messages` — the current session's message array
- `schema` — the schema data for the active connection

`activeConnectionId` and `selectedProvider` are persisted to `localStorage`. `schema` is not persisted (too large; refetched from the API on page load).

The `savvina-theme` preference is persisted under a separate `localStorage` key by `appStore`'s `toggleTheme` / `initTheme` functions rather than through Zustand's persist middleware, because `initTheme` must apply the theme class to `<html>` synchronously on page load before Zustand hydrates.

### Cross-Tab Token Coordination

`authStore` uses two mechanisms in combination to prevent concurrent token-refresh storms when the app is open in multiple tabs:

1. **`localStorage` TTL lock** (`savvina-refresh-lock`): Before calling the refresh endpoint, a tab writes the current timestamp to this key. Other tabs that are about to refresh check the key first and skip their own call if the timestamp is within the 5-second TTL.

2. **`BroadcastChannel` (`savvina-auth`)**: Immediately after claiming the lock, the tab broadcasts a `REFRESH_STARTED` message. Receiving tabs call `clearTimeout` on their pending proactive-refresh timers, removing them from the queue before they can fire. When the refresh completes, the winning tab broadcasts `TOKEN_REFRESHED` with the new access token; receiving tabs update their in-memory token and re-arm their own proactive-refresh timer.

Together these eliminate the race in the common case (two tabs' timers fire milliseconds apart). A sub-millisecond race remains if two tabs fire in the exact same JavaScript turn before either `REFRESH_STARTED` message arrives; the backend's single-use token reuse-detection is the backstop for that edge case (all sessions are revoked and the user must re-login).

### API Layer

Each resource has a dedicated module under `src/api/`:
- `connections.ts` — CRUD + schema/privacy/execution-mode endpoints
- `chat.ts` — messages, sessions, cache, examples
- `providers.ts` — provider config management
- `semantic.ts` — semantic model generation and updates
- `datasources.ts` — adapter type listing

TanStack Query wraps these for caching, background refetching, and loading states.

### Routing

React Router v6 routes:
- `/` → redirects based on active connection
- `/connect` → ConnectionPage
- `/chat` → ChatPage
- `/settings` → SettingsPage
- `/history` → HistoryPage
- `/semantic/:connectionId` → SemanticModelPage

---

## Backend

**Stack:** Python 3.12, FastAPI 0.131.0, SQLAlchemy 2.0 (async), asyncpg, fastembed (ONNX Runtime), Fernet encryption

### FastAPI Application (`app/main.py`)

The application entry point:
1. Imports all datasource adapters to trigger `@register_datasource` decorators
2. Imports all LLM providers to trigger `@register_provider` decorators
3. Imports all SQLAlchemy models so `Base.metadata` is populated
4. Runs `alembic upgrade head` via `entrypoint.sh` before uvicorn starts (schema migrations)
5. Bootstraps default admin user and cleans up expired tokens on startup
6. Pre-warms the sentence-transformer embedding model on startup
7. Registers all routers under `/api/v1`

### Routers

| Router | Prefix | File |
|---|---|---|
| Auth | `/api/v1/auth` | `routers/auth.py` |
| Datasources | `/api/v1/datasources` | `routers/datasources.py` |
| Connections | `/api/v1/connections` | `routers/connections.py` |
| Semantic | `/api/v1/connections/{id}/semantic` | `routers/semantic.py` |
| Providers | `/api/v1/providers` | `routers/providers.py` |
| Chat | `/api/v1/chat` | `routers/chat.py` |
| Settings | `/api/v1/settings` | `routers/settings.py` |
| Share | `/api/v1/share` | `routers/share.py` |
| Export | `/api/v1/export` | `routers/export.py` |

### ChatService

`ChatService` lives in `services/chat_service.py` (slim orchestrator) and delegates to six focused sub-modules. See [Data Flow](data-flow.md) for the step-by-step breakdown including precise function and class references.

```
services/
├── schema_utils.py    — schema serialization, SQL table extraction, column/complexity validation
├── correction.py      — self-correction loops (schema, complexity, execution, zero-result)
├── schema_pruning.py  — schema pruning, relevance filtering, schema resolution
├── validation.py      — query validation pipeline (_validate_and_correct_query)
├── execution.py       — query execution, result masking, row-filter injection
├── pipeline.py        — LLM provider setup, prompt compression, query generation
└── chat_service.py    — ChatService class, session helpers, message persistence
```

| Symbol | Module | Role |
|---|---|---|
| `ChatService.stream_message()` | `chat_service` | SSE async generator — top-level pipeline orchestrator |
| `ChatService.process_message()` | `chat_service` | Non-streaming version — returns `ChatResponse` directly |
| `_build_provider()` | `pipeline` | Resolves + instantiates an LLM provider from UUID or name |
| `_generate_query()` | `pipeline` | Cache lookup → LLM call → validate → store |
| `_compress_prompt()` | `pipeline` | 5-level progressive prompt compression |
| `_compact_semantic_model()` | `pipeline` | Strips table DDL; keeps business metrics / time expressions |
| `_resolve_schema()` | `schema_pruning` | Loads per-user schema from `UserSchemaCache` or introspects |
| `_select_relevant_tables()` | `schema_pruning` | Schema pruning via cosine similarity + domain-aware score boost |
| `_filter_semantic_to_schema()` | `schema_pruning` | Drops pruned tables from the `SemanticModel` |
| `_filter_semantic_by_relevance()` | `schema_pruning` | Token-score narrowing of semantic model to question |
| `_validate_and_correct_query()` | `validation` | Orchestrates column check + self-correction attempts |
| `_attempt_sql_correction()` | `correction` | Self-corrects schema/column validation errors (up to 2 retries) |
| `_attempt_complexity_correction()` | `correction` | Self-corrects CROSS JOIN / large-table complexity rejections |
| `_attempt_sql_execution_correction()` | `correction` | Self-corrects runtime execution errors |
| `_attempt_zero_result_correction()` | `correction` | Re-examines queries returning 0 rows; finds wrong filter/join |
| `_validate_columns_against_schema()` | `schema_utils` | Cross-references query table/column references against schema |
| `_check_query_complexity()` | `schema_utils` | Rejects CROSS JOIN and large-table full scans |
| `_execute_auto_query()` | `execution` | Runs the generated query and handles results |
| `_mask_sensitive_result_columns()` | `execution` | Redacts sensitive/excluded columns in results |
| `_inject_row_filter()` | `execution` | Enforces mandatory row-level security filter via subquery wrap |
| `_get_or_create_session()` | `chat_service` | Loads or creates `ChatSession` |
| `_save_user_message()` / `_save_assistant_message()` | `chat_service` | Persists `ChatMessage` rows |

### DataSource Adapter System

Abstract base class: `datasources/base.py` — `BaseDataSource`

Every adapter must implement:
- `connect(config)` / `disconnect()`
- `test_connection(config)`
- `introspect(privacy)` — schema discovery
- `get_sample_values(schema, table, column, limit)`
- `execute_query(query, timeout, max_rows)`
- `validate_query(query)` — delegates to a Validator class
- `format_schema_for_llm(schema, privacy)`
- `get_system_prompt_additions()` — dialect-specific LLM instructions
- `get_config_schema()` — JSON schema for the frontend form

Registered adapters: `postgresql`, `mysql` — discovered automatically on import via `@register_datasource` decorator.

### LLM Provider System

Abstract base class: `providers/base.py` — `BaseLLMProvider`

Every provider must implement:
- `generate_response(system_prompt, user_message, conversation_history, model, temperature, max_tokens)`
- `health_check()` → `(bool, str)` — success flag + error detail
- `get_available_models()` → `list[str]`

Registration: `@register_provider("claude")` decorator.

All providers share response parsing logic in `providers/base.py` — `parse_llm_response(raw, model, tokens)` extracts the query by slicing from `QUERY:` to `EXPLANATION:`, skipping the preceding `REASONING:` block (tables/joins/filters/columns). Multiple fallback patterns handle partial responses.

### Semantic Model System

See [Semantic Model Generation](semantic-model-generation.md) for the full call chain, LLM prompts, and post-processing steps.

- `semantic/models.py` — Pydantic v2 models + enums: `SemanticModel`, `TableSemantic`, `ColumnSemantic`, `BusinessMetric` (discriminated union of `SimpleMetric`, `RatioMetric`, `DerivedMetric`, `CumulativeMetric`, `ConversionMetric`), `DerivedColumn`, `CommonJoin`, `RelationshipEdge`, `Segment`; enums: `SemanticType`, `CardinalityClass`, `RelationshipType`, `MetricType`, `GenerationStatus`
- `semantic/generator.py` — `SemanticModelGenerator` class: sends schema DDL batches to LLM (`generate_table_batch()`), generates cross-table sections (`generate_globals()`), detects drift (`detect_drift()`), computes schema hash and time expressions
- `semantic/formatter.py` — `SemanticFormatter.format_for_prompt()` converts `SemanticModel` to structured text injected as `## Business Context` in the LLM system prompt
- `routers/semantic.py` — three-phase generation endpoints (`/generate/init`, `/generate/batch`, `/generate/globals`), model CRUD, and drift check

### Query Cache

`cache/query_cache.py` — process-level singleton (`@lru_cache(maxsize=1)`):
- **Level 1:** Exact match on normalized (lowercase, stripped) question text
- **Level 2:** Cosine similarity of sentence-transformer embeddings vs. all cached entries for the same connection
- TTL filtering: entries not accessed within `CACHE_MAX_AGE_DAYS` are excluded from lookup
- `hit_count` uses a server-side increment to avoid read-modify-write races

`cache/example_library.py` — stores thumbs-up verified question → query pairs as few-shot examples for future prompts. `find_similar_examples()` applies three filters before returning: dialect match (no cross-dialect mixing), cosine similarity ≥ 0.4 (low-scoring examples hurt accuracy), then top-3 by score.

### PostgreSQL App Database

The application's own state is stored in the `db` PostgreSQL service (port 5434 on the host). Schema is managed by Alembic migrations (`backend/alembic/versions/`). Tables:

| Table | Purpose |
|---|---|
| `users` | User accounts (bcrypt passwords, roles, email verification) |
| `refresh_tokens` | Stored refresh tokens (sha256 hash only — never raw) |
| `connections` | Saved database connections (Fernet-encrypted host/port/db config) |
| `chat_sessions` | Conversation sessions, one per connection+topic |
| `chat_messages` | Individual messages (user and assistant) with query, results, status |
| `provider_configs` | Saved LLM provider configurations (Fernet-encrypted API keys) |
| `query_cache` | Cached question → query pairs with embeddings |
| `verified_examples` | Thumbs-up approved examples for few-shot prompting |
| `app_settings` | Persisted application-level settings overrides |
| `semantic_suggestions` | User-submitted feedback on generated queries for model improvement |
| `query_usage` | Per-user daily query counts |
| `user_schema_caches` | Per-user cached schema snapshots with table embeddings |

---

## Security Layers

| Layer | Mechanism |
|---|---|
| Credential storage | Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) |
| Query safety | Allowlist validation — only SELECT / WITH permitted |
| PG-specific safety | Blocked functions + patterns (pg_sleep, COPY TO, SET ROLE, etc.) |
| Row limit | Auto-injected LIMIT clause; configurable timeout |
| Privacy | Per-connection controls on what schema metadata reaches the LLM |
| Network | Backend runs as non-root `appuser`; app DB on an isolated Docker network (`savvina`) |

---

## Key Design Decisions

### Why PostgreSQL for the App DB?

The app database stores user accounts, encrypted credentials, session history, cache embeddings, and audit logs. PostgreSQL provides the ACID guarantees needed for multi-user auth (refresh token rotation uses `SELECT FOR UPDATE` to prevent race conditions), connection pooling via asyncpg, and the ability to scale to multi-instance deployments without locking contention. The schema is managed by Alembic migrations, making upgrades deterministic and reversible.

### Why a Process-Level Cache Singleton?

Loading a sentence-transformer model takes 1–3 seconds. Using `@lru_cache(maxsize=1)` on `_get_shared_cache()` ensures the model is loaded once at startup and reused across all requests. The pre-warm call in `lifespan()` ensures the first real user request doesn't pay the cold-start penalty.

### Why Fernet and Not bcrypt/Argon2?

Fernet is symmetric — it can decrypt as well as encrypt, which is required for credentials (we need to actually use the stored password to connect to the database). bcrypt is a one-way hash and cannot be decrypted. Fernet uses AES-128-CBC + HMAC-SHA256, which is standard and well-audited.

### Why asyncpg Instead of psycopg3?

asyncpg is a purpose-built async PostgreSQL driver with no synchronous blocking code. It delivers lower latency and higher throughput than psycopg3's async wrapper for the workloads Savvina AI produces (many small metadata queries + occasional user queries).
