-- ============================================================
-- Savvina AI — MySQL Sample Database
-- Domain: Food Delivery Platform
-- Tables: restaurants, customers, drivers, menu_items,
--         orders, order_items, reviews
-- Views:  v_restaurant_stats, v_driver_performance
--
-- Connection details (read-only analyst):
--   host: sample-mysql   port: 3306
--   user: savvina_analyst   password: analyst_readonly_2024
--   database: sample_delivery
--
-- Sample questions to try:
--   "Which cuisine types generate the most revenue?"
--   "Show me the top 10 restaurants by average review rating"
--   "How many orders are in each status?"
--   "Which drivers have completed the most deliveries?"
--   "What is the most popular menu category by order volume?"
--   "Show monthly order counts for the last 6 months"
--   "Which cities have the highest average order value?"
--   "What percentage of orders were cancelled by restaurant?"
--   "Show me the top 5 menu items ordered this year"
--   "Which payment method is most common for high-value orders?"
-- ============================================================

USE sample_delivery;

-- Allow deep recursion for data generation CTEs
SET SESSION cte_max_recursion_depth = 5000;

-- ── Tables ────────────────────────────────────────────────────────────────────

CREATE TABLE restaurants (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(150) NOT NULL                                          COMMENT 'Restaurant display name',
    cuisine_type ENUM('Italian','Asian','Mexican','American','Mediterranean',
                      'Indian','Japanese','Greek','French','Middle Eastern')
                 NOT NULL                                                       COMMENT 'Cuisine category',
    status       ENUM('active','inactive','suspended') NOT NULL DEFAULT 'active' COMMENT 'active=accepting orders, suspended=under review',
    city         VARCHAR(100),
    country      VARCHAR(50),
    rating       DECIMAL(3,2) DEFAULT 0.00                                     COMMENT 'Platform average rating 0–5',
    review_count INT          DEFAULT 0,
    created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP
) COMMENT = 'Partner restaurants registered on the platform';

CREATE TABLE customers (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    email      VARCHAR(150) UNIQUE                                              COMMENT 'Login email — PII',
    phone      VARCHAR(20)                                                      COMMENT 'Contact phone — PII',
    city       VARCHAR(100),
    country    VARCHAR(50),
    is_active  BOOLEAN  DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) COMMENT = 'Registered customer accounts';

CREATE TABLE drivers (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(100) NOT NULL,
    email            VARCHAR(150) UNIQUE                                        COMMENT 'Driver login email — PII',
    phone            VARCHAR(20)                                                COMMENT 'Driver phone — PII',
    vehicle_type     ENUM('bike','scooter','car') NOT NULL                      COMMENT 'bike=pedal, scooter=motor, car=auto',
    status           ENUM('available','busy','offline') DEFAULT 'offline'       COMMENT 'Current availability',
    city             VARCHAR(100),
    total_deliveries INT          DEFAULT 0,
    rating           DECIMAL(3,2) DEFAULT 0.00,
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP
) COMMENT = 'Delivery driver accounts';

CREATE TABLE menu_items (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    restaurant_id INT          NOT NULL,
    name          VARCHAR(200) NOT NULL,
    category      ENUM('starter','main','dessert','drink','side') NOT NULL      COMMENT 'Menu section',
    price         DECIMAL(8,2) NOT NULL,
    is_available  BOOLEAN DEFAULT TRUE                                          COMMENT 'Currently orderable',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
) COMMENT = 'Restaurant menu items';

CREATE TABLE orders (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    customer_id   INT NOT NULL,
    restaurant_id INT NOT NULL,
    driver_id     INT,
    order_date    DATE     NOT NULL,
    status        ENUM('placed','confirmed','preparing',
                       'out_for_delivery','delivered','cancelled')
                  NOT NULL DEFAULT 'placed'                                     COMMENT 'Order lifecycle status',
    total_amount  DECIMAL(10,2),
    delivery_fee  DECIMAL(6,2) DEFAULT 2.99,
    payment_method ENUM('card','cash','wallet') NOT NULL                        COMMENT 'card=credit/debit, wallet=in-app balance',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id)   REFERENCES customers(id),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id),
    FOREIGN KEY (driver_id)     REFERENCES drivers(id)
) COMMENT = 'Customer delivery orders';

