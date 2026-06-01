# PostgreSQL Testing

Connections, sample questions, and QA sessions for the PostgreSQL adapter.

See [user-testing-playbook.md](user-testing-playbook.md) for prerequisites and the full session index.

---

## Sample Database — `sample-postgres`

Seeds `savvina_test` with realistic synthetic data across six schemas.

### Schemas

| Schema | Contents |
|--------|----------|
| `ecommerce` | customers, orders, order_items, products, categories, brands, coupons, reviews, wishlists |
| `hr` | employees, departments, salary_history, leave_requests, performance_reviews, training_records |
| `finance` | accounts, transactions, invoices, budgets, expense_reports |
| `inventory` | warehouses, stock, stock_movements, suppliers, purchase_orders |
| `support` | tickets, ticket_messages, agents, sla_policies |
| `analytics` | page_views, marketing_campaigns, ab_tests, cart_events |

### Views

| View | Schema | Description |
|------|--------|-------------|
| `vw_sales_summary` | `analytics` | Monthly revenue, orders, discounts |
| `vw_product_performance` | `analytics` | Revenue, margin, units sold per product |
| `vw_cart_funnel` | `analytics` | Cart-to-purchase conversion rates by product/device/source |
| `vw_customer_ltv` | `analytics` | Customer lifetime value — **full-access only** (contains PII) |
| `vw_stock_alerts` | `inventory` | Products at or below reorder point per warehouse |
| `vw_ticket_metrics` | `support` | Weekly ticket volume, SLA breach rate, avg satisfaction |
| `vw_headcount_summary` | `hr` | Headcount vs budget per department — **full-access only** |
| `vw_training_completion` | `hr` | Training pass/fail/expired rates by department — **full-access only** |
| `vw_orders_no_pii` | `ecommerce` | Orders with customer segment instead of customer_id |

### Roles

| Role | Access |
|------|--------|
| `savvina` | Full access to all 6 schemas — used by Connection 1 |
| `savvina_analyst` | Read-only on `ecommerce` (partial), `inventory`, `support`, `analytics` (incl. `cart_events`); `hr` and `finance` blocked; `ecommerce.wishlists` blocked; RLS on `support.tickets` — used by Connection 2 |
| `savvina_admin` | Superuser for seeding only — do **not** use for app connections |

---

## Connection 1 — PostgreSQL · Full Access

**Use case:** Main analytics connection. All 6 schemas visible. Tests cross-schema
queries, complex JOINs, aggregations, and the full semantic layer.

| Setting  | Value             |
|----------|-------------------|
| Adapter  | PostgreSQL        |
| Host     | sample-postgres |
| Port     | 5432              |
| Database | savvina_test     |
| Username | savvina          |
| Password | *(value of `SAMPLE_POSTGRES_PASSWORD` in your `.env`)* |
| SSL      | disable           |

### Sample Questions

1. What was our total revenue last month, and how does it compare to the month before?
2. Show me the top 10 best-selling products by total revenue with their profit margin.
3. Which departments are over budget this quarter and by how much?
4. Show me all employees earning more than €80,000 sorted by department.
5. Which customers have spent more than €3,000 total and haven't ordered in the last 60 days?
6. What is the average ticket resolution time in hours, broken down by priority level?
7. Show me monthly revenue trend for the last 12 months.
8. Which marketing campaign generated the highest return on investment?
9. Show me customers who placed an order and also filed a support ticket in the same calendar month.
10. Which products are below their reorder point and in which warehouses?

### Extended Questions

1. Show me the 3-month rolling average revenue by product category.
2. Rank product categories by month-over-month revenue growth for the last 6 months.
3. Which customers placed at least one order in every month of the last quarter?
4. How many employees were hired in the last 6 months versus open headcount by department?
5. What is the Net Promoter Score equivalent — percentage of promoters minus detractors — from ticket satisfaction scores?
6. Which warehouse locations have the highest stockout frequency per SKU?
7. Who are the top 5 support agents by first-contact resolution rate this year?
8. Show me the sales funnel conversion rate at each pipeline stage.
9. Which marketing campaigns touched the most customers who later placed a second order?
10. Show me the average order value trend week-over-week for the last 8 weeks.

---

## Connection 2 — PostgreSQL · Read-Only Analyst

**Use case:** Restricted analyst access. `hr` and `finance` schemas are invisible.
`ecommerce.customers` is blocked. Row-Level Security hides fraud tickets.
Tests that Savvina correctly scopes introspection and doesn't generate queries
against schemas the user cannot access.

