# Semantic Models

The Semantic Model is a business-context layer you configure on top of a database connection. It tells the LLM what your tables and columns *mean*, defines reusable business calculations, and constrains query generation so results are accurate — without requiring the user to know SQL.

Consider a database with columns like `cx_tp_cd`, `ord_sts`, `prd_ctg_id`. Without context, an LLM guesses what these mean. With a semantic model, the LLM knows:

```
cx_tp_cd   → Customer Type: E=Enterprise, S=SMB, I=Individual
ord_sts    → Order Status: P=Pending, C=Confirmed, X=Cancelled
prd_ctg_id → FK to Product Category ID
```

---

## How It Works

When a user asks a question in chat, the backend:

1. Loads the connection's `semantic_model` JSON from the database.
2. Filters it to tables actually visible in the user's schema (privacy + permissions).
3. Formats it into a `## Business Context` block injected into the LLM system prompt, after the raw schema DDL.
4. The LLM generates SQL using both the raw schema **and** this business context.

If the prompt exceeds the provider's context window, a compression cascade drops content in priority order — see [Common Pitfalls — Semantic model dropped from prompt](#semantic-model-dropped-from-prompt) for the full sequence and mitigations.

---

## Generating a Semantic Model

### Via the UI

1. Go to **Connections** → click your connection.
2. Click the **Semantic Model** tab.
3. Select an LLM provider from the dropdown.
4. Click **Generate Semantic Model**.
5. Wait 10–60 seconds depending on schema size and model speed.
6. Review the output carefully — especially **Business Metrics** and their filters.
7. Click **Save Changes**.

### Via the API

Generation is a three-phase process. All endpoints accept a `provider` query parameter (type name or config UUID; default: `claude`).

```bash
# Phase 1 — initialise: introspects schema and returns batch count
POST /api/v1/connections/{id}/semantic/generate/init?provider=claude

# Phase 2 — repeat for each batch (batch_idx 0 … batch_count-1)
POST /api/v1/connections/{id}/semantic/generate/batch?batch_idx=0&provider=claude

# Phase 3 — generate cross-table metrics, joins, and derived columns
POST /api/v1/connections/{id}/semantic/generate/globals?provider=claude
```

---

## Managing the Model

| Action | When to use |
|--------|-------------|
| **Generate** | First setup, or after a major schema change when you want the LLM to start fresh. **Replaces the entire model — manual edits will be lost.** |
| **Save Changes** | Persist in-progress edits. All edits live in a local draft until saved. |
| **Check Drift** | Compare saved model against the live schema; warns if referenced tables or columns no longer exist. |
| **Delete** | Remove the semantic layer entirely. Queries still work but without business context. |
| **Marked as reviewed** | Tracking flag only — has no effect on prompt generation. Use it to indicate the model has been human-verified. |

> **Schema refresh** does not regenerate the semantic model. After refreshing the schema to pick up new tables/columns, regenerate the model manually if needed.

---

## Tabs and Fields

### Tables

Editable view of every table in the model. Each table entry contains:

| Field | Description |
|-------|-------------|
| **Display name** | Human-readable table name shown to the LLM (e.g. `Orders` instead of `tbl_ord_mstr`). |
| **Description** | One-sentence description of what the table contains. |
| **Default filters** | SQL conditions always applied to this table (e.g. `o.status NOT IN ('cancelled', 'refunded')`). The LLM is instructed to apply these unless the user explicitly asks to override. **This is the strongest way to enforce domain constraints on generated SQL.** |
| **Domain** | Optional business domain tag (e.g. `finance`, `marketing`, `ops`). Tables sharing the same domain receive a cosine-score boost during schema pruning so they tend to be selected together even if some have lower individual similarity scores. Leave blank if your schema is not partitioned by domain. |
| **Grain** | Describes what one row represents (e.g. `one row per order`, `one row per daily snapshot per user`). Injected verbatim so the LLM understands the table's granularity and avoids incorrect fan-out joins. |
| **Primary timestamp column** | Column to use for time-based questions by default (e.g. `created_at`). Rendered with a `[primary timestamp]` label in the column summary block. |
| **Primary date column** | Date-only counterpart to the primary timestamp (e.g. `order_date`). Rendered with a `[primary date]` label. |
| **Partition columns** | Columns the database physically partitions on. The LLM is instructed to always filter on these; queries without such a filter will full-scan and are expensive. |
| **Cluster columns** | Columns the database clusters on. Marked as filter-friendly in the prompt. |
| **Base SQL** | For views and CTEs: the underlying SQL expression. The LLM uses this as a CTE/subquery reference. Only the first line is included in the prompt to stay compact. |
| **Hierarchies** | Ordered drill-down paths within the table (e.g. `year → quarter → month → week → day`). Prevents the LLM from mixing grain levels in a single aggregation. Each hierarchy has a **name** and an ordered list of **levels** (column names or aliases). |
| **Columns** | Expandable per-column editor (see below). |

#### Column Fields

Click any column row to expand it:

| Field | Description |
|-------|-------------|
| **Display name** | Human-readable column name. |
| **Description** | What the column contains. |
| **Value mappings** | Maps raw coded values to display labels (e.g. `confirmed → Confirmed`). Enumerates all valid values in the prompt — prevents the LLM from hallucinating values like `'completed'` that don't exist in the data. Add entries for every status/type column. |
| **Sensitive** | Marks the column `[SENSITIVE]`. The column is omitted entirely from the LLM prompt (not just from SELECT clauses). |
| **Default aggregation** | Prescribes the aggregation function to use for this column: `sum`, `count`, `count_distinct`, `count_distinct_approx`, `average`, `max`, `min`, `median`. Appended as `[aggregate: sum]` in the prompt. |
| **Non-additive** | When `true`, the LLM is told never to SUM this column across dimensions (e.g. distinct-count metrics, percentages, balances). Use for any measure that is not safely addable across rows. |
| **Time granularity** | The coarsest meaningful time grain for DATE or TIMESTAMP columns: `second`, `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year`. Appended as `[grain: month]` in the prompt. |

> Column Intelligence fields (`semantic_type`, `cardinality`, `currency`, `unit`) are auto-generated and shown in the Column Intelligence tab. They are not editable in the Tables tab UI but are preserved when you save.

---

### Column Intelligence

Read-only panel summarising auto-generated metadata for every column across all tables:

| Attribute | Values |
|-----------|--------|
| **Semantic type** | `identifier`, `status_flag`, `monetary`, `percentage`, `timestamp`, `date`, `free_text`, `categorical`, `boolean_flag`, `foreign_key`, `url`, `email`, `phone`, `measurement`, `count`, `unknown` |
| **Cardinality** | `unique`, `high` (>100 distinct), `medium` (10–100), `low` (<10 — good for filtering/grouping) |
| **Currency** | ISO code for `monetary` columns (e.g. `EUR`, `USD`). |
| **Unit** | Physical unit for `measurement` columns (e.g. `kg`, `km`). |

To correct these values, expand the column in the **Tables** tab and edit the relevant field there.

> **`foreign_key` semantic type** is what marks a column as a foreign key reference. Formal join paths are captured in the **Relationships** tab, not as a separate column attribute.

---

### Business Metrics

Named, reusable calculations for common KPIs. The LLM is instructed to use the exact definition when the metric name is relevant to the user's question.

#### Common fields (all metric types)