CREATE TABLE order_items (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    order_id     INT          NOT NULL,
    menu_item_id INT          NOT NULL,
    quantity     INT          NOT NULL DEFAULT 1,
    unit_price   DECIMAL(8,2) NOT NULL,
    line_total   DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (order_id)     REFERENCES orders(id),
    FOREIGN KEY (menu_item_id) REFERENCES menu_items(id)
) COMMENT = 'Individual items within an order';

CREATE TABLE reviews (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    order_id      INT     NOT NULL,
    customer_id   INT     NOT NULL,
    restaurant_id INT     NOT NULL,
    rating        TINYINT NOT NULL                                              COMMENT '1–5 star rating',
    comment       TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id)      REFERENCES orders(id),
    FOREIGN KEY (customer_id)   REFERENCES customers(id),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
) COMMENT = 'Customer reviews for completed orders';

-- ── Sample data ───────────────────────────────────────────────────────────────
-- Note: MySQL does not allow WITH RECURSIVE before INSERT INTO.
-- CTEs must appear inside a derived table in the SELECT clause instead.

-- 40 restaurants
INSERT INTO restaurants (name, cuisine_type, status, city, country, rating, review_count)
SELECT
    CONCAT('Restaurant ', n),
    ELT(1 + (n % 10), 'Italian','Asian','Mexican','American','Mediterranean',
                       'Indian','Japanese','Greek','French','Middle Eastern'),
    ELT(1 + (n % 8), 'active','active','active','active','active','active','inactive','suspended'),
    ELT(1 + (n % 6), 'New York','London','Berlin','Tokyo','Sydney','Athens'),
    ELT(1 + (n % 6), 'USA','UK','Germany','Japan','Australia','Greece'),
    ROUND(3.0 + (RAND() * 2.0), 2),
    FLOOR(10 + RAND() * 500)
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 40
    )
    SELECT n FROM seq
) AS t;

-- 150 customers
INSERT INTO customers (name, email, phone, city, country)
SELECT
    CONCAT('Customer ', n),
    CONCAT('customer', n, '@example.com'),
    CONCAT('+1-555-', LPAD(n, 4, '0')),
    ELT(1 + (n % 6), 'New York','London','Berlin','Tokyo','Sydney','Athens'),
    ELT(1 + (n % 6), 'USA','UK','Germany','Japan','Australia','Greece')
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 150
    )
    SELECT n FROM seq
) AS t;

-- 30 drivers
INSERT INTO drivers (name, email, phone, vehicle_type, status, city, total_deliveries, rating)
SELECT
    CONCAT('Driver ', n),
    CONCAT('driver', n, '@example.com'),
    CONCAT('+1-555-9', LPAD(n, 3, '0')),
    ELT(1 + (n % 3), 'bike','scooter','car'),
    ELT(1 + (n % 3), 'available','busy','offline'),
    ELT(1 + (n % 4), 'New York','London','Berlin','Athens'),
    FLOOR(RAND() * 800),
    ROUND(3.5 + RAND() * 1.5, 2)
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 30
    )
    SELECT n FROM seq
) AS t;

-- 200 menu items (5 per restaurant)
INSERT INTO menu_items (restaurant_id, name, category, price, is_available)
SELECT
    1 + ((n - 1) DIV 5),
    CONCAT(ELT(1 + ((n - 1) % 5), 'Starter','Main Dish','Dessert','Drink','Side'), ' ', n),
    ELT(1 + ((n - 1) % 5), 'starter','main','dessert','drink','side'),
    ROUND(4.50 + RAND() * 25.00, 2),
    IF(n % 10 = 0, FALSE, TRUE)
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 200
    )
    SELECT n FROM seq
) AS t;

-- 1 500 orders
INSERT INTO orders (customer_id, restaurant_id, driver_id, order_date, status,
                    total_amount, delivery_fee, payment_method)
SELECT
    1 + (n % 150),
    1 + (n % 40),
    1 + (n % 30),
    DATE_SUB(CURDATE(), INTERVAL FLOOR(RAND() * 365) DAY),
    ELT(1 + (n % 6), 'delivered','delivered','delivered','confirmed','preparing','cancelled'),
    ROUND(12.00 + RAND() * 80.00, 2),
    ROUND(1.99  + RAND() *  4.00, 2),
    ELT(1 + (n % 3), 'card','cash','wallet')
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 1500
    )
    SELECT n FROM seq
) AS t;

