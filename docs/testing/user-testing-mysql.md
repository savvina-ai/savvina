# MySQL Testing

Connections, sample questions, and QA sessions for the MySQL / MariaDB adapter.

See [user-testing-playbook.md](user-testing-playbook.md) for prerequisites and the full session index.

---

## Sample Database — `sample-mysql`

Seeds `sample_delivery` — a food delivery platform — with MySQL-specific features:
ENUM columns, inline column comments, AUTO_INCREMENT PKs, and MySQL 8 recursive CTEs for data generation.

| Table | Contents |
|-------|----------|
| `restaurants` | 40 partner restaurants with `cuisine_type` ENUM and `status` ENUM |
| `customers` | 150 customers (PII: email, phone) |
| `drivers` | 30 delivery drivers with `vehicle_type` and `status` ENUMs (PII: email, phone) |
| `menu_items` | 200 items with `category` ENUM (`starter`, `main`, `dessert`, `drink`, `side`) |
| `orders` | 1,500 orders with `status` and `payment_method` ENUMs |
| `order_items` | 3,000 line items |
| `reviews` | ~375 reviews (TINYINT rating 1–5) |
| `promotions` | 60 promotional campaigns with `promo_type` ENUM and validity windows |
| `driver_earnings` | Per-delivery earnings: base fee, tip, bonus, distance, time (generated `total_earned`) |
| `customer_loyalty` | Loyalty points ledger with `transaction_type` ENUM and running balance |
| `restaurant_hours` | 280 rows — opening hours per day of week (0=Sun … 6=Sat) per restaurant |
| `v_restaurant_stats` | View — revenue, order count, avg rating per restaurant |
| `v_driver_performance` | View — deliveries, fees earned per driver |
| `v_customer_stats` | View — aggregate customer metrics (orders, spend, loyalty balance) — no PII |
| `v_menu_performance` | View — menu item popularity, quantity sold, and revenue |

| Role | Access |
|------|--------|
| `savvina` | Full access to `sample_delivery` |
| `savvina_analyst` | SELECT on non-PII tables and all views; `customers`, `drivers`, `customer_loyalty` blocked |

---

## Connection 6 — MySQL · Food Delivery (Full Access)

**Use case:** MySQL/MariaDB adapter smoke test. Exercises ENUM sample values, backtick
quoting in generated SQL, AUTO_INCREMENT PKs, and MySQL-specific syntax (`DATE_FORMAT`,
`GROUP_CONCAT`, `IF()`). Use the analyst user to test the restricted view.

| Setting  | Value            |
|----------|------------------|
| Adapter  | MySQL / MariaDB  |
| Host     | sample-mysql     |
| Port     | 3306             |
| Database | sample_delivery  |
| Username | savvina_analyst |
| Password | *(value of `SAMPLE_MYSQL_PASSWORD` in your `.env`)* |
| SSL      | off              |

**Accessible:** `restaurants`, `menu_items`, `orders`, `order_items`, `reviews`,
`promotions`, `restaurant_hours`, `driver_earnings`,
`v_restaurant_stats`, `v_driver_performance`, `v_customer_stats`, `v_menu_performance`
**Blocked:** `customers`, `drivers`, `customer_loyalty` (PII — email, phone; or links to PII)

### Sample Questions

1. Which cuisine types generate the most total revenue?
2. Show me the top 10 restaurants by average review rating.
3. How many orders are in each status?
4. Which drivers have completed the most deliveries?
5. What is the most popular menu category by order volume?
6. Show monthly order counts for the last 6 months.
7. Which cities have the highest average order value?
8. What percentage of orders were cancelled?
9. Show me the top 5 menu items by quantity ordered this year.
10. Which payment method is most common for orders over $50?

### Extended Questions