| Setting  | Value                 |
|----------|-----------------------|
| Adapter  | PostgreSQL            |
| Host     | sample-postgres     |
| Port     | 5432                  |
| Database | savvina_test         |
| Username | savvina_analyst      |
| Password | analyst_readonly_2024 |
| SSL      | disable               |

**Visible:** `ecommerce` (partial), `inventory`, `support` (no messages), `analytics`
**Blocked:** `hr`, `finance`
**RLS active:** `support.tickets` — fraud rows hidden silently

### Sample Questions

1. What is the total revenue from delivered orders this year?
2. Show me the top 5 products by number of units sold this month.
3. Which product categories have the highest average order value?
4. How many support tickets were opened this week, broken down by category?
5. What percentage of orders were cancelled versus delivered in the last 90 days?
6. Show me all products that are currently out of stock across all warehouses.
7. Which marketing campaigns are currently active and what is their total spend so far?
8. What is the average customer satisfaction score for resolved tickets per agent?
9. Show me the usage count for each active coupon code.
10. Which A/B tests have been completed and which variant won each one?

### Extended Questions

> Questions 1–3 test permission boundaries — they should return an error or empty result
> because the required schemas/tables are blocked for this role.

1. What is the average salary by department? *(blocked — `hr` schema invisible)*
2. Show me all budget line items for Q2. *(blocked — `finance` schema invisible)*
3. List all customers who placed an order this year. *(partial — LLM queries `ecommerce.orders` for `customer_id` values since `ecommerce.customers` is blocked; result is IDs only, no name/email/details)*
4. What is the month-on-month support ticket volume trend for the last 6 months?
5. Which product category had the most out-of-stock events this quarter?
6. Show me the top 3 A/B test variants by revenue lift.
7. What is the average cart abandonment rate by traffic source?
8. Which coupons have been redeemed more times than their configured usage limit?

---

## Session 1 — Basic SQL Happy Path

**Goal:** Verify the core NL→SQL→results flow works end-to-end for common analytical queries.

**Setup:** PostgreSQL Full Access · any provider · Auto-Execute

**Steps**

1. Open a new chat session on the PostgreSQL Full Access connection.
2. Send: `What was our total revenue last month, and how does it compare to the month before?`
3. Send: `Show me the top 10 best-selling products by total revenue with their profit margin.`
4. Send: `Which departments are over budget this quarter and by how much?`
5. Send: `Show me monthly revenue trend for the last 12 months.`
6. Send: `Which products are below their reorder point and in which warehouses?`

**Pass Criteria**

- Each message produces a SQL query, a plain-English explanation, and a results table.
- Results table has at least 1 row for revenue and product queries.
- SQL shown is syntactically valid PostgreSQL.
- Status badge shows **Executed** (green).
- Response time under 15 seconds per query.

**Watch For**

- Empty result sets on revenue queries — may indicate date range mismatch with seeded data.
- `500 Internal Server Error` — check backend logs.
- SQL referencing non-existent columns — semantic layer may need regeneration.

---

## Session 2 — Permission Boundary

**Goal:** Confirm that blocked schemas produce graceful errors (not crashes), visible
schemas return results, and RLS silently hides fraud tickets.

**Setup:** PostgreSQL Read-Only Analyst · any provider · Auto-Execute

**Steps**

1. Open a new chat session on the PostgreSQL Read-Only Analyst connection.
2. Send: `What is the total revenue from delivered orders this year?` — expects results
3. Send: `What is the average salary by department?` — expects error/empty (`hr` blocked)
4. Send: `Show me all budget line items for Q2.` — expects error/empty (`finance` blocked)
5. Send: `List all customers who placed an order this year.` — expects error/empty (`ecommerce.customers` blocked)
6. Send: `How many support tickets were opened this week, broken down by category?` — expects results but fraud rows silently absent (RLS)
7. Send: `Show me all products that are currently out of stock across all warehouses.` — expects results

**Pass Criteria**

- Steps 2, 6, 7 return non-empty result tables.
- Steps 3, 4, 5 return a user-friendly error or empty result — **no 500 crash**.
- No SQL generated for steps 3–5 references `hr`, `finance`, or `ecommerce.customers`.
- Fraud tickets are absent from step 6 results without any error message.

**Watch For**

