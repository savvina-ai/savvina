# Execution Modes

Every connection has an **execution mode** that controls what happens after Savvina AI generates a SQL query. Choose the mode that matches your comfort level with the database.

---

## The Three Modes

### Auto-Execute ⚡

The generated query is validated and executed immediately. Results appear in the chat without any intermediate step.

**Best for:**
- Development or staging databases
- Databases where a read-only role is in place
- Users who want the fastest workflow

**Flow:**
```
Question → [Cache check] → LLM → Validate → Execute → Results shown
```

---

### Review First 👁️

The generated query is shown for your review before any execution. You can run it as-is, edit it, copy it, or cancel.

**Best for:**
- Production databases
- Any connection where you want a human checkpoint before touching data
- Situations where LLM accuracy must be verified before execution

**Flow:**
```
Question → [Cache check] → LLM → Validate → Show query for review
                                                       │
                        ┌──────────────────────────────┘
                        ▼
              [Run] → Execute → Results shown
              [Edit] → User modifies query → Validate → Execute → Results shown
              [Copy] → User copies query (no execution in Savvina AI)
              [Cancel] → No execution
```

---

### Generate Only 📝

The query is generated and displayed but never executed by Savvina AI. Copy the query and run it in your own SQL client.

**Best for:**
- Maximum control over what runs
- Environments where all queries must go through a DBA review process
- Using Savvina AI purely as a SQL translation tool
- Databases not accessible from the Savvina AI backend (e.g., VPN-only databases)

**Flow:**
```
Question → [Cache check] → LLM → Validate → Show query (copy only)
```

---

## Changing Execution Mode

### Per-Connection Setting

Execution mode is set per connection:

1. Go to **Connections** → click your connection
2. Click **Execution Mode** tab (or scroll to the section)
3. Select your desired mode
4. Click **Save**

You can also set it during connection creation.

### API

```bash
PUT /api/v1/connections/{id}/execution-mode
Content-Type: application/json

{"execution_mode": "review_first"}
```

Valid values: `auto_execute`, `review_first`, `generate_only`

---

## Execution Mode and the Query Cache

When a query is served from the cache:

| Mode | Behavior |
|---|---|
| Auto-Execute | Cached query executes immediately; response tagged `status: cached` |
| Review First | Cached query is still shown for review before execution |
| Generate Only | Cached query is shown; no execution |

The cache respects execution mode exactly as if the LLM had generated the query.

---

## Editing Queries (Review First Mode)

When you click **Edit** in a Review First review panel:

1. The query block becomes an editable `<textarea>`
2. The "Run Query" button changes to "Run Edited Query"
3. When submitted, the edited query goes through the **same safety validation** as any generated query
4. If the edit introduces a disallowed statement (e.g., `UPDATE`), the execution is blocked with an error

Edits are recorded in the message history — the stored query is updated to reflect the user-edited version, not the original LLM output.

---

## Safety Validation (All Modes)

Regardless of execution mode, all queries — whether LLM-generated or user-edited — pass through the validator before execution. The validator rejects anything that is not a `SELECT` or `WITH` (CTE) statement, blocks dangerous keywords and dialect-specific functions, rejects multi-statement inputs, and adds a `LIMIT` clause if none is present. A query that fails validation is rejected with an error response — it is never sent to the database.

For the complete blocked keyword list and dialect-specific patterns (PostgreSQL, MySQL), see [Guardrails — SQL Query Validation](../architecture/guardrails.md#4-sql-query-validation).