| Field | Description |
|-------|-------------|
| **Name** | Short label (e.g. `Total Revenue`, `Customer Total Spend`). |
| **Description** | Plain-English explanation of what the metric measures. |
| **Filters** | SQL conditions applied as a post-aggregation filter when this metric is used (e.g. `o.status NOT IN ('cancelled', 'refunded')`). Rendered as `[filter: ...]` in the prompt. |
| **Related tables** | Comma-separated table keys (e.g. `store.orders, store.customers`). |
| **Format hint** | Optional display format: `currency_eur`, `currency_usd`, `currency_gbp`, `percentage`, `integer`. |
| **Non-additive** | When `true`, the LLM is warned not to SUM this metric across dimensions and is told to use `LAST_VALUE` or a snapshot approach instead. |
| **Non-additive dimension** | The dimension over which the metric is non-additive (e.g. `date` for a balance metric). Clarifies the warning message. |

#### Metric types

The `metric_type` field selects the calculation variant. The UI creates `simple` metrics by default; all types can be set via the API.

**`simple`** — single aggregation over one expression.

| Field | Description |
|-------|-------------|
| **Definition** | SQL expression to aggregate (e.g. `orders.total_amount`). |
| **Aggregation** | Aggregation function: `sum`, `count`, `count_distinct`, `count_distinct_approx`, `average`, `max`, `min`, `median`. |
| **Measure filters** | Pre-aggregation `FILTER (WHERE ...)` clauses appended to the aggregate call. |

**`ratio`** — numerator / denominator pair. Rendered as `(numerator) / NULLIF(denominator, 0)`.

| Field | Description |
|-------|-------------|
| **Numerator expr** | SQL expression for the numerator (e.g. `SUM(orders.revenue)`). |
| **Denominator expr** | SQL expression for the denominator (e.g. `COUNT(DISTINCT orders.customer_id)`). |

**`derived`** — formula combining other metrics or expressions. Aggregation is embedded inside `definition`.

| Field | Description |
|-------|-------------|
| **Definition** | Complete SQL formula (e.g. `(SUM(base_price) - SUM(cost_price)) / NULLIF(SUM(base_price), 0) * 100`). |

**`cumulative`** — running total or window function.

| Field | Description |
|-------|-------------|
| **Definition** | SQL expression to aggregate. |
| **Aggregation** | Aggregation function (same values as `simple`). |
| **Window** | Description of the OVER clause (e.g. `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`). |
| **Measure filters** | Pre-aggregation `FILTER (WHERE ...)` clauses. |

**`conversion`** — funnel metric: fraction of a base event that leads to a conversion event.

| Field | Description |
|-------|-------------|
| **Base measure** | Name of the base event (e.g. `sessions`). |
| **Conversion measure** | Name of the conversion event (e.g. `signups`). |
| **Entity** | Join column linking the two events (e.g. `user_id`). |
| **Window** | Time window for attribution (e.g. `7 days`). |
| **Calculation** | `conversion_rate` (default) or `conversions` (absolute count). |

> **Warning:** Auto-generated metrics often have wrong filter values — the LLM guesses status values from training data (e.g. `status = 'completed'`) that may not exist in your database. Always verify with `SELECT DISTINCT status FROM table` and correct accordingly. A wrong filter here actively teaches the LLM to use the wrong value for every spend-related query.

---

### Common Joins

Frequently-used join patterns stored as model metadata.

| Field | Description |
|-------|-------------|
| **Description** | Label for the join (e.g. `Customer orders`). |
| **Tables** | Tables involved. |
| **Join pattern** | SQL ON condition (e.g. `customers.id = orders.customer_id`). |

> **Common Joins are not injected into the LLM prompt.** The **Relationships** tab covers formal FK joins, which the LLM prefers. Common Joins are retained for documentation and tooling purposes only.

---

### Relationships

Formal foreign-key relationships sourced from `information_schema` (never inferred). Rendered under `RELATIONSHIPS (use these for JOINs)`. The LLM prefers these over ad-hoc join patterns.