- App crashing instead of returning a graceful message for blocked schemas.
- Schema introspection leaking blocked table names into the explanation text.

---

## Session 3 — Review First Editing

**Goal:** Validate the Review First execution mode — SQL is shown for approval, can be
edited, and the edited version executes correctly.

**Setup:** PostgreSQL Full Access · any provider · **Review First** (change in connection settings before starting)

**Steps**

1. Open a new chat session (Review First mode active).
2. Send: `Show me all employees earning more than €80,000 sorted by department.`
3. Verify the response shows the SQL in an editable box and an **Execute** button — no results yet.
4. Edit the SQL threshold: change `80000` to `60000`.
5. Click **Execute**.
6. Verify results reflect the edited threshold (more rows than at €80k).
7. Send: `Which marketing campaign generated the highest return on investment?`
8. Verify the SQL review panel appears again.
9. Click **Execute** without editing.
10. Verify results appear normally.

**Pass Criteria**

- After sending a message, results are NOT auto-executed — the review panel appears.
- Editing the SQL and clicking Execute runs the modified version.
- Results table reflects the edited SQL (more employees at ≥60k than ≥80k).
- Status badge shows **Executed** after approval.

**Watch For**

- SQL editor not pre-populated with the generated query.
- Editing not reflected in the executed query (stale state).
- Execute button unresponsive.

---

## Session 6 — Multi-Turn Context

**Goal:** Confirm follow-up questions within the same session use context from prior
turns rather than starting fresh.

**Setup:** PostgreSQL Full Access · any provider · Auto-Execute

> ⚠️ **Cache isolation required.** Step 5 (`Which of those products are below their
> reorder point?`) and step 8 (`Which products are below their reorder point?`) are
> semantically close enough to produce a cache hit. A cached response in step 8 would
> silently contain the context-scoped SQL from step 5, making the cross-session
> isolation check invalid. Follow step 6 to invalidate before opening the new session.

**Steps**

1. Open a new chat session.
2. Send: `Show me the top 5 products by total revenue this year.` — note the product names.
3. Send: `Now filter that to only products in the Electronics category.`
4. Send: `What was the revenue growth for those products compared to last year?`
5. Send: `Which of those products are below their reorder point?`
6. Click the **thumbs-down** button on the step 5 response to invalidate its cache entry.
7. Start a **new session**.
8. Send: `Which products are below their reorder point?`
9. Verify the status badge shows **Executed** (not Cached).

**Pass Criteria**

- Step 3 narrows results without re-stating context.
- Step 4 adds a year-over-year comparison column.
- Step 5 cross-references the inventory schema using products from step 2.
- Step 9 badge shows **Executed** (not Cached) — a fresh, context-free query was run.
- Step 8 returns a broader result than step 5 (no context carry-over).

**Watch For**

- Follow-up queries ignoring prior context and restarting from scratch.
- Context bleed between sessions (step 8 incorrectly filtering to Electronics).
- Step 8 returning a cache badge — thumbs-down in step 6 may not have invalidated correctly.

---

## Session 7 — LLM Provider Switching

**Goal:** Verify switching the active LLM provider doesn't break query generation and
both providers produce valid, comparable SQL.

**Setup:** PostgreSQL Full Access · start with Provider A (e.g. Groq) · Auto-Execute

> ⚠️ **Cache isolation required.** Sending the same question twice will return a cache
> hit for Provider B, bypassing its SQL generation entirely. Step 4 invalidates the
> cached response before switching providers.

**Steps**

1. Open a new chat session with **Provider A** selected.
2. Send: `Which customers have spent more than €3,000 total and haven't ordered in the last 60 days?`
3. Note the SQL generated and row count. Verify the status badge shows **Executed** (not Cached).
4. Click the **thumbs-down** button on the step 2 response to invalidate the cache entry.
5. Switch the active provider to **Provider B** (e.g. Gemini).
6. Open a new session.
7. Send the identical question.
8. Verify the status badge shows **Executed** (not Cached) — confirming Provider B generated fresh SQL.
9. Compare SQL and row count to step 3.

**Pass Criteria**

- Both providers return valid, executable SQL.
- Step 8 badge shows **Executed**, not **Cached** — Provider B actually ran SQL generation.
- Row counts are equal (or differ by ≤2 rows due to floating-point rounding).
- No 500 errors when switching providers.
- Provider label in response metadata matches the active provider.

**Watch For**

