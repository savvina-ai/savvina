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

1. Loads the connection's semantic model rows from the database.
2. Filters it to the tables and columns actually visible in the user's schema (privacy + permissions). Columns your privacy settings hide are left out of the prompt, along with relationships that use them and any business metric, segment, derived column, note or default filter that names a hidden table or column. Only what your privacy settings hide counts: a metric or note that names a column dropped or renamed in the database is out of date, not secret, so it stays in the prompt until you edit or remove it (or a regenerate replaces a generated one). Descriptions you write for visible tables and columns are not checked: do not name a hidden table or column in one.
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

Once a model exists the button reads **Regenerate**. Regenerating asks the LLM to re-annotate the model from your current schema. Saved fields you edited by hand are kept, and tables and columns that are no longer in the database are removed. Deleting a generated item is not remembered: a metric, segment, derived column, join or relationship the LLM generated and you deleted (or renamed) may come back on the next regenerate, so delete it again after regenerating if you do not want it. A hand edit that names a column the table no longer has (for example a primary timestamp column) is cleared or trimmed, and the **Incomplete generation** box at the top of the page says so.

A run starts from the saved model, so generation is disabled while you have unsaved edits: save them first. While a run is in progress the editors are read-only ("Editing is paused while generation runs."), **Save Changes** and applying suggestions are disabled, and **Regenerate** shows *Generating…* until the run ends.

If the LLM returns nothing for a table, the page asks once more for the tables it skipped. A table still without annotations at the end is listed in the **Incomplete generation** box; **Regenerate** to annotate it.

#### If generation is interrupted

A provider rate limit, a timeout or a closed tab can stop a run part-way (a rate-limited call is first retried a few times, waiting longer each time; an exhausted provider balance or any other provider error stops the run at once). The tables generated so far are kept, and the page shows a banner ("Generation was interrupted at table N of M") with three actions:

| Action | What it does |
|--------|--------------|
| **Resume** | Generates only the tables that are still missing, then the metrics and joins. |
| **Finish with what's generated** | Skips the missing tables and builds the metrics and joins from the tables already generated. Disabled when no tables were generated. |
| **Restart from scratch** | Deletes the model, including your edits, and generates it again. The only action that discards work; it asks for confirmation first. |

Resume is refused if the database schema changed since the interrupted run. Use **Regenerate** in that case: it generates against the current schema and keeps your edits. **Restart from scratch** also works but discards them.

### Via the API

Generation is a three-phase process. All endpoints accept a `provider` query parameter (type name or config UUID; default: `claude`).

```bash
# Phase 1 — initialise: returns pending_table_keys, the tables still to generate.
# Nothing is deleted. Add resume=true to continue an interrupted run.
POST /api/v1/connections/{id}/semantic/generate/init?provider=claude

# Phase 2 — one call per batch of 1–4 table keys taken from pending_table_keys
POST /api/v1/connections/{id}/semantic/generate/batch?provider=claude
{"table_keys": ["public.customers", "public.orders"]}

# Phase 3 — generate cross-table metrics, joins, and derived columns
POST /api/v1/connections/{id}/semantic/generate/globals?provider=claude
```

---

## Managing the Model

