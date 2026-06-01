-- ============================================================
-- Savvina AI — MySQL Read-Only Analyst User
-- ============================================================
-- Creates savvina_analyst with SELECT access to non-PII tables.
--
-- Accessible:  restaurants, menu_items, orders, order_items,
--              reviews, v_restaurant_stats, v_driver_performance
-- Blocked:     customers (email, phone PII)
--              drivers   (email, phone PII)
-- ============================================================

USE sample_delivery;

-- ── Create user ───────────────────────────────────────────────────────────────

CREATE USER IF NOT EXISTS 'savvina_analyst'@'%'
    IDENTIFIED BY 'analyst_readonly_2024';

-- ── Grant SELECT on non-PII tables ───────────────────────────────────────────

GRANT SELECT ON sample_delivery.restaurants  TO 'savvina_analyst'@'%';
GRANT SELECT ON sample_delivery.menu_items   TO 'savvina_analyst'@'%';
GRANT SELECT ON sample_delivery.orders       TO 'savvina_analyst'@'%';
GRANT SELECT ON sample_delivery.order_items  TO 'savvina_analyst'@'%';
GRANT SELECT ON sample_delivery.reviews      TO 'savvina_analyst'@'%';

-- customers and drivers intentionally excluded — contain email and phone PII.
-- The analyst can still aggregate orders → restaurants without touching PII.

-- ── Grant SELECT on views (conditional — views may not exist on first run) ────

DROP PROCEDURE IF EXISTS _grant_view_if_exists;

DELIMITER $$
CREATE PROCEDURE _grant_view_if_exists(
    IN p_view VARCHAR(64),
    IN p_user VARCHAR(64)
)
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA = 'sample_delivery'
          AND TABLE_NAME   = p_view
    ) THEN
        SET @sql = CONCAT(
            'GRANT SELECT ON sample_delivery.`', p_view,
            '` TO ''', p_user, '''@''%'''
        );
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

CALL _grant_view_if_exists('v_restaurant_stats',  'savvina_analyst');
CALL _grant_view_if_exists('v_driver_performance', 'savvina_analyst');
CALL _grant_view_if_exists('v_customer_stats',     'savvina_analyst');
CALL _grant_view_if_exists('v_menu_performance',   'savvina_analyst');

DROP PROCEDURE IF EXISTS _grant_view_if_exists;

-- ── Grant SELECT on new non-PII tables ───────────────────────────────────────
-- promotions: restaurant-level, no PII
GRANT SELECT ON sample_delivery.promotions       TO 'savvina_analyst'@'%';
-- restaurant_hours: operational schedule, no PII
GRANT SELECT ON sample_delivery.restaurant_hours TO 'savvina_analyst'@'%';
-- driver_earnings: financial metrics only; driver_id FK but drivers table blocked
GRANT SELECT ON sample_delivery.driver_earnings  TO 'savvina_analyst'@'%';

-- customer_loyalty: intentionally excluded — customer_id links to PII (email, phone).
-- The analyst can use the v_customer_stats view for aggregate loyalty data instead.

FLUSH PRIVILEGES;

-- ── Summary ───────────────────────────────────────────────────────────────────
SELECT 'Read-only user ready'  AS status,
       'savvina_analyst'      AS username,
       'analyst_readonly_2024' AS password_hint,
       'sample_delivery'       AS `database`;