- Step 8 returning a cache badge — thumbs-down in step 4 may not have invalidated correctly.
- One provider referencing non-existent columns or wrong table aliases.
- Provider health-check failures — check `/api/providers/test`.

---

## Session 8 — Cache Hit Behaviour

**Goal:** Confirm a repeated query returns a cached result, and thumbs-down feedback
invalidates the cache.

**Setup:** PostgreSQL Full Access · any provider · Auto-Execute

**Steps**

1. Open a new chat session.
2. Send: `Show me the top 10 best-selling products by total revenue with their profit margin.`
3. Note the response time and status badge.
4. Open a second new session (same connection, same provider).
5. Send the exact same query.
6. Compare response time — should be faster; badge should show a cache indicator.
7. Click the **thumbs-down** feedback button on the cached response.
8. Open a third new session.
9. Send the same query again.
10. Verify the cache is no longer used (response time similar to step 3, no cache badge).

**Pass Criteria**

- Step 5 response is noticeably faster than step 3 with a cache-hit badge.
- After thumbs-down, step 9 re-executes without a cache hit.
- SQL and results in step 5 match step 3 exactly.

**Watch For**

- Cache never hitting — fastembed embedding model may not have loaded (check logs for
  `QueryCache` initialization messages).
- Cache persisting after thumbs-down — verify the feedback endpoint calls cache invalidation.

---

## Session 9 — Semantic Model Influence

**Goal:** Verify that business terms defined in the semantic model are used in generated
SQL — metric expressions, column aliases, and join hints.

**Setup:** PostgreSQL Full Access · any provider · Auto-Execute
**Prerequisite:** Generate a semantic model first (Settings → Semantic Model → Generate)

**Steps**

1. Open the semantic model editor for the PostgreSQL Full Access connection.
2. Confirm a metric entry exists for **Total Revenue** mapped to an expression like
   `SUM(order_items.unit_price * order_items.quantity)`. If not, add one manually.
3. Save and close the semantic model editor.
4. Open a new chat session.
5. Send: `What is the total revenue by product category for this year?`
6. Inspect the generated SQL — it should use the metric expression.
7. Send: `Which customers have the highest lifetime value?`
8. Inspect whether column aliases use the display names from the semantic model.

**Pass Criteria**

- SQL in step 5 uses the `SUM(...)` expression from the semantic model metric definition.
- Column aliases use friendly names (e.g. `total_revenue` not `sum_1`).
- Step 7 returns customer data with semantically named columns.

**Watch For**

- Semantic model not loaded — check that `format_schema_for_llm` includes semantic context
  in the prompt (enable debug logging if needed).
- LLM ignoring the semantic model and using raw column names.

---

## Session 10 — Generate Only Mode

**Goal:** Confirm Generate Only mode produces SQL without executing it, and the SQL
is valid enough to run manually.

**Setup:** PostgreSQL Full Access · any provider · **Generate Only** (change in connection settings before starting)

> ⚠️ **Cache isolation required.** Both questions below also appear in Session 3
> (Review First). If Session 3 ran earlier in the same test run, these queries will
> return cache hits — and a **Cached** badge is indistinguishable from a **Generated**
> badge in terms of execution mode. Before starting, ensure neither question has a live
> cache entry: if Session 3 was run, use thumbs-down there to invalidate both responses,
> or use fresh questions not asked in Session 3.

**Steps**

1. Open a new chat session (Generate Only mode active).
2. Send: `Show me all employees earning more than €80,000 sorted by department.`
3. Verify the status badge shows **Generated** (not Executed or **Cached**).
4. Verify the response shows SQL only — no results table.
5. Copy the SQL and run it directly against `savvina_test` (e.g. via psql or a DB client).
6. Verify it executes without errors and returns rows.
7. Send: `Which marketing campaign generated the highest return on investment?`
8. Verify the status badge again shows **Generated** (not Cached).
9. Copy and manually execute the SQL.
10. Verify it executes without errors.

**Pass Criteria**

- Responses show SQL with no results table.
- Status badge shows **Generated** (not Executed or Cached) for both queries.
- Copied SQL executes without syntax errors when run directly.
- App does not accidentally execute the query.

**Watch For**

- Badge showing **Cached** instead of **Generated** — invalidate the Session 3 cache entries and re-run.
- App executing SQL despite Generate Only mode — check execution mode logic in `chat_service.py`.
- Generated SQL with missing schema-qualified names or placeholder values.

---