| Action | When to use |
|--------|-------------|
| **Generate / Regenerate** | First setup, or to re-annotate every table after a schema change (for a small change, **Preview & apply** below is quicker). Regenerating refreshes the LLM-written parts of the model and keeps the fields you edited by hand, including metrics, segments, derived columns and joins you wrote or edited. A generated item you deleted may come back. Tables and columns that no longer exist in the database are removed. |
| **Save Changes** | Persist in-progress edits. All edits live in a local draft until saved. |
| **Check Drift** | Compare the saved model against the live database (privacy settings applied); warns if referenced tables or columns no longer exist, if new tables appeared, if new columns appeared in a table the model holds, or if the connection's role cannot read a table the model contains (PostgreSQL only, if the role can read the system catalog; otherwise, and on MySQL, such a table looks like a dropped one and is reported as removed). Each check also refreshes your cached schema. If the database cannot be reached, the check uses your cached schema, if you have one, and says so (**Preview & apply** is then unavailable); otherwise it fails with an error. Saving, applying a suggestion or completing a regenerate clears cached schemas (a regenerate that stops before its last step keeps them, and an applied drift keeps yours, which it has just refreshed), so a check straight after one of those needs the database. A column the connection's role cannot read looks like a dropped one and is reported, and applied, as removed. When a table or column was added or removed, the warning offers **Preview & apply**. |
| **Preview & apply** | Appears under the drift warning when the check found a table or column added or removed. Syncs the model to that change without regenerating everything — see [Applying schema drift](#applying-schema-drift). |
| **Drift history** | Appears below the drift panels once an apply has been made, collapsed under a **Drift history** heading. Expand it to list the most recent applies, newest first, with what each added and removed; click an entry for the full list. It survives a reload, and deleting the model does not erase it: it shows again once a model exists. |
| **Delete** | Remove the semantic layer entirely, including your edits. Queries still work but without business context. |
| **Marked as reviewed** | Tracking flag only — has no effect on prompt generation. Use it to indicate the model has been human-verified. Regenerating clears it, and so does an applied drift that annotates new tables or new columns. |

Fields you edit by hand are remembered as yours: a later regenerate refreshes the rest of the model but leaves them alone. Edits to an item are kept; deleting a generated item is not remembered, so a regenerate may add it back (renaming one keeps your renamed copy, and the original may come back beside it). An applied drift does not bring back a deleted relationship: it adds the database's relationships only on new tables and new columns, so a foreign key added later between two existing columns appears on the next **Regenerate**. One exception is described under [New columns](#new-columns). A column marked **Sensitive** stays sensitive after a regenerate. **Delete** and **Restart from scratch** discard your edits, and a regenerate or an applied drift also removes hand-edited tables and columns that no longer exist in the database.

> **Schema refresh** does not change the semantic model. After a schema change, run **Check Drift** and then **Preview & apply** to sync the model (see below). **Regenerate** re-annotates every table; it is needed to refresh the LLM's descriptions, or to pick up a column Check Drift does not report as new (see [New columns](#new-columns)).

### Applying schema drift

**Preview & apply** syncs the model to the changes **Check Drift** found, without regenerating the tables that did not change.

1. **Preview.** Click **Preview & apply**. A panel headed "Apply Drift will:" lists, by name, what the apply would change — for example "Will remove 1 table: ecommerce.coupons" — and the new tables and new columns it would annotate (at most 12 new tables per apply; any more are counted in a warning that names up to five of them). Nothing is written to the model until you click **Apply these changes**; **Cancel** closes the preview. The preview makes no LLM call, so the annotations of new columns, relationships to new tables and the metrics, joins and other items the LLM writes over new tables appear only after you apply. The relationships the database defines on the new columns between tables the model already holds are listed, except on a column the model holds under a different spelling (see [New columns](#new-columns)).
2. **Apply.** Removed tables leave the model with the relationships that use them and the metrics, joins and derived columns that list the table by its qualified name (a metric that names the table by bare name stays, and Check Drift reports it; a bare-name reference in a join or derived column, or one only inside SQL, stays and is not reported), and with the segments that apply only to them (a segment that also covers a surviving table stays and stops applying to the removed one). Removed columns leave their table with the relationships and table settings that used them (primary timestamp and date columns, partition and cluster columns, hierarchy levels); free SQL such as default filters and metric expressions is left as written. New columns are annotated by the LLM and added to their table; the columns the table already had, and its settings such as the primary timestamp column, are not touched. New tables are annotated by the LLM, and the new metrics, joins, derived columns and segments the LLM writes over them are added beside the ones you have (nothing is added over tables that did not change, so an item you deleted stays deleted). A metric, join or segment removed with its table loses any edit mark, and one of the same name written by the LLM is a fresh item. Your hand edits on surviving tables are kept. Tables hidden by privacy settings, and on PostgreSQL (if the role can read the system catalog) tables unreadable by the connection's role, are left alone.
3. **Result.** A "Schema drift applied" summary of what actually changed stays on the page until you dismiss it or start another preview, apply or run, and the apply is added to **Drift history**.

Things to know:
- Both **Preview & apply** and **Apply these changes** are disabled while you have unsaved edits; save first.
- When the preview lists new tables, or new columns with nothing to remove, the apply needs a working LLM provider (the one selected on the page). New columns beside removed tables or columns do not: without a working provider the removals are applied and the summary says the new columns were not annotated.
- While the apply runs, editing is paused and **Regenerate**, **Save Changes** and **Check Drift** are disabled.
- Past 12 new tables the rest wait: the summary warns, and running **Check Drift** and applying again annotates the next ones (tables the LLM skipped last time are retried after the others).
- If the LLM returned nothing for a new table, the summary says so and the table stays missing. Run **Check Drift** again and apply to retry.
- New columns share the apply's three LLM batches with new tables (new tables go first), at most 25 columns per batch. The rest wait: the summary warns, naming up to five of their tables, and running **Check Drift** and applying again annotates them.
- If the LLM call fails or returns nothing for a table's new columns, the rest of the apply still goes through and the summary counts the columns that were not annotated and names the first few. Running **Check Drift** and applying again retries them after the others.
- If new columns are the only change and the provider rate-limits the apply or is unavailable, the apply fails with that error, unless an earlier batch already succeeded: then what it annotated is saved and the summary names the rest.
- Preview and apply are also disabled while a suggestion is being applied, a save is in flight or a drift check is running, and **Preview & apply** is unavailable when the check could only use your cached schema (check again once the database is back). An apply that changed nothing in the model (for example, the LLM returned nothing for the only new table) is shown as "Nothing was applied". It is added to **Drift history** only when it skipped tables or new columns, because the next apply uses that entry to retry them after the others.

### New columns

**Check Drift** reports a column as new only if the model has not seen it before. For each table the model remembers the columns it has seen: those it was generated with and those an apply added. (A column added to the model through the API, `PUT /api/v1/connections/{id}/semantic`, is remembered from the next apply or regenerate.) So:
- A column the LLM left out when the model was generated is not reported again, and neither is one the model had that was deleted through the API.
- A column the LLM leaves out while annotating the other new columns of its table is not retried either; the summary names it. **Regenerate** to annotate it.
- If the LLM returns nothing for a table, its new columns stay new and the next apply retries them.
- A column that leaves the database is forgotten with its annotation, so if it comes back it is new again. A column hidden by privacy settings keeps its annotation, but the model stops tracking it: if you show it again and the model never annotated it, Check Drift reports it as new. Hidden tables keep what the model has seen.
- A column the model holds under a slightly different spelling (in quotes, or in different letter case) is annotated again under its real name; the old annotation is not kept. If its LLM call fails or returns nothing for its table, or the column waits for a later apply, Check Drift reports it again as a new column, and a relationship on it that you deleted can then come back once, with a later apply. If the LLM annotates the table's other new columns but leaves this one out, it is treated like any other column left out (above).
- A model created before this tracking existed starts tracking at its first **Check Drift**, apply or regenerate, so a column added before then is not reported; **Regenerate** to annotate it.

One gap, for changes made through the API: a new column added to the model and deleted again before an apply or regenerate has recorded it is reported as new.

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