-- 3 000 order items (2 per order)
INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price, line_total)
SELECT
    1 + ((n - 1) DIV 2),
    1 + (n % 200),
    1 + (n % 3),
    ROUND(5.00 + RAND() * 20.00, 2),
    ROUND((1 + (n % 3)) * (5.00 + RAND() * 20.00), 2)
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 3000
    )
    SELECT n FROM seq
) AS t;

-- Reviews for ~60% of delivered orders (% 5 != 0 avoids the parity bias that
-- caused even-ID-only sampling to skip Italian restaurants entirely)
INSERT INTO reviews (order_id, customer_id, restaurant_id, rating, comment)
SELECT
    o.id,
    o.customer_id,
    o.restaurant_id,
    1 + (o.id % 5),
    ELT(1 + (o.id % 5), 'Poor experience','Below average','Decent','Good service','Excellent!')
FROM orders o
WHERE o.status = 'delivered'
  AND o.id % 5 != 0;

-- ── Views ─────────────────────────────────────────────────────────────────────

CREATE VIEW v_restaurant_stats AS
SELECT
    r.id,
    r.name,
    r.cuisine_type,
    r.city,
    r.status,
    COUNT(DISTINCT o.id)    AS total_orders,
    COALESCE(SUM(o.total_amount), 0)  AS total_revenue,
    COALESCE(AVG(o.total_amount), 0)  AS avg_order_value,
    COALESCE(AVG(rv.rating), 0)       AS avg_review_rating,
    COUNT(DISTINCT rv.id)   AS review_count
FROM restaurants r
LEFT JOIN orders o  ON r.id = o.restaurant_id AND o.status = 'delivered'
LEFT JOIN reviews rv ON r.id = rv.restaurant_id
GROUP BY r.id, r.name, r.cuisine_type, r.city, r.status;

CREATE VIEW v_driver_performance AS
SELECT
    d.id,
    d.name,
    d.vehicle_type,
    d.city,
    d.status,
    COUNT(o.id)              AS completed_deliveries,
    COALESCE(SUM(o.delivery_fee), 0)  AS total_fees_earned,
    d.rating                 AS driver_rating
FROM drivers d
LEFT JOIN orders o ON d.id = o.driver_id AND o.status = 'delivered'
GROUP BY d.id, d.name, d.vehicle_type, d.city, d.status, d.rating;

-- ── Additional Tables ─────────────────────────────────────────────────────────

CREATE TABLE promotions (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    restaurant_id    INT NOT NULL,
    name             VARCHAR(150) NOT NULL,
    promo_type       ENUM('percentage','fixed','free_delivery') NOT NULL            COMMENT 'percentage=% off, fixed=flat amount off, free_delivery=waived fee',
    discount_value   DECIMAL(8,2)                                                   COMMENT 'NULL for free_delivery type',
    min_order_amount DECIMAL(8,2) NOT NULL DEFAULT 0.00,
    start_date       DATE NOT NULL,
    end_date         DATE NOT NULL,
    status           ENUM('active','expired','scheduled') NOT NULL DEFAULT 'scheduled',
    uses_count       INT NOT NULL DEFAULT 0,
    max_uses         INT                                                             COMMENT 'NULL = unlimited',
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
) COMMENT = 'Restaurant promotional campaigns';

CREATE TABLE driver_earnings (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    driver_id         INT NOT NULL,
    order_id          INT NOT NULL UNIQUE,
    base_fee          DECIMAL(8,2) NOT NULL                                          COMMENT 'Platform delivery fee paid to driver',
    tip_amount        DECIMAL(8,2) NOT NULL DEFAULT 0.00,
    bonus_amount      DECIMAL(8,2) NOT NULL DEFAULT 0.00                            COMMENT 'Distance or performance bonus',
    total_earned      DECIMAL(8,2) GENERATED ALWAYS AS (base_fee + tip_amount + bonus_amount) STORED,
    distance_km       DECIMAL(6,2)                                                   COMMENT 'Approximate delivery distance',
    delivery_time_min INT                                                            COMMENT 'Actual door-to-door time in minutes',
    earned_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (driver_id) REFERENCES drivers(id),
    FOREIGN KEY (order_id)  REFERENCES orders(id)
) COMMENT = 'Per-delivery earnings breakdown for drivers';

