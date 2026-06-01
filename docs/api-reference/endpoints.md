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
    "excluded_columns": []
  },
  "execution_mode": "auto_execute"
}
```

**Execution mode values:** `auto_execute`, `review_first`, `generate_only`

**Response `201`:** `ConnectionResponse`

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

Delete a connection and cascade-delete all associated sessions, messages, cache entries, examples, and semantic model.

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
  "excluded_columns": ["public.users.password_hash"]
}
```

**Response `200`:** `ConnectionDetail`

---

### `PUT /api/v1/connections/{id}/execution-mode`

Change the execution mode for a connection.

**Request body:**
```json
{"execution_mode": "review_first"}
```

**Response `200`:** `ConnectionDetail`

---

## Semantic Model

### `GET /api/v1/connections/{id}/semantic`

Get the saved semantic model for a connection.

**Response `200`:** `SemanticModelResponse`
**Response `404`:** No semantic model saved

---

### `POST /api/v1/connections/{id}/semantic/generate/init`

Phase 1 of semantic model generation. Introspects the schema and returns the number of table batches to process.

**Query parameter:** `provider` (string, default: `claude`) — provider type name or config UUID

**Response `200`:** `GenerateInitResponse` — `{"batch_count": N, "table_count": M}`
**Response `400`:** Schema introspection failed or no API key configured

---

### `POST /api/v1/connections/{id}/semantic/generate/batch`

Phase 2 — call once per batch. Sends one batch of tables to the LLM and merges the result into the partial model.

**Query parameters:**
- `batch_idx` (int, required) — zero-based batch index (0 … batch_count-1)
- `provider` (string, default: `claude`)

**Response `200`:** `SemanticModel` (partial, updated after this batch)
**Response `400`:** `/generate/init` not called first, or batch_idx out of range

---

### `POST /api/v1/connections/{id}/semantic/generate/globals`

Phase 3 — generate cross-table business metrics, common joins, and derived columns. Call after all batches are complete.

**Query parameter:** `provider` (string, default: `claude`)

**Response `200`:** `SemanticModel` (final, saved to the connection)
**Response `400`:** Init or batch phases not completed
**Response `500`:** LLM generation failed

---

### `PUT /api/v1/connections/{id}/semantic`

Partially update the semantic model. Only the fields you provide are updated — unmentioned tables/metrics/joins are preserved.

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

**Response `200`:** `SemanticModelResponse`

---

### `DELETE /api/v1/connections/{id}/semantic`

Remove the semantic model from a connection. Future queries will not have business context.

**Response `204`:** No content

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
    "bytes_scanned": null
  },
  "execution_time_ms": 12.4,
  "status": "executed",
  "cache_hit": false,
  "error": null
}
```

**Status values:** `executed`, `cached`, `pending_approval`, `query_only`, `error`

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

**Response `200`:** `MessageResponse[]`

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

### `DELETE /api/v1/chat/examples/{example_id}`

Delete a verified example by its ID.

**Response `204`:** No content

---

## Providers

### `GET /api/v1/providers`

List all saved provider configs. Each entry shows status, model, and configuration — but never the decrypted API key.

**Response `200`:** `ProviderStatusResponse[]`

---

### `POST /api/v1/providers/test`

Test a provider configuration before saving it. Returns success/failure without creating any DB records.

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

Fetch available model IDs from a provider's API using the supplied credentials, **before** saving a config. Returns a sorted list of model ID strings. Falls back to an empty list if the provider's models endpoint is unreachable or credentials are invalid.

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

**Response `400`:** Unknown provider type
**Response `404`:** Config not found

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

Return the current application settings (non-sensitive fields only).

**Response `200`:**
```json
{
  "cache_enabled": true,
  "semantic_similarity_threshold": 0.87,
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "cache_max_age_days": 30,
  "default_query_timeout": 30,
  "default_row_limit": 1000,
  "log_level": "INFO"
}
```

### `PUT /api/v1/settings`

Update application settings. Changes take effect immediately without restart.

**Request body (all fields optional):**
```json
{
  "cache_enabled": false,
  "default_row_limit": 500
}
```

---

## Common HTTP Status Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Created |
| 204 | Success, no content |
| 400 | Bad request (invalid input, connection failed, validation error) |
| 404 | Resource not found |
| 500 | Server error (LLM failure, unexpected exception) |

Errors return a JSON body: `{"detail": "Human-readable error message"}`