| Field | Description |
|-------|-------------|
| **From table / column** | Child side of the FK (e.g. `store.orders.customer_id`). |
| **To table / column** | Parent side of the FK (e.g. `store.customers.id`). |
| **Relationship type** | `many_to_one`, `one_to_one`, `one_to_many`. |
| **Join SQL** | Full join condition included verbatim in the prompt. |
| **Required** | `true` if the FK column is NOT NULL — the LLM uses `INNER JOIN`. `false` (nullable FK) — the LLM uses `LEFT JOIN` to preserve rows. |
| **Entity type** | Role of the `from_table` in the relationship: `primary` (PK/dimension side), `unique` (alternate key), `foreign` (FK/fact side), `natural` (business key, may not be unique). Appended as a hint in the prompt. |
| **Description** | Optional contextual note. |

You can manually add a relationship via the **Add Manual Relationship** form. Manual relationships always default to `many_to_one`, nullable (`is_required: false`).

---

### Derived

Custom calculated fields for which the LLM must use an exact SQL expression — regardless of how the user words the question.

| Field | Description |
|-------|-------------|
| **Name** | Concept name (e.g. `Gross Margin %`). |
| **SQL expression** | Exact expression (e.g. `(base_price - cost_price) / NULLIF(base_price, 0) * 100`). |
| **Description** | What the calculation represents. |
| **Format hint** | `percentage`, `currency_eur`, `currency_usd`, `currency_gbp`, `integer`. |
| **Base tables** | Tables the expression references (comma-separated). |

Rendered under `DERIVED CALCULATIONS (always use these exact expressions)`.

---

### Segments

Named, reusable WHERE-clause fragments. The LLM references these by name instead of regenerating complex filter logic, preventing inconsistent predicate spelling across queries.

Rendered under `NAMED FILTERS (use these exact WHERE clauses — do not rewrite them)`.

| Field | Description |
|-------|-------------|
| **Name** | Short identifier (e.g. `active_customers`, `paid_orders`). |
| **SQL expression** | Complete WHERE-clause fragment (e.g. `status = 'active' AND deleted_at IS NULL`). |
| **Description** | Plain-English description of what the segment selects. |
| **Applicable tables** | Tables the segment applies to (comma-separated). Rendered as `[applies to: ...]` in the prompt. |

> Segments are defined and edited via the API (`PUT /api/v1/connections/{id}/semantic`) or by the generator. There is no dedicated Segments tab in the UI.

---

### Time Expressions

Named aliases for time-based SQL expressions used in date-range queries.

| Field | Description |
|-------|-------------|
| **Label** | Short name (e.g. `this_month`, `last_7_days`). |
| **Expression** | SQL fragment (e.g. `DATE_TRUNC('month', CURRENT_DATE)`). |

Also stores the database timezone (e.g. `UTC`, `Europe/London`), rendered as `Database timezone: UTC` in the prompt.

---

### Notes

Free-form cross-table annotations injected verbatim at the top of every prompt, before table definitions. Use these to capture facts that cannot be expressed as table or column metadata — for example:

- `finance.budgets is quarterly; for monthly revenue use finance.transactions`
- `customer_id in orders refers to the B2C customer table; for B2B use account_id`

Notes are partially auto-populated by the generator and can be extended via the API:

```bash
PUT /api/v1/connections/{id}/semantic
{
  "notes": [
    "finance.budgets is quarterly; for monthly revenue use finance.transactions"
  ]
}
```

There is no dedicated Notes tab in the UI.

---

---

## How the Model Appears in the LLM Prompt

The formatter produces a `## Business Context` block injected after `## Database Schema`. Example:

```
## Business Context

SEMANTIC CONTEXT:

NOTES (apply to all queries against this database):
  • finance.budgets is quarterly; for monthly revenue use finance.transactions

Table "store.orders" → Orders
  Description: Records customer orders, including financial totals and status.
  Grain: one row per order
  Performance: ALWAYS filter on created_at — queries without this filter will full-scan and are expensive.
  Default filters: o.status NOT IN ('cancelled', 'refunded')
  Primary timestamp: created_at
  Columns:
    - status (Order Status): Pending='pending', Confirmed='confirmed', Cancelled='cancelled'  [SQL values: 'pending' | 'confirmed' | 'cancelled']
    - total_amount (Order Total) (currency: USD) [aggregate: sum]
    - customer_id (Customer ID)
  Aggregate with: total_amount (USD)
  Group by:       status
  Date columns:   created_at [primary timestamp]
  Identifiers:    id, customer_id  [do not aggregate]
  Drill path (Date Hierarchy): year → quarter → month → week → day

RELATIONSHIPS:
  FROM orders INNER JOIN customers ON store.orders.customer_id = store.customers.id

Business Metrics:
  - Customer Total Spend = SUM(o.total_amount) [filter: o.status NOT IN ('cancelled', 'refunded')] [currency_usd]
      Total purchase value per customer, excluding cancelled and refunded orders
  - Revenue vs Target = (SUM(revenue)) / NULLIF(SUM(target), 0) [type: ratio]
      Actual revenue as a fraction of the period target.
  - Active Signups (7d) = signups / sessions per user_id within 7 days [conversion_rate] [type: conversion]
      Fraction of sessions that result in a signup within 7 days.

DERIVED CALCULATIONS (always use these exact expressions):
  "Gross Margin %" = (base_price - cost_price) / NULLIF(base_price, 0) * 100
    Format as: percentage

NAMED FILTERS (use these exact WHERE clauses — do not rewrite them):
  "active_customers": status = 'active' AND deleted_at IS NULL [applies to: store.customers]
    Customers who have not been soft-deleted and are in active status.

TIME EXPRESSIONS (use these for date-based questions):
  "this_month" = DATE_TRUNC('month', CURRENT_DATE)
  Database timezone: UTC
```

---

## Priority of Constraints (Strongest → Weakest)

1. **Default filters** on tables — always applied; override training data bias.
2. **Business Metric filters** — `[filter: ...]` on named calculations.
3. **Named Filters (Segments)** — reusable WHERE-clause fragments; referenced by name.
4. **Value mappings** — enumerate valid values; prevent hallucinated values.
5. **Sensitive flag** — suppresses column entirely from the prompt.
6. **Non-additive flag** — warns LLM against summing across dimensions.
7. **Descriptions and Notes** — provide naming and domain context.

---

## Common Pitfalls

### Wrong status values in auto-generated metrics

The LLM guesses status values from training data (e.g. `'completed'`) that may not exist in your database. Verify actual values:

```sql
SELECT DISTINCT status FROM schema.table ORDER BY 1;
```

Correct the metric filter and — for strongest enforcement — also set a **Default filter** on the table itself (e.g. `o.status NOT IN ('cancelled', 'refunded')`).

### Sensitive columns blocking entire queries

Marking a column `[SENSITIVE]` removes it entirely from the LLM prompt. The LLM will be unaware of the column's existence. If this causes broken queries (e.g. a join key is sensitive), consider using connection-level privacy controls instead.

### Semantic model dropped from prompt

For connections with many tables and columns, the full semantic model may not fit within the LLM's context window. The system progressively compresses the prompt before dropping content:

1. Few-shot examples are removed first.
2. The semantic model is **compacted** — per-table DDL, relationships, and derived columns are stripped, but **business metrics, segments, and time expressions are preserved**.
3. If still too large, the entire semantic model is dropped.
4. Finally, schema annotations (sample values, comments, row counts) are stripped.

Check backend logs for `semantic_context=False` to confirm the model was dropped entirely. Mitigations:

- Use a provider with a larger context window (Claude, GPT-4o).
- Exclude unused schemas/tables via connection privacy settings.
- Keep descriptions short and focused.
- Business Metrics, Segments, and Time Expressions survive compression longest — prioritise these if you must choose what to fill in.