CREATE TABLE customer_loyalty (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    customer_id      INT NOT NULL,
    transaction_type ENUM('earn','redeem','expire','adjust') NOT NULL               COMMENT 'earn=points gained, redeem=points spent',
    points           INT NOT NULL                                                   COMMENT 'Positive for earn, negative for redeem/expire',
    balance_after    INT NOT NULL,
    order_id         INT                                                             COMMENT 'Source order; NULL for non-order transactions',
    description      VARCHAR(200),
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (order_id)    REFERENCES orders(id)
) COMMENT = 'Customer loyalty points ledger';

CREATE TABLE restaurant_hours (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    restaurant_id   INT NOT NULL,
    day_of_week     TINYINT NOT NULL                                                COMMENT '0=Sunday 1=Monday 2=Tuesday 3=Wednesday 4=Thursday 5=Friday 6=Saturday',
    open_time       TIME                                                            COMMENT 'NULL when is_closed = TRUE',
    close_time      TIME                                                            COMMENT 'NULL when is_closed = TRUE',
    is_closed       BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE KEY uq_restaurant_day (restaurant_id, day_of_week),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
) COMMENT = 'Restaurant hours of operation by day of week';

-- ── Seed: promotions (60 rows — 1–2 per restaurant) ──────────────────────────
INSERT INTO promotions (restaurant_id, name, promo_type, discount_value, min_order_amount,
                        start_date, end_date, status, uses_count, max_uses)
SELECT
    1 + ((n - 1) % 40),
    CONCAT(ELT(1 + (n % 5), 'Happy Hour', 'Weekend Special', 'Lunch Deal', 'Family Combo', 'Flash Sale'), ' #', n),
    ELT(1 + (n % 3), 'percentage', 'fixed', 'free_delivery'),
    CASE n % 3
        WHEN 0 THEN ROUND(10 + (n % 21), 0)   -- 10–30 %
        WHEN 1 THEN ROUND(3  + (n % 8),  2)   -- €3–10 fixed
        ELSE   NULL                            -- free_delivery has no value
    END,
    ELT(1 + (n % 4), 20.00, 25.00, 30.00, 15.00),
    DATE_SUB(CURDATE(), INTERVAL (n * 3) DAY),
    DATE_ADD(CURDATE(), INTERVAL ((61 - n) * 3) DAY),
    ELT(1 + (n % 3), 'active', 'active', 'expired'),
    (n % 200) + FLOOR(RAND() * 50),
    CASE WHEN n % 5 = 0 THEN NULL ELSE 150 + (n * 7) % 400 END
FROM (
    WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 60)
    SELECT n FROM seq
) AS t;

-- ── Seed: restaurant_hours (7 days × 40 restaurants = 280 rows) ──────────────
INSERT INTO restaurant_hours (restaurant_id, day_of_week, open_time, close_time, is_closed)
SELECT
    r.id,
    d.dow,
    CASE
        WHEN r.status = 'inactive'           THEN NULL
        WHEN d.dow = 0                       THEN '11:00:00'
        WHEN d.dow = 6                       THEN '09:30:00'
        ELSE                                      '09:00:00'
    END,
    CASE
        WHEN r.status = 'inactive'           THEN NULL
        WHEN d.dow IN (5, 6)                 THEN '23:30:00'
        ELSE                                      '22:30:00'
    END,
    CASE
        WHEN r.status = 'inactive'           THEN TRUE
        WHEN d.dow = 0 AND r.id % 6 = 0     THEN TRUE   -- ~17 % of restaurants closed Sunday
        WHEN d.dow = 1 AND r.id % 10 = 0    THEN TRUE   -- ~10 % closed Monday
        ELSE                                     FALSE
    END
FROM restaurants r
CROSS JOIN (
    WITH RECURSIVE dow_seq(dow) AS (SELECT 0 UNION ALL SELECT dow + 1 FROM dow_seq WHERE dow < 6)
    SELECT dow FROM dow_seq
) AS d;

-- ── Seed: driver_earnings (one row per delivered order with a driver) ─────────
INSERT INTO driver_earnings (driver_id, order_id, base_fee, tip_amount, bonus_amount,
                             distance_km, delivery_time_min, earned_at)
