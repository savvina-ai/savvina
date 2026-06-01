-- ============================================================
-- Savvina AI — Read-Only Analyst User
-- ============================================================
-- This file creates a limited-permission user that mirrors what
-- a real Savvina deployment should use:
--   - Can SELECT on ecommerce, analytics, inventory schemas
--   - Can SELECT on support (no PII visibility — hr/finance excluded)
--   - CANNOT access hr.employees salaries or finance.transactions
--   - CANNOT INSERT, UPDATE, DELETE, DROP anything
--   - CANNOT see sensitive columns via row-level security
-- ============================================================

-- ── 1. Create the read-only user ─────────────────────────────────────────────

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT FROM pg_catalog.pg_roles WHERE rolname = 'savvina_analyst'
  ) THEN
    CREATE ROLE savvina_analyst LOGIN PASSWORD 'analyst_readonly_2024';
    RAISE NOTICE 'Role savvina_analyst created.';
  ELSE
    RAISE NOTICE 'Role savvina_analyst already exists, skipping creation.';
  END IF;
END
$$;

-- ── 2. Grant connect to the database ─────────────────────────────────────────

GRANT CONNECT ON DATABASE savvina_test TO savvina_analyst;

-- ── 3. Grant USAGE on allowed schemas ────────────────────────────────────────
-- hr and finance are intentionally excluded at schema level

GRANT USAGE ON SCHEMA ecommerce  TO savvina_analyst;
GRANT USAGE ON SCHEMA inventory  TO savvina_analyst;
GRANT USAGE ON SCHEMA analytics  TO savvina_analyst;
GRANT USAGE ON SCHEMA support    TO savvina_analyst;

-- NO GRANT on hr schema        → employees, salaries invisible
-- NO GRANT on finance schema   → transactions, invoices invisible

-- ── 4. Grant SELECT on specific tables per schema ────────────────────────────

-- ecommerce: full read access (no PII tables)
GRANT SELECT ON ecommerce.categories   TO savvina_analyst;
GRANT SELECT ON ecommerce.brands       TO savvina_analyst;
GRANT SELECT ON ecommerce.products     TO savvina_analyst;
GRANT SELECT ON ecommerce.orders       TO savvina_analyst;
GRANT SELECT ON ecommerce.order_items  TO savvina_analyst;
GRANT SELECT ON ecommerce.reviews      TO savvina_analyst;
GRANT SELECT ON ecommerce.coupons      TO savvina_analyst;
-- ecommerce.customers: EXCLUDED — contains PII (email, phone, dob)

-- inventory: full read access
GRANT SELECT ON inventory.warehouses        TO savvina_analyst;
GRANT SELECT ON inventory.stock             TO savvina_analyst;
GRANT SELECT ON inventory.stock_movements   TO savvina_analyst;
GRANT SELECT ON inventory.suppliers         TO savvina_analyst;
GRANT SELECT ON inventory.purchase_orders   TO savvina_analyst;

-- support: tickets and metrics only — no messages (may contain PII)
GRANT SELECT ON support.sla_policies    TO savvina_analyst;
GRANT SELECT ON support.tickets         TO savvina_analyst;
GRANT SELECT ON support.agents          TO savvina_analyst;
-- support.ticket_messages: EXCLUDED — free-text, may contain PII

-- analytics: full read access (no raw PII)
GRANT SELECT ON analytics.marketing_campaigns TO savvina_analyst;
GRANT SELECT ON analytics.ab_tests            TO savvina_analyst;
GRANT SELECT ON analytics.cart_events         TO savvina_analyst;
-- analytics.page_views: EXCLUDED — contains session tracking data

-- ecommerce.wishlists: EXCLUDED — customer_id FK links to PII customers table
-- hr.training_records: EXCLUDED — hr schema not accessible
-- finance.expense_reports: EXCLUDED — finance schema not accessible

-- ── 5. Grant SELECT on views (safe aggregated data only) ─────────────────────

GRANT SELECT ON analytics.vw_sales_summary       TO savvina_analyst;
GRANT SELECT ON analytics.vw_product_performance  TO savvina_analyst;
GRANT SELECT ON analytics.vw_cart_funnel          TO savvina_analyst;
GRANT SELECT ON inventory.vw_stock_alerts         TO savvina_analyst;
GRANT SELECT ON support.vw_ticket_metrics         TO savvina_analyst;

-- vw_customer_ltv: EXCLUDED — contains customer email and personal data
-- hr.vw_headcount_summary: EXCLUDED — hr schema not accessible
-- hr.vw_training_completion: EXCLUDED — hr schema not accessible

-- ── 6. Grant SELECT on sequences (needed for serial column reads) ─────────────

GRANT SELECT ON ALL SEQUENCES IN SCHEMA ecommerce  TO savvina_analyst;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA inventory  TO savvina_analyst;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA analytics  TO savvina_analyst;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA support    TO savvina_analyst;

-- ── 7. Lock down pg_catalog visibility for sensitive system info ──────────────

-- Restrict pg_stat_user_tables and pg_stats access (prevents cardinality snooping)
-- The analyst user can read schema structure but NOT table statistics
REVOKE ALL ON pg_catalog.pg_statistic FROM savvina_analyst;

-- ── 8. Row-Level Security on support.tickets ─────────────────────────────────
-- The analyst cannot see tickets marked as internal fraud investigations

ALTER TABLE support.tickets ENABLE ROW LEVEL SECURITY;

-- Policy: savvina (owner) sees everything
CREATE POLICY tickets_owner_all ON support.tickets
  FOR ALL
  TO savvina
  USING (true);

-- Policy: analyst sees all tickets EXCEPT fraud category
CREATE POLICY tickets_analyst_no_fraud ON support.tickets
  FOR SELECT
  TO savvina_analyst
  USING (category != 'fraud');

-- ── 9. Create a restricted view for order data (no customer PII) ─────────────

CREATE OR REPLACE VIEW ecommerce.vw_orders_no_pii AS
SELECT
  o.id,
  o.order_number,
  o.status,
  o.payment_status,
  o.payment_method,
  o.subtotal,
  o.discount_amount,
  o.shipping_amount,
  o.tax_amount,
  o.total_amount,
  o.currency,
  o.shipping_country,
  o.shipping_city,        -- city is fine; no street address
  o.coupon_code,
  o.ordered_at,
  o.shipped_at,
  o.delivered_at,
  o.cancelled_at,
  -- customer segment instead of customer_id (prevents JOIN to customers PII)
  CASE
    WHEN c.is_vip THEN 'vip'
    WHEN c.total_orders > 10 THEN 'loyal'
    WHEN c.total_orders > 3  THEN 'returning'
    ELSE 'new'
  END AS customer_segment,
  c.country AS customer_country
FROM ecommerce.orders o
JOIN ecommerce.customers c ON o.customer_id = c.id;

GRANT SELECT ON ecommerce.vw_orders_no_pii TO savvina_analyst;

-- ── 10. Summary notice ───────────────────────────────────────────────────────

DO $$
BEGIN
  RAISE NOTICE '============================================';
  RAISE NOTICE 'Read-only user created: savvina_analyst';
  RAISE NOTICE 'Password: analyst_readonly_2024';
  RAISE NOTICE '--------------------------------------------';
  RAISE NOTICE 'Accessible schemas: ecommerce, inventory, analytics, support';
  RAISE NOTICE 'Blocked schemas: hr (salaries), finance (transactions)';
  RAISE NOTICE 'Blocked tables: customers (PII), ticket_messages, page_views';
  RAISE NOTICE 'RLS active: support.tickets (fraud category hidden)';
  RAISE NOTICE '============================================';
END $$;