1. What is the revenue breakdown by cuisine type and city?
2. Show me restaurants with an average rating above 4.0 and more than 50 orders.
3. Which drivers have a rating above 4.5 and more than 100 completed deliveries?
4. What is the weekly order volume trend for the last 8 weeks?
5. Show me the menu categories with the highest average item price per restaurant.
6. Which restaurants have a cancellation rate above 20%?
7. What is the average delivery fee by city?
8. Show the month-over-month revenue growth rate for the top 5 restaurants.

### Advanced Questions *(window functions, ranking, CTEs)*

1. Rank restaurants within each cuisine type by total revenue — show the rank, restaurant name, cuisine, and revenue.
2. For each restaurant, show the running total of orders placed over time, ordered by order date.
3. Which menu items are in the top 3 by quantity ordered within their category?
4. Show each restaurant's revenue as a percentage of the total revenue for its cuisine type.
5. For each city, show the restaurant with the single highest revenue — one row per city.
6. Show the 3-month rolling average order value per cuisine type.
7. Compare each restaurant's average order value against the overall platform average — show the difference and whether it is above or below average.
8. For each order, show the previous order's total amount for the same restaurant, and the difference between them.
9. What is the cumulative revenue per cuisine type, ordered by month?
10. Identify restaurants whose order volume in the most recent month is less than half of their peak month — show the peak month, current month, and the drop percentage.
11. Rank drivers by total fees earned within each city, and return only those ranked 1st or 2nd.
12. For each menu category, show which restaurant contributes the highest share of orders in that category.

> **Permission boundary test:** Sending "List all customer emails" or "Show me driver
> phone numbers" should return an error — `customers` and `drivers` are not accessible
> for this role. Asking "Show me all loyalty point transactions with customer names" should
> also fail — `customer_loyalty` is blocked.

---

## Session 11 — MySQL Food Delivery

**Goal:** Validate the MySQL adapter end-to-end — connection, schema introspection, ENUM
sample value extraction, backtick-quoted SQL generation, and the permission boundary for
the analyst role.

**Setup:** MySQL Food Delivery (Connection 6) · any provider · Auto-Execute

**Steps**

1. Add Connection 6 (MySQL analyst user) and trigger schema introspection.
2. Open the schema viewer and confirm ENUM columns show sample values
   (e.g. `cuisine_type` shows `Italian, Asian, Mexican…`; `status` shows `placed, confirmed…`).
3. Open a new chat session.
4. Send: `Which cuisine types generate the most total revenue?`
5. Verify generated SQL uses backtick quoting (`` `orders` ``, `` `cuisine_type` ``) and
   `GROUP BY` with `SUM`.
6. Send: `Show monthly order counts for the last 6 months.`
7. Verify SQL uses `DATE_FORMAT()` or `DATE_SUB()` — not PostgreSQL `DATE_TRUNC`.
8. Send: `Which drivers have completed the most deliveries?`
9. Verify SQL queries `v_driver_performance` (or `orders` with driver join) and returns rows.
10. Send: `List all customer emails.`
11. Verify the response returns an error or empty result — `customers` table is blocked.

**Pass Criteria**

- Schema viewer shows ENUM sample values without any table scan queries in logs.
- All generated SQL uses backtick quoting, not double-quote PostgreSQL style.
- Date functions are MySQL-flavoured (`DATE_FORMAT`, `MONTH()`, `YEAR()`).
- Steps 4, 6, 8 return non-empty result tables.
- Step 10 returns a graceful error — no crash, no data from the `customers` table.

**Watch For**

- Generated SQL using `"table_name"` (PostgreSQL style) instead of `` `table_name` `` — the
  system prompt addition in the adapter controls this; check `get_system_prompt_additions()`.
- ENUM sample values missing — check that `introspect()` parsed `COLUMN_TYPE` correctly for
  `ENUM(...)` columns.
- `customers` or `drivers` table accessible despite the analyst grant restrictions — verify
  `02_readonly_user.sql` ran during container initialisation (check `docker compose logs sample-mysql`).