SELECT
    o.driver_id,
    o.id,
    o.delivery_fee,
    ROUND(IF(o.total_amount > 50, 2.0 + (o.id % 5) * 0.5, (o.id % 4) * 0.5), 2),
    ROUND(IF(o.total_amount > 70, 1.0 + (o.id % 3) * 0.5, 0.0), 2),
    ROUND(1.5 + (o.id % 15), 2),
    10 + (o.id % 45),
    o.created_at
FROM orders o
WHERE o.status = 'delivered'
  AND o.driver_id IS NOT NULL;

-- ── Seed: customer_loyalty — earn on delivered orders, redeem for top earners ──
-- Step 1: earn events with running balance per customer (window function)
INSERT INTO customer_loyalty (customer_id, transaction_type, points, balance_after,
                              order_id, description, created_at)
SELECT
    customer_id,
    'earn',
    pts,
    SUM(pts) OVER (PARTITION BY customer_id ORDER BY ord_date, order_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS balance_after,
    order_id,
    CONCAT('Earned for order #', order_id),
    ord_date
FROM (
    SELECT
        o.customer_id,
        o.id         AS order_id,
        o.order_date AS ord_date,
        FLOOR(o.total_amount) AS pts
    FROM orders o
    WHERE o.status = 'delivered'
) AS earn_src;

-- Step 2: redeem 100 points for customers who have accumulated > 200
INSERT INTO customer_loyalty (customer_id, transaction_type, points, balance_after,
                              order_id, description, created_at)
SELECT
    customer_id,
    'redeem',
    -100,
    max_bal - 100,
    NULL,
    'Redeemed 100 points for €5 discount',
    DATE_ADD(last_earn, INTERVAL 7 DAY)
FROM (
    SELECT customer_id, MAX(balance_after) AS max_bal, MAX(created_at) AS last_earn
    FROM   customer_loyalty
    WHERE  transaction_type = 'earn'
    GROUP  BY customer_id
    HAVING MAX(balance_after) > 200
) AS redeemers
LIMIT 60;

-- ── View: v_customer_stats (aggregate only — no PII email/phone) ──────────────
CREATE VIEW v_customer_stats AS
SELECT
    c.id                                        AS customer_id,
    c.city,
    c.country,
    c.is_active,
    COUNT(DISTINCT o.id)                        AS total_orders,
    COALESCE(SUM(o.total_amount), 0)            AS total_spent,
    COALESCE(AVG(o.total_amount), 0)            AS avg_order_value,
    MAX(o.order_date)                           AS last_order_date,
    DATEDIFF(CURDATE(), MAX(o.order_date))      AS days_since_last_order,
    COALESCE((
        SELECT SUM(cl.points)
        FROM   customer_loyalty cl
        WHERE  cl.customer_id = c.id
    ), 0)                                       AS loyalty_points_balance,
    COUNT(DISTINCT rv.id)                       AS reviews_written
FROM customers c
LEFT JOIN orders   o  ON c.id = o.customer_id AND o.status = 'delivered'
LEFT JOIN reviews  rv ON c.id = rv.customer_id
GROUP BY c.id, c.city, c.country, c.is_active;

-- ── View: v_menu_performance ──────────────────────────────────────────────────
CREATE VIEW v_menu_performance AS
SELECT
    mi.id                                       AS menu_item_id,
    mi.name                                     AS item_name,
    mi.category,
    mi.price                                    AS list_price,
    r.id                                        AS restaurant_id,
    r.name                                      AS restaurant_name,
    r.cuisine_type,
    r.city,
    COUNT(oi.id)                                AS times_ordered,
    COALESCE(SUM(oi.quantity), 0)               AS total_quantity_sold,
    COALESCE(SUM(oi.line_total), 0)             AS total_revenue,
    COALESCE(AVG(oi.unit_price), mi.price)      AS avg_unit_price,
    mi.is_available
FROM menu_items mi
JOIN  restaurants  r  ON mi.restaurant_id = r.id
LEFT JOIN order_items oi ON mi.id = oi.menu_item_id
LEFT JOIN orders      o  ON oi.order_id = o.id AND o.status = 'delivered'
GROUP BY mi.id, mi.name, mi.category, mi.price, r.id, r.name, r.cuisine_type, r.city, mi.is_available;
