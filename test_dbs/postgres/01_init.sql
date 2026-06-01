-- ============================================================
-- Savvina AI — Test Database
-- Complex multi-schema PostgreSQL database for product testing
-- Schemas: ecommerce, hr, finance, analytics, inventory, support
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gin";

-- ============================================================
-- SCHEMA CREATION
-- ============================================================
CREATE SCHEMA IF NOT EXISTS ecommerce;
CREATE SCHEMA IF NOT EXISTS hr;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS inventory;
CREATE SCHEMA IF NOT EXISTS support;
CREATE SCHEMA IF NOT EXISTS analytics;

-- ============================================================
-- HR SCHEMA
-- ============================================================

CREATE TABLE hr.departments (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    code            VARCHAR(10) UNIQUE NOT NULL,
    budget          NUMERIC(15,2),
    head_count_max  INT,
    location        VARCHAR(100),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE hr.employees (
    id              SERIAL PRIMARY KEY,
    employee_code   VARCHAR(20) UNIQUE NOT NULL,
    first_name      VARCHAR(50) NOT NULL,
    last_name       VARCHAR(50) NOT NULL,
    email           VARCHAR(100) UNIQUE NOT NULL,
    phone           VARCHAR(20),
    department_id   INT REFERENCES hr.departments(id),
    manager_id      INT REFERENCES hr.employees(id),
    job_title       VARCHAR(100),
    employment_type VARCHAR(20) CHECK (employment_type IN ('full_time','part_time','contractor','intern')),
    salary          NUMERIC(12,2),
    hire_date       DATE NOT NULL,
    termination_date DATE,
    is_active       BOOLEAN DEFAULT TRUE,
    date_of_birth   DATE,
    gender          VARCHAR(10),
    nationality     VARCHAR(50),
    office_location VARCHAR(100),
    remote_work     BOOLEAN DEFAULT FALSE,
    performance_score NUMERIC(3,1),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE hr.leave_requests (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    leave_type      VARCHAR(30) CHECK (leave_type IN ('annual','sick','maternity','paternity','unpaid','emergency')),
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    days_requested  INT,
    status          VARCHAR(20) CHECK (status IN ('pending','approved','rejected','cancelled')),
    approved_by     INT REFERENCES hr.employees(id),
    reason          TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE hr.performance_reviews (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    reviewer_id     INT REFERENCES hr.employees(id),
    review_period   VARCHAR(20),
    review_date     DATE,
    technical_score NUMERIC(3,1),
    communication_score NUMERIC(3,1),
    leadership_score NUMERIC(3,1),
    overall_score   NUMERIC(3,1),
    promotion_recommended BOOLEAN DEFAULT FALSE,
    salary_increase_pct NUMERIC(5,2),
    comments        TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE hr.salary_history (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    previous_salary NUMERIC(12,2),
    new_salary      NUMERIC(12,2),
    change_reason   VARCHAR(50),
    effective_date  DATE,
    approved_by     INT REFERENCES hr.employees(id),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- ECOMMERCE SCHEMA
-- ============================================================

CREATE TABLE ecommerce.categories (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    slug            VARCHAR(100) UNIQUE NOT NULL,
    parent_id       INT REFERENCES ecommerce.categories(id),
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    display_order   INT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.brands (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    slug            VARCHAR(100) UNIQUE NOT NULL,
    country_of_origin VARCHAR(50),
    website         VARCHAR(200),
    is_premium      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.products (
    id              SERIAL PRIMARY KEY,
    sku             VARCHAR(50) UNIQUE NOT NULL,
    name            VARCHAR(200) NOT NULL,
    slug            VARCHAR(200) UNIQUE NOT NULL,
    description     TEXT,
    category_id     INT REFERENCES ecommerce.categories(id),
    brand_id        INT REFERENCES ecommerce.brands(id),
    base_price      NUMERIC(12,2) NOT NULL,
    sale_price      NUMERIC(12,2),
    cost_price      NUMERIC(12,2),
    weight_kg       NUMERIC(8,3),
    is_active       BOOLEAN DEFAULT TRUE,
    is_featured     BOOLEAN DEFAULT FALSE,
    tags            TEXT[],
    rating_avg      NUMERIC(3,2) DEFAULT 0,
    rating_count    INT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.customers (
    id              SERIAL PRIMARY KEY,
    uuid            UUID DEFAULT uuid_generate_v4() UNIQUE,
    email           VARCHAR(200) UNIQUE NOT NULL,
    first_name      VARCHAR(50),
    last_name       VARCHAR(50),
    phone           VARCHAR(20),
    date_of_birth   DATE,
    gender          VARCHAR(10),
    country         VARCHAR(50),
    city            VARCHAR(100),
    postal_code     VARCHAR(20),
    is_verified     BOOLEAN DEFAULT FALSE,
    is_vip          BOOLEAN DEFAULT FALSE,
    total_orders    INT DEFAULT 0,
    total_spent     NUMERIC(15,2) DEFAULT 0,
    loyalty_points  INT DEFAULT 0,
    acquisition_channel VARCHAR(50),
    created_at      TIMESTAMP DEFAULT NOW(),
    last_login_at   TIMESTAMP
);

CREATE TABLE ecommerce.orders (
    id              SERIAL PRIMARY KEY,
    order_number    VARCHAR(30) UNIQUE NOT NULL,
    customer_id     INT REFERENCES ecommerce.customers(id),
    status          VARCHAR(30) CHECK (status IN ('pending','confirmed','processing','shipped','delivered','cancelled','refunded','returned')),
    payment_status  VARCHAR(20) CHECK (payment_status IN ('pending','paid','failed','refunded','partial_refund')),
    payment_method  VARCHAR(30),
    subtotal        NUMERIC(12,2),
    discount_amount NUMERIC(12,2) DEFAULT 0,
    shipping_amount NUMERIC(12,2) DEFAULT 0,
    tax_amount      NUMERIC(12,2) DEFAULT 0,
    total_amount    NUMERIC(12,2),
    currency        VARCHAR(3) DEFAULT 'EUR',
    shipping_country VARCHAR(50),
    shipping_city   VARCHAR(100),
    coupon_code     VARCHAR(30),
    notes           TEXT,
    ordered_at      TIMESTAMP DEFAULT NOW(),
    shipped_at      TIMESTAMP,
    delivered_at    TIMESTAMP,
    cancelled_at    TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.order_items (
    id              SERIAL PRIMARY KEY,
    order_id        INT REFERENCES ecommerce.orders(id),
    product_id      INT REFERENCES ecommerce.products(id),
    quantity        INT NOT NULL,
    unit_price      NUMERIC(12,2) NOT NULL,
    discount_pct    NUMERIC(5,2) DEFAULT 0,
    line_total      NUMERIC(12,2),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.reviews (
    id              SERIAL PRIMARY KEY,
    product_id      INT REFERENCES ecommerce.products(id),
    customer_id     INT REFERENCES ecommerce.customers(id),
    order_id        INT REFERENCES ecommerce.orders(id),
    rating          INT CHECK (rating BETWEEN 1 AND 5),
    title           VARCHAR(200),
    body            TEXT,
    is_verified_purchase BOOLEAN DEFAULT FALSE,
    helpful_votes   INT DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE ecommerce.coupons (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(30) UNIQUE NOT NULL,
    discount_type   VARCHAR(20) CHECK (discount_type IN ('percentage','fixed_amount','free_shipping')),
    discount_value  NUMERIC(10,2),
    min_order_amount NUMERIC(10,2),
    max_uses        INT,
    used_count      INT DEFAULT 0,
    valid_from      TIMESTAMP,
    valid_until     TIMESTAMP,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- INVENTORY SCHEMA
-- ============================================================

CREATE TABLE inventory.warehouses (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    code            VARCHAR(10) UNIQUE NOT NULL,
    country         VARCHAR(50),
    city            VARCHAR(100),
    address         TEXT,
    capacity_sqm    NUMERIC(10,2),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventory.stock (
    id              SERIAL PRIMARY KEY,
    product_id      INT REFERENCES ecommerce.products(id),
    warehouse_id    INT REFERENCES inventory.warehouses(id),
    quantity_on_hand INT DEFAULT 0,
    quantity_reserved INT DEFAULT 0,
    quantity_available INT GENERATED ALWAYS AS (quantity_on_hand - quantity_reserved) STORED,
    reorder_point   INT DEFAULT 10,
    reorder_quantity INT DEFAULT 50,
    last_restocked_at TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(product_id, warehouse_id)
);

CREATE TABLE inventory.stock_movements (
    id              SERIAL PRIMARY KEY,
    product_id      INT REFERENCES ecommerce.products(id),
    warehouse_id    INT REFERENCES inventory.warehouses(id),
    movement_type   VARCHAR(20) CHECK (movement_type IN ('purchase','sale','return','transfer','adjustment','damaged','expired')),
    quantity        INT NOT NULL,
    reference_id    INT,
    reference_type  VARCHAR(30),
    unit_cost       NUMERIC(12,2),
    notes           TEXT,
    created_by      INT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventory.suppliers (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    contact_name    VARCHAR(100),
    email           VARCHAR(200),
    phone           VARCHAR(30),
    country         VARCHAR(50),
    city            VARCHAR(100),
    payment_terms   INT DEFAULT 30,
    rating          NUMERIC(3,1),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventory.purchase_orders (
    id              SERIAL PRIMARY KEY,
    po_number       VARCHAR(30) UNIQUE NOT NULL,
    supplier_id     INT REFERENCES inventory.suppliers(id),
    warehouse_id    INT REFERENCES inventory.warehouses(id),
    status          VARCHAR(20) CHECK (status IN ('draft','sent','confirmed','partial','received','cancelled')),
    total_amount    NUMERIC(15,2),
    expected_date   DATE,
    received_date   DATE,
    created_by      INT,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- FINANCE SCHEMA
-- ============================================================

CREATE TABLE finance.accounts (
    id              SERIAL PRIMARY KEY,
    account_code    VARCHAR(20) UNIQUE NOT NULL,
    name            VARCHAR(100) NOT NULL,
    account_type    VARCHAR(30) CHECK (account_type IN ('asset','liability','equity','revenue','expense')),
    parent_id       INT REFERENCES finance.accounts(id),
    currency        VARCHAR(3) DEFAULT 'EUR',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE finance.transactions (
    id              SERIAL PRIMARY KEY,
    transaction_ref VARCHAR(50) UNIQUE NOT NULL,
    transaction_date DATE NOT NULL,
    description     TEXT,
    transaction_type VARCHAR(30) CHECK (transaction_type IN ('revenue','expense','transfer','refund','adjustment','payroll','tax')),
    amount          NUMERIC(15,2) NOT NULL,
    currency        VARCHAR(3) DEFAULT 'EUR',
    account_id      INT REFERENCES finance.accounts(id),
    reference_type  VARCHAR(30),
    reference_id    INT,
    department_id   INT REFERENCES hr.departments(id),
    created_by      INT REFERENCES hr.employees(id),
    approved_by     INT REFERENCES hr.employees(id),
    status          VARCHAR(20) CHECK (status IN ('pending','posted','reversed','cancelled')),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE finance.invoices (
    id              SERIAL PRIMARY KEY,
    invoice_number  VARCHAR(30) UNIQUE NOT NULL,
    invoice_type    VARCHAR(20) CHECK (invoice_type IN ('customer','supplier','internal')),
    customer_id     INT REFERENCES ecommerce.customers(id),
    supplier_id     INT REFERENCES inventory.suppliers(id),
    issue_date      DATE NOT NULL,
    due_date        DATE,
    paid_date       DATE,
    subtotal        NUMERIC(15,2),
    tax_amount      NUMERIC(15,2),
    total_amount    NUMERIC(15,2),
    paid_amount     NUMERIC(15,2) DEFAULT 0,
    currency        VARCHAR(3) DEFAULT 'EUR',
    status          VARCHAR(20) CHECK (status IN ('draft','sent','partial','paid','overdue','cancelled','disputed')),
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE finance.budgets (
    id              SERIAL PRIMARY KEY,
    department_id   INT REFERENCES hr.departments(id),
    account_id      INT REFERENCES finance.accounts(id),
    fiscal_year     INT NOT NULL,
    fiscal_quarter  INT CHECK (fiscal_quarter BETWEEN 1 AND 4),
    budgeted_amount NUMERIC(15,2),
    actual_amount   NUMERIC(15,2) DEFAULT 0,
    variance        NUMERIC(15,2) GENERATED ALWAYS AS (actual_amount - budgeted_amount) STORED,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- SUPPORT SCHEMA
-- ============================================================

CREATE TABLE support.agents (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    skills          TEXT[],
    max_tickets     INT DEFAULT 20,
    is_available    BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE support.tickets (
    id              SERIAL PRIMARY KEY,
    ticket_number   VARCHAR(20) UNIQUE NOT NULL,
    customer_id     INT REFERENCES ecommerce.customers(id),
    order_id        INT REFERENCES ecommerce.orders(id),
    assigned_agent_id INT REFERENCES support.agents(id),
    category        VARCHAR(50) CHECK (category IN ('billing','shipping','product','technical','return','complaint','general','fraud')),
    priority        VARCHAR(10) CHECK (priority IN ('low','medium','high','urgent','critical')),
    status          VARCHAR(20) CHECK (status IN ('new','open','pending_customer','pending_internal','resolved','closed','escalated')),
    subject         VARCHAR(200),
    description     TEXT,
    channel         VARCHAR(20) CHECK (channel IN ('email','chat','phone','social','web_form')),
    satisfaction_score INT CHECK (satisfaction_score BETWEEN 1 AND 5),
    first_response_at TIMESTAMP,
    resolved_at     TIMESTAMP,
    closed_at       TIMESTAMP,
    sla_breached    BOOLEAN DEFAULT FALSE,
    tags            TEXT[],
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE support.ticket_messages (
    id              SERIAL PRIMARY KEY,
    ticket_id       INT REFERENCES support.tickets(id),
    sender_type     VARCHAR(10) CHECK (sender_type IN ('customer','agent','system')),
    sender_id       INT,
    message         TEXT NOT NULL,
    is_internal     BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE support.sla_policies (
    id              SERIAL PRIMARY KEY,
    priority        VARCHAR(10) UNIQUE,
    first_response_hours INT,
    resolution_hours INT,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- ANALYTICS SCHEMA
-- ============================================================

CREATE TABLE analytics.page_views (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(50),
    customer_id     INT REFERENCES ecommerce.customers(id),
    page_url        VARCHAR(500),
    page_type       VARCHAR(30) CHECK (page_type IN ('home','category','product','cart','checkout','confirmation','account','search','blog')),
    product_id      INT REFERENCES ecommerce.products(id),
    referrer        VARCHAR(500),
    device_type     VARCHAR(20) CHECK (device_type IN ('desktop','mobile','tablet')),
    browser         VARCHAR(50),
    country         VARCHAR(50),
    duration_seconds INT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE analytics.marketing_campaigns (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    channel         VARCHAR(30) CHECK (channel IN ('email','paid_search','social_media','display','affiliate','influencer','seo','direct')),
    start_date      DATE,
    end_date        DATE,
    budget          NUMERIC(12,2),
    spent           NUMERIC(12,2) DEFAULT 0,
    impressions     INT DEFAULT 0,
    clicks          INT DEFAULT 0,
    conversions     INT DEFAULT 0,
    revenue_generated NUMERIC(15,2) DEFAULT 0,
    status          VARCHAR(20) CHECK (status IN ('draft','active','paused','completed','cancelled')),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE analytics.ab_tests (
    id              SERIAL PRIMARY KEY,
    test_name       VARCHAR(100),
    hypothesis      TEXT,
    variant_a_name  VARCHAR(50),
    variant_b_name  VARCHAR(50),
    metric          VARCHAR(50),
    variant_a_value NUMERIC(10,4),
    variant_b_value NUMERIC(10,4),
    sample_size_a   INT,
    sample_size_b   INT,
    p_value         NUMERIC(8,6),
    winner          VARCHAR(10),
    start_date      DATE,
    end_date        DATE,
    status          VARCHAR(20) CHECK (status IN ('running','completed','stopped')),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- INDEXES
-- ============================================================
CREATE INDEX idx_employees_dept ON hr.employees(department_id);
CREATE INDEX idx_employees_manager ON hr.employees(manager_id);
CREATE INDEX idx_orders_customer ON ecommerce.orders(customer_id);
CREATE INDEX idx_orders_status ON ecommerce.orders(status);
CREATE INDEX idx_orders_ordered_at ON ecommerce.orders(ordered_at);
CREATE INDEX idx_order_items_order ON ecommerce.order_items(order_id);
CREATE INDEX idx_order_items_product ON ecommerce.order_items(product_id);
CREATE INDEX idx_products_category ON ecommerce.products(category_id);
CREATE INDEX idx_stock_product ON inventory.stock(product_id);
CREATE INDEX idx_tickets_customer ON support.tickets(customer_id);
CREATE INDEX idx_tickets_status ON support.tickets(status);
CREATE INDEX idx_transactions_date ON finance.transactions(transaction_date);
CREATE INDEX idx_page_views_customer ON analytics.page_views(customer_id);
CREATE INDEX idx_page_views_created ON analytics.page_views(created_at);

-- ============================================================
-- DATA POPULATION
-- ============================================================

-- HR: Departments
INSERT INTO hr.departments (name, code, budget, head_count_max, location) VALUES
('Executive',         'EXEC',  2500000, 10,  'Athens HQ'),
('Engineering',       'ENG',   4500000, 80,  'Athens HQ'),
('Product',           'PROD',  1800000, 25,  'Athens HQ'),
('Sales',             'SALES', 2200000, 45,  'Athens HQ'),
('Marketing',         'MKT',   1500000, 30,  'Athens HQ'),
('Human Resources',   'HR',    800000,  15,  'Athens HQ'),
('Finance',           'FIN',   900000,  20,  'Athens HQ'),
('Customer Support',  'CS',    1200000, 40,  'Thessaloniki'),
('Logistics',         'LOG',   1100000, 35,  'Piraeus'),
('Data & Analytics',  'DATA',  1600000, 22,  'Athens HQ');

-- HR: Employees (50 employees across departments)
INSERT INTO hr.employees (employee_code, first_name, last_name, email, department_id, manager_id, job_title, employment_type, salary, hire_date, is_active, date_of_birth, gender, nationality, office_location, remote_work, performance_score) VALUES
('EMP001','Alexandros','Papadopoulos','a.papadopoulos@savvina.com',1,NULL,'CEO','full_time',180000,'2019-01-15',TRUE,'1975-04-12','male','Greek','Athens HQ',FALSE,4.8),
('EMP002','Maria','Konstantinou','m.konstantinou@savvina.com',1,1,'CFO','full_time',150000,'2019-03-01',TRUE,'1978-07-22','female','Greek','Athens HQ',FALSE,4.7),
('EMP003','Nikos','Georgiou','n.georgiou@savvina.com',2,1,'CTO','full_time',160000,'2019-02-01',TRUE,'1980-11-05','male','Greek','Athens HQ',FALSE,4.9),
('EMP004','Elena','Dimitriou','e.dimitriou@savvina.com',4,1,'VP Sales','full_time',130000,'2019-06-01',TRUE,'1982-03-18','female','Greek','Athens HQ',FALSE,4.6),
('EMP005','Kostas','Nikolaou','k.nikolaou@savvina.com',3,1,'VP Product','full_time',140000,'2020-01-10',TRUE,'1983-09-28','male','Greek','Athens HQ',FALSE,4.5),
('EMP006','Sophia','Alexiou','s.alexiou@savvina.com',2,3,'Lead Engineer','full_time',95000,'2020-03-15',TRUE,'1988-02-14','female','Greek','Athens HQ',TRUE,4.7),
('EMP007','Dimitris','Stavros','d.stavros@savvina.com',2,3,'Senior Engineer','full_time',85000,'2020-06-01',TRUE,'1990-05-20','male','Greek','Athens HQ',TRUE,4.4),
('EMP008','Anna','Petrou','a.petrou@savvina.com',2,3,'Senior Engineer','full_time',82000,'2021-01-15',TRUE,'1991-08-10','female','Cypriot','Athens HQ',TRUE,4.3),
('EMP009','Giorgos','Makris','g.makris@savvina.com',2,6,'Engineer','full_time',72000,'2021-04-01',TRUE,'1993-12-03','male','Greek','Athens HQ',TRUE,4.1),
('EMP010','Ioanna','Vasiliou','i.vasiliou@savvina.com',2,6,'Engineer','full_time',70000,'2021-07-01',TRUE,'1994-06-15','female','Greek','Athens HQ',FALSE,4.2),
('EMP011','Petros','Andreou','p.andreou@savvina.com',2,7,'Junior Engineer','full_time',58000,'2022-03-01',TRUE,'1996-01-25','male','Greek','Athens HQ',FALSE,3.9),
('EMP012','Christina','Manolis','c.manolis@savvina.com',2,7,'Junior Engineer','full_time',56000,'2022-06-15',TRUE,'1997-09-08','female','Greek','Athens HQ',TRUE,3.8),
('EMP013','Thanasis','Oikonomou','t.oikonomou@savvina.com',3,5,'Product Manager','full_time',88000,'2020-09-01',TRUE,'1987-04-30','male','Greek','Athens HQ',FALSE,4.4),
('EMP014','Despina','Karagianni','d.karagianni@savvina.com',3,5,'Product Designer','full_time',75000,'2021-02-01',TRUE,'1992-11-17','female','Greek','Athens HQ',TRUE,4.5),
('EMP015','Michalis','Papadakis','m.papadakis@savvina.com',4,4,'Senior Sales Manager','full_time',90000,'2020-05-01',TRUE,'1985-07-22','male','Greek','Athens HQ',FALSE,4.6),
('EMP016','Katerina','Zervou','k.zervou@savvina.com',4,4,'Sales Manager','full_time',78000,'2021-01-10',TRUE,'1989-03-14','female','Greek','Thessaloniki',FALSE,4.3),
('EMP017','Stavros','Logothetis','s.logothetis@savvina.com',4,15,'Sales Executive','full_time',62000,'2021-08-01',TRUE,'1993-10-20','male','Greek','Athens HQ',FALSE,4.0),
('EMP018','Irene','Spanou','i.spanou@savvina.com',4,15,'Sales Executive','full_time',60000,'2022-01-15',TRUE,'1994-05-11','female','Greek','Athens HQ',FALSE,4.1),
('EMP019','Lefteris','Anagnostou','l.anagnostou@savvina.com',4,16,'Sales Executive','full_time',58000,'2022-04-01',TRUE,'1995-08-28','male','Greek','Thessaloniki',FALSE,3.9),
('EMP020','Vicky','Tsiros','v.tsiros@savvina.com',5,1,'CMO','full_time',120000,'2020-02-01',TRUE,'1984-12-05','female','Greek','Athens HQ',FALSE,4.4),
('EMP021','Panos','Efthymiou','p.efthymiou@savvina.com',5,20,'Marketing Manager','full_time',82000,'2020-07-01',TRUE,'1988-06-15','male','Greek','Athens HQ',FALSE,4.2),
('EMP022','Lena','Stamatiou','l.stamatiou@savvina.com',5,20,'Content Manager','full_time',68000,'2021-03-01',TRUE,'1991-02-28','female','Greek','Athens HQ',TRUE,4.3),
('EMP023','Aggelos','Katsaros','a.katsaros@savvina.com',5,21,'SEO Specialist','full_time',62000,'2021-09-01',TRUE,'1993-11-12','male','Greek','Athens HQ',TRUE,4.0),
('EMP024','Marianna','Deligianni','m.deligianni@savvina.com',6,1,'HR Director','full_time',100000,'2019-09-01',TRUE,'1981-08-19','female','Greek','Athens HQ',FALSE,4.5),
('EMP025','Takis','Economou','t.economou@savvina.com',6,24,'HR Manager','full_time',72000,'2020-10-01',TRUE,'1987-04-05','male','Greek','Athens HQ',FALSE,4.2),
('EMP026','Natalia','Hadjidimitriou','n.hadjidimitriou@savvina.com',7,2,'Finance Manager','full_time',88000,'2020-04-01',TRUE,'1986-01-30','female','Cypriot','Athens HQ',FALSE,4.4),
('EMP027','Vasilis','Samaras','v.samaras@savvina.com',7,26,'Senior Accountant','full_time',72000,'2020-11-01',TRUE,'1990-07-14','male','Greek','Athens HQ',FALSE,4.1),
('EMP028','Aggeliki','Mitsou','a.mitsou@savvina.com',8,1,'Support Director','full_time',95000,'2019-11-01',TRUE,'1983-03-22','female','Greek','Thessaloniki',FALSE,4.3),
('EMP029','Babis','Papagiannakis','b.papagiannakis@savvina.com',8,28,'Support Team Lead','full_time',68000,'2020-08-01',TRUE,'1989-10-18','male','Greek','Thessaloniki',FALSE,4.2),
('EMP030','Zoe','Christodoulou','z.christodoulou@savvina.com',8,29,'Support Agent','full_time',48000,'2021-05-01',TRUE,'1995-06-25','female','Greek','Thessaloniki',FALSE,4.0),
('EMP031','Nektarios','Fountoulakis','n.fountoulakis@savvina.com',8,29,'Support Agent','full_time',46000,'2021-10-01',TRUE,'1996-12-08','male','Greek','Thessaloniki',FALSE,3.8),
('EMP032','Fofi','Tsatsaroni','f.tsatsaroni@savvina.com',8,29,'Support Agent','full_time',46000,'2022-02-01',TRUE,'1997-04-15','female','Greek','Thessaloniki',FALSE,3.9),
('EMP033','Manolis','Kladakis','m.kladakis@savvina.com',9,1,'Logistics Manager','full_time',85000,'2020-03-01',TRUE,'1985-11-02','male','Greek','Piraeus',FALSE,4.3),
('EMP034','Rania','Alexandrou','r.alexandrou@savvina.com',9,33,'Warehouse Supervisor','full_time',62000,'2020-09-01',TRUE,'1990-08-30','female','Greek','Piraeus',FALSE,4.1),
('EMP035','Makis','Stavrakakis','m.stavrakakis@savvina.com',9,33,'Logistics Coordinator','full_time',52000,'2021-06-01',TRUE,'1993-02-17','male','Greek','Piraeus',FALSE,3.9),
('EMP036','Xenia','Boutari','x.boutari@savvina.com',10,1,'Head of Data','full_time',115000,'2020-01-20',TRUE,'1984-09-14','female','Greek','Athens HQ',FALSE,4.7),
('EMP037','Spyros','Diamantis','s.diamantis@savvina.com',10,36,'Data Engineer','full_time',85000,'2020-08-15',TRUE,'1989-05-28','male','Greek','Athens HQ',TRUE,4.4),
('EMP038','Tatiana','Beka','t.beka@savvina.com',10,36,'Data Analyst','full_time',72000,'2021-03-15',TRUE,'1993-12-20','female','Greek','Athens HQ',TRUE,4.3),
('EMP039','Christos','Kalliris','c.kalliris@savvina.com',10,36,'Data Scientist','full_time',90000,'2021-07-01',TRUE,'1991-07-07','male','Greek','Athens HQ',TRUE,4.5),
('EMP040','Evangelia','Mitrou','e.mitrou@savvina.com',2,3,'DevOps Engineer','full_time',88000,'2021-09-01',TRUE,'1990-03-25','female','Greek','Athens HQ',TRUE,4.3),
('EMP041','Thodoris','Panagiotidis','t.panagiotidis@savvina.com',2,6,'Backend Engineer','full_time',76000,'2022-01-10',TRUE,'1994-11-11','male','Greek','Athens HQ',TRUE,4.0),
('EMP042','Marilena','Papadimitriou','m.papadimitriou@savvina.com',3,13,'UX Researcher','full_time',70000,'2022-04-01',TRUE,'1993-07-19','female','Greek','Athens HQ',TRUE,4.1),
('EMP043','Aris','Karatzas','a.karatzas@savvina.com',4,16,'Sales Executive','full_time',61000,'2022-07-01',TRUE,'1995-01-08','male','Greek','Thessaloniki',FALSE,4.0),
('EMP044','Penelope','Alexaki','p.alexaki@savvina.com',5,21,'Paid Media Specialist','full_time',65000,'2022-02-15',TRUE,'1994-04-22','female','Greek','Athens HQ',FALSE,4.1),
('EMP045','Kosmas','Bouzikos','k.bouzikos@savvina.com',8,29,'Support Agent','full_time',44000,'2022-08-01',TRUE,'1998-09-03','male','Greek','Thessaloniki',FALSE,3.7),
('EMP046','Smaragda','Triantafyllou','s.triantafyllou@savvina.com',2,7,'QA Engineer','full_time',68000,'2022-03-01',TRUE,'1993-06-14','female','Greek','Athens HQ',FALSE,4.0),
('EMP047','Ilias','Mourtzis','i.mourtzis@savvina.com',7,26,'Junior Accountant','full_time',48000,'2022-09-01',TRUE,'1997-02-10','male','Greek','Athens HQ',FALSE,3.8),
('EMP048','Konstantina','Tsakiri','k.tsakiri@savvina.com',6,25,'HR Specialist','full_time',55000,'2022-05-01',TRUE,'1996-08-27','female','Greek','Athens HQ',FALSE,4.0),
('EMP049','Fotis','Lamprou','f.lamprou@savvina.com',9,34,'Warehouse Operator','full_time',38000,'2022-11-01',TRUE,'1999-03-15','male','Greek','Piraeus',FALSE,3.8),
('EMP050','Chrysa','Pantazis','c.pantazis@savvina.com',4,15,'Sales Executive','full_time',60000,'2023-01-15',TRUE,'1996-10-30','female','Greek','Athens HQ',FALSE,3.9);

-- LEAVE REQUESTS
INSERT INTO hr.leave_requests (employee_id, leave_type, start_date, end_date, days_requested, status, approved_by, reason) VALUES
(9,'annual','2024-07-15','2024-07-26',10,'approved',6,'Summer vacation'),
(10,'annual','2024-08-05','2024-08-16',10,'approved',6,'Summer vacation'),
(11,'sick','2024-03-10','2024-03-12',3,'approved',7,'Flu'),
(15,'annual','2024-06-17','2024-06-28',10,'approved',4,'Family trip'),
(22,'maternity','2024-04-01','2024-10-01',130,'approved',24,'Maternity leave'),
(30,'sick','2024-09-05','2024-09-06',2,'approved',29,'Medical appointment'),
(38,'annual','2024-08-12','2024-08-23',10,'approved',36,'Vacation'),
(7,'annual','2025-01-13','2025-01-24',10,'approved',3,'Winter break'),
(14,'annual','2025-02-03','2025-02-07',5,'approved',5,'Short break'),
(41,'sick','2025-01-20','2025-01-21',2,'approved',6,'Cold');

-- PERFORMANCE REVIEWS
INSERT INTO hr.performance_reviews (employee_id, reviewer_id, review_period, review_date, technical_score, communication_score, leadership_score, overall_score, promotion_recommended, salary_increase_pct, comments) VALUES
(6,3,'2023-H2','2024-01-15',4.8,4.6,4.7,4.7,TRUE,8.0,'Excellent technical leadership. Ready for senior role.'),
(7,3,'2023-H2','2024-01-15',4.5,4.3,4.4,4.4,FALSE,5.0,'Strong contributor, consistent delivery.'),
(9,6,'2023-H2','2024-01-20',4.2,4.0,3.8,4.1,FALSE,4.0,'Good progress, needs to improve system design skills.'),
(13,5,'2023-H2','2024-01-18',4.5,4.4,4.3,4.4,TRUE,7.0,'Outstanding product thinking. Recommend for Senior PM.'),
(15,4,'2023-H2','2024-01-22',4.7,4.6,4.5,4.6,TRUE,9.0,'Top performer in sales, exceeded targets by 30%.'),
(38,36,'2023-H2','2024-01-25',4.4,4.2,4.1,4.3,FALSE,5.5,'Great analytical work, improving communication.'),
(39,36,'2023-H2','2024-01-25',4.6,4.4,4.3,4.5,FALSE,7.0,'Excellent ML work, strong technical skills.');

-- SALARY HISTORY
INSERT INTO hr.salary_history (employee_id, previous_salary, new_salary, change_reason, effective_date, approved_by) VALUES
(6,87000,95000,'promotion','2024-02-01',3),
(7,80000,85000,'annual_review','2024-02-01',3),
(13,80000,88000,'promotion','2024-02-01',5),
(15,82000,90000,'performance','2024-02-01',4),
(38,68000,72000,'annual_review','2024-02-01',36),
(9,68000,72000,'annual_review','2024-02-01',6);

-- ECOMMERCE: Categories
INSERT INTO ecommerce.categories (name, slug, parent_id, description, is_active, display_order) VALUES
('Electronics',         'electronics',          NULL, 'All electronic devices',                  TRUE, 1),
('Computers',           'computers',            1,    'Laptops, desktops, and accessories',       TRUE, 1),
('Smartphones',         'smartphones',          1,    'Mobile phones and accessories',            TRUE, 2),
('Audio',               'audio',                1,    'Headphones, speakers, audio equipment',    TRUE, 3),
('Gaming',              'gaming',               1,    'Gaming consoles, games, accessories',      TRUE, 4),
('Home & Garden',       'home-garden',          NULL, 'Everything for your home',                 TRUE, 2),
('Furniture',           'furniture',            6,    'Indoor and outdoor furniture',             TRUE, 1),
('Kitchen',             'kitchen',              6,    'Kitchen appliances and tools',             TRUE, 2),
('Sports & Outdoors',   'sports-outdoors',      NULL, 'Sports equipment and outdoor gear',        TRUE, 3),
('Clothing',            'clothing',             NULL, 'Apparel and accessories',                  TRUE, 4),
('Men''s Clothing',     'mens-clothing',        10,   'Men''s fashion and apparel',               TRUE, 1),
('Women''s Clothing',   'womens-clothing',      10,   'Women''s fashion and apparel',             TRUE, 2),
('Books',               'books',                NULL, 'Physical and digital books',               TRUE, 5),
('Health & Beauty',     'health-beauty',        NULL, 'Personal care and health products',        TRUE, 6),
('Toys & Kids',         'toys-kids',            NULL, 'Toys and products for children',           TRUE, 7);

-- ECOMMERCE: Brands
INSERT INTO ecommerce.brands (name, slug, country_of_origin, website, is_premium) VALUES
('TechPro',       'techpro',      'Taiwan',       'https://techpro.example.com',   FALSE),
('LuxeAudio',     'luxeaudio',    'Germany',      'https://luxeaudio.example.com', TRUE),
('SwiftMobile',   'swiftmobile',  'South Korea',  'https://swiftmobile.example.com', FALSE),
('AlphaGaming',   'alphagaming',  'USA',          'https://alphagaming.example.com', FALSE),
('NordHome',      'nordhome',     'Sweden',       'https://nordhome.example.com',  TRUE),
('ActiveGear',    'activegear',   'Netherlands',  'https://activegear.example.com', FALSE),
('UrbanStyle',    'urbanstyle',   'Italy',        'https://urbanstyle.example.com', TRUE),
('ReadMore',      'readmore',     'UK',           'https://readmore.example.com',  FALSE),
('VitaHealth',    'vitahealth',   'Switzerland',  'https://vitahealth.example.com', TRUE),
('SmartKids',     'smartkids',    'Denmark',      'https://smartkids.example.com', FALSE);

-- ECOMMERCE: Products (30 products)
INSERT INTO ecommerce.products (sku, name, slug, description, category_id, brand_id, base_price, sale_price, cost_price, weight_kg, is_active, is_featured, tags, rating_avg, rating_count) VALUES
('SKU-001','TechPro Laptop Pro 15','techpro-laptop-pro-15','High-performance laptop with 16GB RAM',2,1,1299.00,1099.00,650.00,1.8,TRUE,TRUE,ARRAY['laptop','work','premium'],4.5,234),
('SKU-002','TechPro Laptop Air 13','techpro-laptop-air-13','Ultralight laptop for everyday use',2,1,899.00,NULL,450.00,1.2,TRUE,FALSE,ARRAY['laptop','ultralight'],4.2,187),
('SKU-003','SwiftMobile X20','swiftmobile-x20','Flagship smartphone 128GB',3,3,799.00,699.00,320.00,0.19,TRUE,TRUE,ARRAY['smartphone','5g','flagship'],4.6,521),
('SKU-004','SwiftMobile Lite 12','swiftmobile-lite-12','Budget smartphone 64GB',3,3,299.00,NULL,120.00,0.17,TRUE,FALSE,ARRAY['smartphone','budget'],4.0,342),
('SKU-005','LuxeAudio WH-500','luxeaudio-wh-500','Premium wireless noise-cancelling headphones',4,2,349.00,299.00,120.00,0.28,TRUE,TRUE,ARRAY['headphones','wireless','premium'],4.8,678),
('SKU-006','LuxeAudio BT-200','luxeaudio-bt-200','Wireless earbuds with charging case',4,2,149.00,119.00,52.00,0.06,TRUE,FALSE,ARRAY['earbuds','wireless'],4.4,423),
('SKU-007','AlphaGaming Console X','alphagaming-console-x','Next-gen gaming console 1TB',5,4,499.00,NULL,220.00,3.2,TRUE,TRUE,ARRAY['console','gaming','4k'],4.7,891),
('SKU-008','AlphaGaming Controller Pro','alphagaming-controller-pro','Wireless gaming controller',5,4,79.00,59.00,28.00,0.24,TRUE,FALSE,ARRAY['controller','gaming'],4.3,567),
('SKU-009','NordHome Sofa 3-Seater','nordhome-sofa-3-seater','Scandinavian design sofa in grey',7,5,1499.00,1299.00,600.00,45.0,TRUE,TRUE,ARRAY['sofa','living-room','premium'],4.6,89),
('SKU-010','NordHome Coffee Table Oak','nordhome-coffee-table-oak','Solid oak coffee table',7,5,399.00,NULL,160.00,18.0,TRUE,FALSE,ARRAY['table','living-room','oak'],4.5,112),
('SKU-011','KitchenPro Blender 1000W','kitchenpro-blender-1000w','Professional blender for smoothies',8,5,129.00,99.00,45.00,2.1,TRUE,FALSE,ARRAY['blender','kitchen','appliance'],4.2,334),
('SKU-012','KitchenPro Coffee Maker','kitchenpro-coffee-maker','Programmable coffee maker 12-cup',8,5,89.00,NULL,32.00,1.8,TRUE,FALSE,ARRAY['coffee','kitchen'],4.4,445),
('SKU-013','ActiveGear Running Shoes M','activegear-running-shoes-m','Men''s professional running shoes',9,6,119.00,89.00,42.00,0.35,TRUE,TRUE,ARRAY['shoes','running','sport'],4.5,678),
('SKU-014','ActiveGear Yoga Mat Pro','activegear-yoga-mat-pro','Non-slip professional yoga mat 6mm',9,6,59.00,49.00,18.00,1.2,TRUE,FALSE,ARRAY['yoga','mat','fitness'],4.6,289),
('SKU-015','UrbanStyle Men Suit Navy','urbanstyle-men-suit-navy','Premium wool blend men''s suit',11,7,599.00,499.00,180.00,1.5,TRUE,TRUE,ARRAY['suit','formal','premium'],4.7,145),
('SKU-016','UrbanStyle Women Dress Evening','urbanstyle-women-dress-evening','Elegant evening dress',12,7,299.00,249.00,90.00,0.5,TRUE,FALSE,ARRAY['dress','evening','premium'],4.5,234),
('SKU-017','ReadMore E-Reader Pro','readmore-e-reader-pro','E-ink reader 32GB waterproof',13,8,189.00,159.00,68.00,0.18,TRUE,FALSE,ARRAY['ereader','books','waterproof'],4.6,456),
('SKU-018','VitaHealth Vitamin C 1000mg','vitahealth-vitamin-c','Vitamin C supplement 90 tablets',14,9,24.99,NULL,8.00,0.15,TRUE,FALSE,ARRAY['vitamin','supplement','health'],4.4,892),
('SKU-019','VitaHealth Protein Powder','vitahealth-protein-powder','Whey protein powder 2kg vanilla',14,9,49.99,42.99,18.00,2.1,TRUE,TRUE,ARRAY['protein','fitness','supplement'],4.5,634),
('SKU-020','SmartKids Educational Tablet','smartkids-edu-tablet','Kids learning tablet 7 inch',15,10,149.00,119.00,52.00,0.42,TRUE,FALSE,ARRAY['kids','tablet','education'],4.3,321),
('SKU-021','TechPro Mechanical Keyboard','techpro-mech-keyboard','RGB mechanical keyboard USB-C',2,1,149.00,129.00,55.00,1.1,TRUE,FALSE,ARRAY['keyboard','mechanical','gaming'],4.4,287),
('SKU-022','TechPro Wireless Mouse','techpro-wireless-mouse','Ergonomic wireless mouse',2,1,49.00,39.00,16.00,0.12,TRUE,FALSE,ARRAY['mouse','wireless','ergonomic'],4.3,412),
('SKU-023','LuxeAudio Soundbar 2.1','luxeaudio-soundbar-21','Premium soundbar with subwoofer',4,2,499.00,399.00,175.00,5.5,TRUE,TRUE,ARRAY['soundbar','audio','home-theater'],4.7,189),
('SKU-024','SwiftMobile Smart Watch','swiftmobile-smart-watch','Fitness smartwatch GPS',3,3,249.00,199.00,88.00,0.05,TRUE,FALSE,ARRAY['smartwatch','fitness','gps'],4.4,523),
('SKU-025','ActiveGear Cycling Helmet','activegear-cycling-helmet','Aerodynamic road cycling helmet',9,6,89.00,NULL,32.00,0.28,TRUE,FALSE,ARRAY['cycling','helmet','safety'],4.6,178),
('SKU-026','NordHome Desk Lamp LED','nordhome-desk-lamp','Adjustable LED desk lamp USB charging',6,5,59.00,49.00,20.00,0.8,TRUE,FALSE,ARRAY['lamp','led','home-office'],4.4,345),
('SKU-027','AlphaGaming Headset 7.1','alphagaming-headset','Surround sound gaming headset',5,4,99.00,79.00,35.00,0.35,TRUE,FALSE,ARRAY['headset','gaming','7.1'],4.3,456),
('SKU-028','UrbanStyle Sneakers White','urbanstyle-sneakers-white','Premium leather sneakers',10,7,179.00,149.00,62.00,0.4,TRUE,FALSE,ARRAY['sneakers','shoes','fashion'],4.5,312),
('SKU-029','VitaHealth Omega-3','vitahealth-omega3','Omega-3 fish oil 120 capsules',14,9,29.99,NULL,10.00,0.18,TRUE,FALSE,ARRAY['omega3','supplement','health'],4.5,567),
('SKU-030','SmartKids LEGO Set 500pcs','smartkids-lego-500','Creative building blocks 500 pieces',15,10,49.99,39.99,15.00,0.9,TRUE,FALSE,ARRAY['lego','kids','creative'],4.7,789);

-- ECOMMERCE: Customers (generate 60 realistic customers)
INSERT INTO ecommerce.customers (email, first_name, last_name, phone, date_of_birth, gender, country, city, postal_code, is_verified, is_vip, total_orders, total_spent, loyalty_points, acquisition_channel, created_at, last_login_at) VALUES
('george.papadopoulos@gmail.com','George','Papadopoulos','+30-694-1234567','1985-03-15','male','Greece','Athens','10431',TRUE,TRUE,24,4521.50,4521,'organic','2022-01-15 10:30:00','2025-01-28 14:22:00'),
('maria.stavridou@yahoo.gr','Maria','Stavridou','+30-693-2345678','1990-07-22','female','Greece','Thessaloniki','54626',TRUE,FALSE,8,892.30,892,'paid_search','2022-05-20 09:15:00','2025-01-25 11:45:00'),
('nikos.alexiou@hotmail.com','Nikos','Alexiou','+30-697-3456789','1978-11-08','male','Greece','Patras','26222',TRUE,FALSE,3,234.80,234,'social_media','2023-02-10 16:20:00','2025-01-20 08:30:00'),
('elena.petridou@gmail.com','Elena','Petridou','+30-698-4567890','1995-04-30','female','Greece','Heraklion','71201',TRUE,TRUE,15,2876.40,2876,'email','2022-03-01 12:00:00','2025-01-29 16:10:00'),
('kostas.nikolaidis@gmail.com','Kostas','Nikolaidis','+30-694-5678901','1982-09-12','male','Greece','Athens','11521',TRUE,FALSE,6,567.20,567,'organic','2023-06-15 14:30:00','2025-01-22 09:15:00'),
('sofia.dimitriou@gmail.com','Sofia','Dimitriou','+30-695-6789012','1988-02-25','female','Greece','Athens','15341',TRUE,TRUE,31,7234.60,7234,'referral','2021-11-20 10:00:00','2025-01-30 17:45:00'),
('dimitris.georgiou@outlook.com','Dimitris','Georgiou','+30-697-7890123','1975-06-18','male','Greece','Larissa','41221',TRUE,FALSE,4,345.90,345,'paid_search','2023-04-08 11:20:00','2025-01-18 13:20:00'),
('anna.konstantinou@gmail.com','Anna','Konstantinou','+30-693-8901234','1992-12-03','female','Greece','Athens','12132',TRUE,FALSE,11,1456.70,1456,'organic','2022-09-14 08:45:00','2025-01-27 10:30:00'),
('giorgos.papadakis@gmail.com','Giorgos','Papadakis','+30-694-9012345','1987-05-20','male','Greece','Rhodes','85100',TRUE,FALSE,2,189.99,189,'social_media','2024-01-10 15:30:00','2025-01-15 12:00:00'),
('ioanna.vasiliou@yahoo.com','Ioanna','Vasiliou','+30-698-0123456','1993-08-14','female','Greece','Thessaloniki','54640',TRUE,TRUE,19,3654.20,3654,'email','2022-02-28 09:30:00','2025-01-29 14:20:00'),
('petros.andreou@gmail.com','Petros','Andreou','+30-695-1234567','1980-01-25','male','Greece','Athens','10432',TRUE,FALSE,7,678.50,678,'organic','2023-01-20 14:00:00','2025-01-26 11:15:00'),
('christina.manoli@gmail.com','Christina','Manoli','+30-697-2345678','1998-09-08','female','Greece','Athens','17561',TRUE,FALSE,5,456.80,456,'social_media','2023-07-05 10:30:00','2025-01-21 16:45:00'),
('thanasis.oikonomou@hotmail.gr','Thanasis','Oikonomou','+30-693-3456789','1972-04-30','male','Greece','Volos','38221',TRUE,FALSE,9,987.60,987,'paid_search','2022-11-12 13:20:00','2025-01-23 09:00:00'),
('despina.karagianni@gmail.com','Despina','Karagianni','+30-694-4567890','1991-11-17','female','Greece','Thessaloniki','54628',TRUE,TRUE,22,5123.40,5123,'referral','2022-04-03 11:45:00','2025-01-30 15:30:00'),
('michalis.papadimitriou@gmail.com','Michalis','Papadimitriou','+30-698-5678901','1986-07-22','male','Greece','Athens','16341',TRUE,FALSE,4,398.70,398,'organic','2023-08-20 09:15:00','2025-01-19 12:30:00'),
('katerina.zervou@yahoo.gr','Katerina','Zervou','+30-695-6789012','1989-03-14','female','Greece','Athens','10433',TRUE,FALSE,6,523.10,523,'email','2023-03-15 14:30:00','2025-01-24 10:00:00'),
('stavros.logothetis@gmail.com','Stavros','Logothetis','+30-697-7890123','1994-10-20','male','Greece','Chalkida','34100',TRUE,FALSE,3,278.40,278,'social_media','2024-02-01 10:00:00','2025-01-20 14:45:00'),
('irene.spanou@gmail.com','Irene','Spanou','+30-693-8901234','1985-05-11','female','Greece','Athens','11143',TRUE,TRUE,28,6789.30,6789,'organic','2021-08-10 08:00:00','2025-01-30 09:15:00'),
('lefteris.anagnostou@outlook.com','Lefteris','Anagnostou','+30-694-9012345','1976-08-28','male','Greece','Kavala','65302',TRUE,FALSE,2,145.60,145,'paid_search','2024-03-10 15:00:00','2025-01-16 11:00:00'),
('vicky.tsiros@gmail.com','Vicky','Tsiros','+30-698-0123456','1997-12-05','female','Greece','Athens','10434',TRUE,FALSE,8,812.30,812,'social_media','2023-05-22 12:30:00','2025-01-28 13:00:00'),
('panos.efthymiou@gmail.com','Panos','Efthymiou','+30-695-1234568','1983-06-15','male','Greece','Thessaloniki','54630',TRUE,FALSE,5,487.20,487,'referral','2023-02-14 09:30:00','2025-01-22 16:30:00'),
('lena.stamatiou@yahoo.com','Lena','Stamatiou','+30-697-2345679','1994-02-28','female','Greece','Athens','17123',TRUE,TRUE,17,3987.60,3987,'organic','2022-06-30 11:00:00','2025-01-29 10:45:00'),
('aggelos.katsaros@gmail.com','Aggelos','Katsaros','+30-693-3456790','1990-11-12','male','Greece','Piraeus','18545',TRUE,FALSE,4,312.80,312,'paid_search','2023-09-10 14:00:00','2025-01-25 08:30:00'),
('marianna.deligianni@gmail.com','Marianna','Deligianni','+30-694-4567891','1992-08-19','female','Greece','Athens','15231',TRUE,FALSE,9,934.50,934,'email','2022-12-05 10:30:00','2025-01-27 14:15:00'),
('takis.economou@hotmail.com','Takis','Economou','+30-698-5678902','1981-04-05','male','Greece','Athens','12244',TRUE,TRUE,20,4234.80,4234,'organic','2022-07-20 09:00:00','2025-01-30 11:30:00'),
('natalia.hadjidimitriou@gmail.com','Natalia','Hadjidimitriou','+30-695-6789013','1979-01-30','female','Cyprus','Nicosia','1050',TRUE,TRUE,14,2567.90,2567,'referral','2022-10-08 13:00:00','2025-01-28 16:00:00'),
('vasilis.samaras@gmail.com','Vasilis','Samaras','+30-697-7890124','1985-07-14','male','Greece','Athens','10441',TRUE,FALSE,6,634.20,634,'social_media','2023-04-25 11:30:00','2025-01-23 10:15:00'),
('aggeliki.mitsou@yahoo.gr','Aggeliki','Mitsou','+30-693-8901235','1993-03-22','female','Greece','Thessaloniki','54636',TRUE,FALSE,3,289.70,289,'paid_search','2024-01-30 09:00:00','2025-01-18 15:45:00'),
('babis.papagiannakis@gmail.com','Babis','Papagiannakis','+30-694-9012346','1982-10-18','male','Greece','Athens','17232',TRUE,FALSE,7,723.40,723,'organic','2023-01-05 14:15:00','2025-01-26 09:30:00'),
('zoe.christodoulou@gmail.com','Zoe','Christodoulou','+30-698-0123457','1996-06-25','female','Greece','Athens','11362',TRUE,FALSE,4,412.60,412,'social_media','2023-08-15 10:00:00','2025-01-21 14:00:00'),
('nektarios.fountoulakis@outlook.com','Nektarios','Fountoulakis','+30-695-1234569','1984-12-08','male','Greece','Heraklion','71306',TRUE,FALSE,5,523.80,523,'email','2023-03-20 15:30:00','2025-01-24 11:45:00'),
('fofi.tsatsaroni@gmail.com','Fofi','Tsatsaroni','+30-697-2345680','1988-04-15','female','Greece','Athens','10435',TRUE,TRUE,26,5876.30,5876,'referral','2021-12-10 09:45:00','2025-01-30 08:45:00'),
('manolis.kladakis@gmail.com','Manolis','Kladakis','+30-693-3456791','1977-11-02','male','Greece','Chania','73132',TRUE,FALSE,2,167.90,167,'paid_search','2024-02-20 13:30:00','2025-01-17 12:30:00'),
('rania.alexandrou@yahoo.com','Rania','Alexandrou','+30-694-4567892','1991-08-30','female','Greece','Athens','15561',TRUE,FALSE,8,867.40,867,'organic','2022-11-28 10:15:00','2025-01-27 13:45:00'),
('makis.stavrakakis@gmail.com','Makis','Stavrakakis','+30-698-5678903','1994-02-17','male','Greece','Piraeus','18536',TRUE,FALSE,4,389.60,389,'social_media','2023-07-12 11:00:00','2025-01-22 10:00:00'),
('xenia.boutari@gmail.com','Xenia','Boutari','+30-695-6789014','1987-09-14','female','Greece','Athens','10436',TRUE,TRUE,21,4567.20,4567,'organic','2022-04-15 08:30:00','2025-01-29 09:00:00'),
('spyros.diamantis@hotmail.gr','Spyros','Diamantis','+30-697-7890125','1990-05-28','male','Greece','Athens','16232',TRUE,FALSE,6,612.50,612,'email','2023-02-08 14:45:00','2025-01-25 12:00:00'),
('tatiana.beka@gmail.com','Tatiana','Beka','+30-693-8901236','1994-12-20','female','Greece','Athens','11528',TRUE,FALSE,3,256.80,256,'paid_search','2024-01-15 09:30:00','2025-01-19 16:00:00'),
('christos.kalliris@gmail.com','Christos','Kalliris','+30-694-9012347','1992-07-07','male','Greece','Thessaloniki','54642',TRUE,FALSE,9,978.60,978,'social_media','2022-08-25 12:00:00','2025-01-28 10:30:00'),
('evangelia.mitrou@yahoo.gr','Evangelia','Mitrou','+30-698-0123458','1991-03-25','female','Greece','Athens','15123',TRUE,TRUE,16,3456.70,3456,'referral','2022-05-10 10:30:00','2025-01-30 13:15:00'),
('thomas.weber@gmail.com','Thomas','Weber','+49-152-12345678','1983-08-12','male','Germany','Berlin','10115',TRUE,TRUE,12,2345.60,2345,'organic','2022-09-01 11:00:00','2025-01-27 15:30:00'),
('julie.martin@gmail.com','Julie','Martin','+33-612-345678','1990-04-18','female','France','Paris','75001',TRUE,FALSE,7,789.40,789,'paid_search','2023-01-10 13:00:00','2025-01-24 09:30:00'),
('john.smith@outlook.com','John','Smith','+44-7700-123456','1978-11-30','male','UK','London','SW1A 1AA',TRUE,TRUE,18,4123.80,4123,'organic','2022-03-15 09:00:00','2025-01-30 10:00:00'),
('anna.kowalski@gmail.com','Anna','Kowalski','+48-501-234567','1995-06-22','female','Poland','Warsaw','00-001',TRUE,FALSE,5,456.20,456,'social_media','2023-05-20 14:15:00','2025-01-21 11:30:00'),
('marco.rossi@gmail.com','Marco','Rossi','+39-334-5678901','1981-02-14','male','Italy','Rome','00100',TRUE,FALSE,6,623.50,623,'email','2023-02-28 10:00:00','2025-01-23 14:45:00'),
('katarina.novak@yahoo.com','Katarina','Novak','+420-601-234567','1993-09-05','female','Czech Republic','Prague','110 00',TRUE,FALSE,4,378.90,378,'paid_search','2023-08-08 12:30:00','2025-01-20 09:15:00'),
('lars.eriksson@gmail.com','Lars','Eriksson','+46-70-1234567','1976-12-28','male','Sweden','Stockholm','111 20',TRUE,TRUE,23,5234.70,5234,'referral','2021-10-05 08:45:00','2025-01-30 16:30:00'),
('sophie.mueller@gmail.com','Sophie','Mueller','+41-79-1234567','1988-05-15','female','Switzerland','Zurich','8001',TRUE,TRUE,11,2867.30,2867,'organic','2022-07-12 11:30:00','2025-01-28 12:45:00'),
('carlos.garcia@gmail.com','Carlos','Garcia','+34-612-345678','1984-01-20','male','Spain','Madrid','28001',TRUE,FALSE,8,845.60,845,'social_media','2022-12-20 09:30:00','2025-01-26 10:15:00'),
('amelia.johnson@outlook.com','Amelia','Johnson','+1-555-1234567','1992-10-08','female','USA','New York','10001',TRUE,TRUE,20,4890.20,4890,'organic','2022-06-01 14:00:00','2025-01-29 17:00:00'),
('alexandros.dimitriou@gmail.com','Alexandros','Dimitriou','+30-694-1111222','1986-03-10','male','Greece','Athens','17122',TRUE,FALSE,5,487.30,487,'organic','2023-11-01 10:00:00','2025-01-22 09:00:00'),
('stavroula.papageorgiou@gmail.com','Stavroula','Papageorgiou','+30-693-2222333','1991-07-18','female','Greece','Athens','10437',TRUE,FALSE,7,712.40,712,'paid_search','2023-06-15 12:00:00','2025-01-25 14:00:00'),
('nikos.triantafyllou@yahoo.gr','Nikos','Triantafyllou','+30-697-3333444','1980-11-25','male','Greece','Thessaloniki','54644',TRUE,FALSE,3,298.50,298,'social_media','2024-03-01 11:00:00','2025-01-17 10:30:00'),
('elpida.kalogera@gmail.com','Elpida','Kalogera','+30-698-4444555','1994-02-12','female','Greece','Athens','16673',TRUE,FALSE,6,623.80,623,'email','2023-04-20 09:00:00','2025-01-24 15:00:00'),
('apostolos.hatzigeorgiou@gmail.com','Apostolos','Hatzigeorgiou','+30-695-5555666','1977-08-05','male','Greece','Piraeus','18538',TRUE,TRUE,15,3123.40,3123,'referral','2022-08-30 13:30:00','2025-01-29 08:30:00'),
('georgia.tsoukala@gmail.com','Georgia','Tsoukala','+30-697-6666777','1989-05-22','female','Greece','Athens','17124',TRUE,FALSE,4,389.70,389,'organic','2023-10-05 10:30:00','2025-01-20 13:00:00'),
('panos.sakellaropoulos@hotmail.com','Panos','Sakellaropoulos','+30-693-7777888','1983-12-15','male','Greece','Athens','16346',TRUE,FALSE,8,867.20,867,'paid_search','2022-12-10 14:00:00','2025-01-27 11:00:00'),
('chrysanthi.anagnou@gmail.com','Chrysanthi','Anagnou','+30-694-8888999','1996-04-08','female','Greece','Athens','11528',TRUE,FALSE,2,178.90,178,'social_media','2024-02-10 09:30:00','2025-01-15 14:30:00'),
('manos.alexiadis@gmail.com','Manos','Alexiadis','+30-698-9999000','1974-09-28','male','Greece','Thessaloniki','54646',TRUE,TRUE,19,4234.60,4234,'organic','2022-01-25 10:00:00','2025-01-30 12:00:00'),
('thekla.georgopoulos@yahoo.gr','Thekla','Georgopoulos','+30-695-0001111','1990-01-15','female','Greece','Athens','15673',TRUE,FALSE,6,578.30,578,'email','2023-03-12 11:30:00','2025-01-23 16:15:00');

-- ECOMMERCE: Coupons
INSERT INTO ecommerce.coupons (code, discount_type, discount_value, min_order_amount, max_uses, used_count, valid_from, valid_until, is_active) VALUES
('WELCOME10',    'percentage',   10.00, 50.00,   NULL, 1247, '2023-01-01','2025-12-31', TRUE),
('SUMMER25',     'percentage',   25.00, 100.00,  500,  487,  '2024-06-01','2024-08-31', FALSE),
('FREESHIP',     'free_shipping', 0.00, 30.00,   NULL, 892,  '2024-01-01','2025-12-31', TRUE),
('VIP50',        'fixed_amount', 50.00, 200.00,  100,  67,   '2024-01-01','2025-12-31', TRUE),
('FLASH30',      'percentage',   30.00, 75.00,   200,  200,  '2024-11-29','2024-11-29', FALSE),
('NEW20',        'percentage',   20.00, 60.00,   NULL, 2345, '2024-01-01','2025-06-30', TRUE),
('LOYALTY15',    'percentage',   15.00, 0.00,    NULL, 456,  '2024-01-01','2025-12-31', TRUE),
('WINTER10',     'fixed_amount', 10.00, 40.00,   1000, 234,  '2024-12-01','2025-02-28', TRUE);

-- INVENTORY: Warehouses
INSERT INTO inventory.warehouses (name, code, country, city, address, capacity_sqm, is_active) VALUES
('Athens Main Warehouse',    'ATH01', 'Greece',     'Athens',       'Industrial Zone Aspropyrgos, Athens',     12000, TRUE),
('Thessaloniki North Hub',   'THE01', 'Greece',     'Thessaloniki', 'VIPE Thessaloniki Industrial Area',        8000, TRUE),
('Piraeus Port Warehouse',   'PIR01', 'Greece',     'Piraeus',      'Piraeus Port Logistics Zone',              6000, TRUE),
('Frankfurt EU Hub',          'FRA01', 'Germany',    'Frankfurt',    'Frankfurt Logistics Park, Rhine-Main',    20000, TRUE),
('Cyprus Distribution',      'CYP01', 'Cyprus',     'Nicosia',      'Nicosia Industrial Zone',                  3000, TRUE);

-- INVENTORY: Suppliers
INSERT INTO inventory.suppliers (name, contact_name, email, phone, country, city, payment_terms, rating, is_active) VALUES
('TechPro Manufacturing Ltd',    'James Chen',     'james.chen@techpro-mfg.com',    '+886-2-12345678',  'Taiwan',       'Taipei',    30, 4.5, TRUE),
('LuxeAudio GmbH',               'Hans Mueller',   'h.mueller@luxeaudio-de.com',    '+49-69-12345678',  'Germany',      'Hamburg',   45, 4.8, TRUE),
('SwiftMobile Corp',             'Kim Jiyeon',     'jiyeon.kim@swiftmobile.kr',     '+82-2-12345678',   'South Korea',  'Seoul',     30, 4.6, TRUE),
('AlphaGaming Inc',              'Mike Johnson',   'm.johnson@alphagaming.com',     '+1-415-1234567',   'USA',          'San Jose',  30, 4.4, TRUE),
('NordHome AB',                  'Erik Svensson',  'erik.s@nordhome.se',            '+46-8-12345678',   'Sweden',       'Stockholm', 60, 4.7, TRUE),
('ActiveGear BV',                'Pieter van Dam', 'p.vandam@activegear.nl',        '+31-20-1234567',   'Netherlands',  'Amsterdam', 30, 4.3, TRUE),
('UrbanStyle SpA',               'Marco Ferrari',  'm.ferrari@urbanstyle.it',       '+39-02-12345678',  'Italy',        'Milan',     45, 4.6, TRUE),
('VitaHealth AG',                'Dr. Anna Weiss', 'a.weiss@vitahealth.ch',         '+41-44-1234567',   'Switzerland',  'Basel',     30, 4.9, TRUE),
('SmartKids APS',                'Lars Nielsen',   'lars.n@smartkids.dk',           '+45-33-123456',    'Denmark',      'Copenhagen',30, 4.5, TRUE),
('Global Electronics Parts',     'Sarah Thompson', 's.thompson@globalparts.com',    '+44-20-12345678',  'UK',           'London',    30, 4.2, TRUE);

-- INVENTORY: Stock (products across warehouses)
INSERT INTO inventory.stock (product_id, warehouse_id, quantity_on_hand, quantity_reserved, reorder_point, reorder_quantity, last_restocked_at) VALUES
(1,1,145,23,20,50,'2025-01-10 09:00:00'),(1,4,18,5,15,30,'2025-01-12 10:00:00'),
(2,1,234,45,30,80,'2025-01-08 09:00:00'),(2,4,12,12,20,60,'2025-01-09 10:00:00'),
(3,1,567,89,50,150,'2025-01-15 09:00:00'),(3,2,234,34,40,100,'2025-01-14 10:00:00'),(3,4,445,67,60,200,'2025-01-15 10:00:00'),
(4,1,789,123,80,200,'2025-01-15 09:00:00'),(4,2,345,56,60,150,'2025-01-14 10:00:00'),
(5,1,456,78,40,100,'2025-01-12 09:00:00'),(5,4,35,8,30,80,'2025-01-13 10:00:00'),
(6,1,678,112,60,150,'2025-01-10 09:00:00'),(6,2,289,45,50,120,'2025-01-11 10:00:00'),
(7,1,234,45,20,50,'2025-01-15 09:00:00'),(7,4,20,20,15,40,'2025-01-14 10:00:00'),
(8,1,456,78,40,100,'2025-01-13 09:00:00'),(8,4,234,34,30,80,'2025-01-12 10:00:00'),
(9,1,89,12,10,25,'2025-01-05 09:00:00'),(9,4,12,5,8,20,'2025-01-06 10:00:00'),
(10,1,134,18,15,40,'2025-01-08 09:00:00'),
(11,1,245,34,30,80,'2025-01-10 09:00:00'),(11,2,28,12,20,50,'2025-01-09 10:00:00'),
(12,1,334,45,40,100,'2025-01-08 09:00:00'),
(13,1,456,67,50,120,'2025-01-12 09:00:00'),(13,2,234,34,40,100,'2025-01-11 10:00:00'),
(14,1,567,89,60,150,'2025-01-10 09:00:00'),
(15,1,15,6,10,25,'2025-01-06 09:00:00'),(15,4,45,8,8,20,'2025-01-07 10:00:00'),
(16,1,10,10,15,40,'2025-01-08 09:00:00'),
(17,1,289,45,30,80,'2025-01-10 09:00:00'),(17,4,156,23,20,60,'2025-01-11 10:00:00'),
(18,1,1234,189,150,300,'2025-01-15 09:00:00'),(18,2,567,78,100,200,'2025-01-14 10:00:00'),
(19,1,678,112,80,200,'2025-01-13 09:00:00'),(19,2,345,56,60,150,'2025-01-12 10:00:00'),
(20,1,234,34,30,80,'2025-01-10 09:00:00'),
(21,1,345,56,40,100,'2025-01-08 09:00:00'),(21,4,189,28,30,80,'2025-01-09 10:00:00'),
(22,1,567,89,60,150,'2025-01-12 09:00:00'),(22,4,289,45,50,120,'2025-01-13 10:00:00'),
(23,1,123,18,15,40,'2025-01-06 09:00:00'),(23,4,14,5,10,25,'2025-01-07 10:00:00'),
(24,1,456,67,50,120,'2025-01-10 09:00:00'),(24,2,234,34,40,100,'2025-01-09 10:00:00'),
(25,1,289,45,30,80,'2025-01-12 09:00:00'),
(26,1,456,67,50,120,'2025-01-08 09:00:00'),
(27,1,345,56,40,100,'2025-01-10 09:00:00'),(27,2,178,28,30,80,'2025-01-09 10:00:00'),
(28,1,38,10,30,80,'2025-01-06 09:00:00'),
(29,1,789,123,100,250,'2025-01-15 09:00:00'),(29,2,345,56,60,150,'2025-01-14 10:00:00'),
(30,1,456,67,50,120,'2025-01-12 09:00:00'),(30,2,234,34,40,100,'2025-01-11 10:00:00');

-- FINANCE: Chart of Accounts
INSERT INTO finance.accounts (account_code, name, account_type, parent_id, currency) VALUES
('1000','Assets','asset',NULL,'EUR'),
('1100','Current Assets','asset',1,'EUR'),
('1110','Cash and Cash Equivalents','asset',2,'EUR'),
('1120','Accounts Receivable','asset',2,'EUR'),
('1130','Inventory','asset',2,'EUR'),
('1200','Fixed Assets','asset',1,'EUR'),
('1210','Property and Equipment','asset',7,'EUR'),
('2000','Liabilities','liability',NULL,'EUR'),
('2100','Current Liabilities','liability',9,'EUR'),
('2110','Accounts Payable','liability',10,'EUR'),
('2120','Accrued Expenses','liability',10,'EUR'),
('3000','Equity','equity',NULL,'EUR'),
('3100','Retained Earnings','equity',13,'EUR'),
('4000','Revenue','revenue',NULL,'EUR'),
('4100','Product Sales Revenue','revenue',15,'EUR'),
('4200','Shipping Revenue','revenue',15,'EUR'),
('5000','Expenses','expense',NULL,'EUR'),
('5100','Cost of Goods Sold','expense',18,'EUR'),
('5200','Payroll Expense','expense',18,'EUR'),
('5300','Marketing Expense','expense',18,'EUR'),
('5400','Technology Expense','expense',18,'EUR'),
('5500','Office & Administrative','expense',18,'EUR'),
('5600','Shipping & Logistics','expense',18,'EUR');

-- SUPPORT: SLA Policies
INSERT INTO support.sla_policies (priority, first_response_hours, resolution_hours) VALUES
('low',      24, 120),
('medium',   8,  48),
('high',     4,  24),
('urgent',   1,  8),
('critical', 0,  2);

-- SUPPORT: Agents (link to CS employees)
INSERT INTO support.agents (employee_id, skills, max_tickets, is_available) VALUES
(28, ARRAY['billing','technical','escalation'], 10, TRUE),
(29, ARRAY['billing','shipping','returns'], 20, TRUE),
(30, ARRAY['general','shipping','product'], 20, TRUE),
(31, ARRAY['billing','general'], 20, TRUE),
(32, ARRAY['technical','product'], 20, TRUE),
(45, ARRAY['general','social'], 20, TRUE);

-- ANALYTICS: Marketing Campaigns
INSERT INTO analytics.marketing_campaigns (name, channel, start_date, end_date, budget, spent, impressions, clicks, conversions, revenue_generated, status) VALUES
('Summer Electronics Sale 2024',   'paid_search',  '2024-06-01','2024-08-31',  50000,  48234,  2340000, 45600, 1234, 198450, 'completed'),
('Back to School 2024',            'social_media', '2024-08-15','2024-09-15',  25000,  24567,  1560000, 28900,  678, 89340,  'completed'),
('Black Friday 2024',              'email',        '2024-11-25','2024-11-30',  15000,  14234,   890000, 67800, 2345, 456780, 'completed'),
('Christmas Campaign 2024',        'paid_search',  '2024-12-01','2024-12-31',  40000,  38456,  3120000, 56700, 1567, 312450, 'completed'),
('New Year New Tech 2025',         'social_media', '2025-01-01','2025-01-31',  20000,  12345,  1230000, 23400,  456, 67890,  'active'),
('Valentine''s Gifts Campaign',    'email',        '2025-02-01','2025-02-14',  10000,   1234,   450000,  8900,  123, 18900,  'active'),
('Spring Brand Awareness',         'display',      '2025-03-01','2025-05-31',  30000,      0,         0,     0,    0,     0, 'draft'),
('Influencer Partnership Q1',      'influencer',   '2025-01-15','2025-03-31',  18000,   4500,   780000, 12300,  234, 34560,  'active'),
('Affiliate Program Launch',       'affiliate',    '2024-10-01','2025-12-31',   5000,   3456,   560000,  9800,  345, 56780,  'active'),
('SEO Content Campaign',           'seo',          '2024-01-01','2025-12-31',  12000,  11234,  8900000,234500, 4567,789000, 'active');

-- ANALYTICS: A/B Tests
INSERT INTO analytics.ab_tests (test_name, hypothesis, variant_a_name, variant_b_name, metric, variant_a_value, variant_b_value, sample_size_a, sample_size_b, p_value, winner, start_date, end_date, status) VALUES
('Checkout Button Color','Red button increases checkout conversion','Blue Button','Red Button','conversion_rate',0.0342,0.0401,12450,12389,0.0023,'B','2024-10-01','2024-10-31','completed'),
('Homepage Hero Layout','Two-column layout improves engagement','Single Column','Two Column','time_on_page',124.5,156.8,8900,8845,0.0001,'B','2024-09-15','2024-10-15','completed'),
('Product Image Count','More images reduces return rate','3 Images','6 Images','return_rate',0.0823,0.0612,15600,15432,0.0089,'B','2024-08-01','2024-09-30','completed'),
('Free Shipping Threshold','Lower threshold increases AOV','€50 Threshold','€30 Threshold','avg_order_value',78.40,82.10,9800,9756,0.0234,'B','2024-11-01','2024-11-30','completed'),
('Email Subject Lines','Personalized subjects improve open rate','Generic Subject','Personalized Subject','open_rate',0.2134,0.2678,45000,45000,0.0000,'B','2024-07-01','2024-07-31','completed'),
('Recommendation Algorithm','ML recommendations increase CTR','Popularity Based','ML Based','ctr',0.0456,0.0589,23400,23234,0.0045,'B','2024-12-01','2025-01-31','running'),
('Mobile Checkout Flow','Simplified flow reduces abandonment','Standard Flow','Simplified Flow','abandonment_rate',0.6234,NULL,8900,0,NULL,NULL,'2025-01-15',NULL,'running');

-- Now generate ORDERS (300 orders over 2 years)
-- Using a DO block to insert orders programmatically
DO $$
DECLARE
  order_statuses TEXT[] := ARRAY['delivered','delivered','delivered','delivered','shipped','processing','confirmed','cancelled','returned','refunded'];
  payment_methods TEXT[] := ARRAY['credit_card','credit_card','credit_card','debit_card','paypal','bank_transfer','klarna'];
  customer_ids INT[];
  product_ids INT[];
  cust_id INT;
  prod_id INT;
  ord_id INT;
  qty INT;
  unit_p NUMERIC;
  sub NUMERIC;
  disc NUMERIC;
  ship NUMERIC;
  tax NUMERIC;
  tot NUMERIC;
  stat TEXT;
  pay_stat TEXT;
  pay_meth TEXT;
  ord_date TIMESTAMP;
  ship_date TIMESTAMP;
  del_date TIMESTAMP;
  i INT;
  j INT;
  items_count INT;
  order_num TEXT;
  coupon TEXT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers) INTO customer_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.products WHERE is_active = TRUE) INTO product_ids;

  FOR i IN 1..300 LOOP
    cust_id := customer_ids[1 + floor(random() * array_length(customer_ids,1))::INT];
    stat := order_statuses[1 + floor(random() * array_length(order_statuses,1))::INT];
    pay_meth := payment_methods[1 + floor(random() * array_length(payment_methods,1))::INT];
    ord_date := NOW() - (random() * 730)::INT * INTERVAL '1 day' - (random() * 23)::INT * INTERVAL '1 hour';

    IF stat = 'delivered' THEN
      pay_stat := 'paid';
      ship_date := ord_date + INTERVAL '2 days';
      del_date := ord_date + INTERVAL '5 days';
    ELSIF stat IN ('shipped','processing') THEN
      pay_stat := 'paid';
      ship_date := CASE WHEN stat = 'shipped' THEN ord_date + INTERVAL '2 days' ELSE NULL END;
      del_date := NULL;
    ELSIF stat = 'cancelled' THEN
      pay_stat := CASE WHEN random() > 0.5 THEN 'refunded' ELSE 'failed' END;
      ship_date := NULL; del_date := NULL;
    ELSIF stat IN ('returned','refunded') THEN
      pay_stat := 'refunded';
      ship_date := ord_date + INTERVAL '2 days';
      del_date := ord_date + INTERVAL '5 days';
    ELSE
      pay_stat := 'paid';
      ship_date := NULL; del_date := NULL;
    END IF;

    order_num := 'ORD-' || TO_CHAR(ord_date,'YYYYMMDD') || '-' || LPAD(i::TEXT, 5, '0');
    coupon := CASE WHEN random() > 0.75 THEN (ARRAY['WELCOME10','FREESHIP','LOYALTY15','NEW20'])[1 + floor(random()*4)::INT] ELSE NULL END;

    INSERT INTO ecommerce.orders (order_number, customer_id, status, payment_status, payment_method,
      subtotal, discount_amount, shipping_amount, tax_amount, total_amount,
      currency, shipping_country, shipping_city, coupon_code, ordered_at, shipped_at, delivered_at)
    VALUES (order_num, cust_id, stat, pay_stat, pay_meth,
      0, 0, CASE WHEN random() > 0.4 THEN 4.99 ELSE 0 END, 0, 0,
      'EUR', 'Greece', 'Athens', coupon, ord_date, ship_date, del_date)
    RETURNING id INTO ord_id;

    -- Order items (1-4 items per order)
    items_count := 1 + floor(random() * 3)::INT;
    sub := 0;

    FOR j IN 1..items_count LOOP
      prod_id := product_ids[1 + floor(random() * array_length(product_ids,1))::INT];
      qty := 1 + floor(random() * 3)::INT;

      SELECT COALESCE(sale_price, base_price) INTO unit_p FROM ecommerce.products WHERE id = prod_id;
      disc := CASE WHEN random() > 0.8 THEN round((random() * 20)::NUMERIC, 0) ELSE 0 END;

      INSERT INTO ecommerce.order_items (order_id, product_id, quantity, unit_price, discount_pct, line_total)
      VALUES (ord_id, prod_id, qty, unit_p, disc, round(unit_p * qty * (1 - disc/100), 2));

      sub := sub + round(unit_p * qty * (1 - disc/100), 2);
    END LOOP;

    disc := CASE WHEN coupon IS NOT NULL THEN round(sub * 0.1, 2) ELSE 0 END;
    ship := CASE WHEN sub > 50 THEN 0 ELSE 4.99 END;
    tax := round((sub - disc) * 0.24, 2);
    tot := sub - disc + ship + tax;

    UPDATE ecommerce.orders SET subtotal = sub, discount_amount = disc, shipping_amount = ship, tax_amount = tax, total_amount = tot WHERE id = ord_id;
  END LOOP;
END $$;

-- Guaranteed seed: customers 11 (Petros Andreou) and 27 (Vasilis Samaras) each have
-- exactly one order in every month of Q1 2026 so the test question
-- "Which customers placed at least one order in every month of the last quarter?"
-- always returns 2 rows regardless of the random seed above.
DO $$
DECLARE
  prod_price NUMERIC(12,2);
BEGIN
  SELECT base_price INTO prod_price FROM ecommerce.products WHERE id = 22;

  INSERT INTO ecommerce.orders
    (order_number, customer_id, status, payment_status, payment_method,
     subtotal, discount_amount, shipping_amount, tax_amount, total_amount,
     currency, shipping_country, shipping_city, ordered_at, shipped_at, delivered_at)
  VALUES
    ('ORD-SEED-C11-JAN', 11, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-01-10 10:00:00', '2026-01-12 10:00:00', '2026-01-15 10:00:00'),
    ('ORD-SEED-C11-FEB', 11, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-02-10 10:00:00', '2026-02-12 10:00:00', '2026-02-15 10:00:00'),
    ('ORD-SEED-C11-MAR', 11, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-03-10 10:00:00', '2026-03-12 10:00:00', '2026-03-15 10:00:00'),
    ('ORD-SEED-C27-JAN', 27, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-01-20 10:00:00', '2026-01-22 10:00:00', '2026-01-25 10:00:00'),
    ('ORD-SEED-C27-FEB', 27, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-02-18 10:00:00', '2026-02-20 10:00:00', '2026-02-23 10:00:00'),
    ('ORD-SEED-C27-MAR', 27, 'delivered', 'paid', 'credit_card', prod_price, 0, 0, round(prod_price*0.24,2), round(prod_price*1.24,2), 'EUR', 'Greece', 'Athens', '2026-03-22 10:00:00', '2026-03-24 10:00:00', '2026-03-27 10:00:00');

  -- Add one order_item per seed order so joins on order_items don't leave them orphaned
  INSERT INTO ecommerce.order_items (order_id, product_id, quantity, unit_price, discount_pct, line_total)
  SELECT o.id, 22, 1, prod_price, 0, prod_price
  FROM ecommerce.orders o
  WHERE o.order_number LIKE 'ORD-SEED-%';
END $$;

-- REVIEWS (80 reviews)
DO $$
DECLARE
  review_titles TEXT[] := ARRAY['Great product!','Very satisfied','Excellent quality','Good value','Highly recommend','Perfect for my needs','Fast delivery','As described','Love it!','Could be better','Good but not perfect','Amazing quality'];
  cust_ids INT[];
  prod_ids INT[];
  ord_ids INT[];
  i INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.products) INTO prod_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.orders WHERE status = 'delivered' LIMIT 200) INTO ord_ids;

  FOR i IN 1..80 LOOP
    BEGIN
      INSERT INTO ecommerce.reviews (product_id, customer_id, order_id, rating, title, body, is_verified_purchase, helpful_votes)
      VALUES (
        prod_ids[1 + floor(random() * array_length(prod_ids,1))::INT],
        cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT],
        ord_ids[1 + floor(random() * array_length(ord_ids,1))::INT],
        (3 + floor(random() * 3))::INT,
        review_titles[1 + floor(random() * array_length(review_titles,1))::INT],
        'This product exceeded my expectations. The quality is outstanding and delivery was fast.',
        random() > 0.3,
        floor(random() * 50)::INT
      );
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
  END LOOP;
END $$;

-- SUPPORT: Tickets (100 tickets)
DO $$
DECLARE
  categories TEXT[] := ARRAY['billing','shipping','product','technical','return','complaint','general','fraud'];
  priorities TEXT[] := ARRAY['low','low','medium','medium','medium','high','urgent','critical'];
  statuses TEXT[] := ARRAY['resolved','resolved','resolved','closed','open','pending_customer','new'];
  channels TEXT[] := ARRAY['email','email','chat','phone','web_form','social'];
  subjects TEXT[] := ARRAY[
    'Order not received','Payment issue','Product damaged','Wrong item received',
    'Refund request','Cannot login to account','Tracking number not working',
    'Price discrepancy','Subscription cancellation','Technical issue with website',
    'Request for invoice','Delivery delay','Product quality complaint','Billing error'
  ];
  cust_ids INT[];
  agent_ids INT[];
  cust_id INT;
  agent_id INT;
  tick_id INT;
  cat TEXT; pri TEXT; stat TEXT; chan TEXT; subj TEXT;
  created TIMESTAMP;
  resolved TIMESTAMP;
  i INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers LIMIT 60) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM support.agents) INTO agent_ids;

  FOR i IN 1..100 LOOP
    cust_id := cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT];
    agent_id := agent_ids[1 + floor(random() * array_length(agent_ids,1))::INT];
    cat  := categories[1 + floor(random() * array_length(categories,1))::INT];
    pri  := priorities[1 + floor(random() * array_length(priorities,1))::INT];
    stat := statuses[1 + floor(random() * array_length(statuses,1))::INT];
    chan := channels[1 + floor(random() * array_length(channels,1))::INT];
    subj := subjects[1 + floor(random() * array_length(subjects,1))::INT];
    created := NOW() - (random() * 365)::INT * INTERVAL '1 day';
    resolved := CASE WHEN stat IN ('resolved','closed') THEN created + (1 + floor(random() * 48))::INT * INTERVAL '1 hour' ELSE NULL END;

    INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
    VALUES (
      'TKT-' || LPAD(i::TEXT, 6, '0'),
      cust_id, agent_id, cat, pri, stat, subj,
      'Customer reported: ' || subj || '. Details have been logged and are being investigated.',
      chan,
      CASE WHEN stat IN ('resolved','closed') THEN (3 + floor(random() * 3))::INT ELSE NULL END,
      created + INTERVAL '2 hours',
      resolved,
      random() > 0.85,
      created
    ) RETURNING id INTO tick_id;

    INSERT INTO support.ticket_messages (ticket_id, sender_type, sender_id, message, is_internal)
    VALUES
      (tick_id, 'customer', cust_id, 'Hello, I need help with: ' || subj, FALSE),
      (tick_id, 'agent', agent_id, 'Thank you for contacting us. We are looking into this issue and will get back to you shortly.', FALSE);
  END LOOP;
END $$;

-- SUPPORT: This-week tickets — explicit rows so "broken down by category" always returns data
-- created_at anchored to date_trunc('week', CURRENT_TIMESTAMP) so they always land in the current ISO week
DO $$
DECLARE
  week_start TIMESTAMP := date_trunc('week', CURRENT_TIMESTAMP);
  cust_ids   INT[];
  agent_ids  INT[];
  tick_id    INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers ORDER BY id LIMIT 30) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM support.agents ORDER BY id)               INTO agent_ids;

  -- billing (5 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00001', cust_ids[1],  agent_ids[1], 'billing',   'high',   'open',     'Incorrect charge on invoice',          'Customer reports a charge that does not match their order total.',          'email',    NULL, week_start + INTERVAL '2 hours', NULL,                              FALSE, week_start + INTERVAL '0 hours'),
    ('TKT-W00002', cust_ids[2],  agent_ids[1], 'billing',   'medium', 'resolved', 'Double billing on subscription',       'Customer was billed twice for the same subscription period.',               'web_form', 4,    week_start + INTERVAL '26 hours',week_start + INTERVAL '28 hours',  FALSE, week_start + INTERVAL '24 hours'),
    ('TKT-W00003', cust_ids[3],  agent_ids[2], 'billing',   'low',    'closed',   'VAT rate applied incorrectly',         'VAT percentage does not match the rate for the customer''s country.',      'email',    5,    week_start + INTERVAL '50 hours',week_start + INTERVAL '52 hours',  FALSE, week_start + INTERVAL '48 hours'),
    ('TKT-W00004', cust_ids[4],  agent_ids[1], 'billing',   'urgent', 'open',     'Refund not processed after 14 days',   'Customer submitted refund request two weeks ago with no update.',          'phone',    NULL, week_start + INTERVAL '73 hours',NULL,                              TRUE,  week_start + INTERVAL '72 hours'),
    ('TKT-W00005', cust_ids[5],  agent_ids[2], 'billing',   'medium', 'pending_customer', 'Promo code not applied at checkout', 'Discount code was accepted but full price was charged.',              'chat',     NULL, week_start + INTERVAL '86 hours',NULL,                              FALSE, week_start + INTERVAL '84 hours');

  -- shipping (4 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00006', cust_ids[6],  agent_ids[3], 'shipping',  'high',   'open',     'Package not delivered — tracking stalled',  'Tracking shows "in transit" for 6 days with no update.',           'email',    NULL, week_start + INTERVAL '2 hours', NULL,                              FALSE, week_start + INTERVAL '1 hour'),
    ('TKT-W00007', cust_ids[7],  agent_ids[3], 'shipping',  'medium', 'resolved', 'Wrong item delivered',                      'Customer received a different product than what was ordered.',      'web_form', 3,    week_start + INTERVAL '27 hours',week_start + INTERVAL '30 hours',  FALSE, week_start + INTERVAL '25 hours'),
    ('TKT-W00008', cust_ids[8],  agent_ids[3], 'shipping',  'low',    'closed',   'Delivery attempted to wrong address',        'Carrier attempted delivery at an old address despite update.',      'phone',    4,    week_start + INTERVAL '49 hours',week_start + INTERVAL '53 hours',  FALSE, week_start + INTERVAL '47 hours'),
    ('TKT-W00009', cust_ids[9],  agent_ids[3], 'shipping',  'high',   'escalated','Fragile item arrived damaged',               'Glass item was not packed adequately and arrived broken.',          'email',    NULL, week_start + INTERVAL '74 hours',NULL,                              TRUE,  week_start + INTERVAL '72 hours');

  -- product (4 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00010', cust_ids[10], agent_ids[5], 'product',   'medium', 'open',     'Missing parts in product box',              'Accessory listed in the manual was not included in the box.',       'web_form', NULL, week_start + INTERVAL '3 hours', NULL,                              FALSE, week_start + INTERVAL '2 hours'),
    ('TKT-W00011', cust_ids[11], agent_ids[5], 'product',   'high',   'open',     'Product stopped working after 3 days',      'Device powers off randomly and will not restart.',                  'chat',     NULL, week_start + INTERVAL '28 hours',NULL,                              FALSE, week_start + INTERVAL '26 hours'),
    ('TKT-W00012', cust_ids[12], agent_ids[5], 'product',   'low',    'resolved', 'Product description mismatch',              'Website listed feature X but the delivered unit does not have it.', 'email',    4,    week_start + INTERVAL '51 hours',week_start + INTERVAL '54 hours',  FALSE, week_start + INTERVAL '49 hours'),
    ('TKT-W00013', cust_ids[13], agent_ids[4], 'product',   'medium', 'pending_internal','Warranty claim for defective screen', 'Screen has dead pixels within the 2-year warranty period.',        'web_form', NULL, week_start + INTERVAL '75 hours',NULL,                              FALSE, week_start + INTERVAL '73 hours');

  -- technical (3 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00014', cust_ids[14], agent_ids[5], 'technical', 'critical','open',    'Cannot log in — account locked',            'Account locked after multiple failed attempts; 2FA not working.',   'chat',     NULL, week_start + INTERVAL '1 hour',  NULL,                              FALSE, week_start + INTERVAL '0 hours' + INTERVAL '30 minutes'),
    ('TKT-W00015', cust_ids[15], agent_ids[4], 'technical', 'high',   'open',     'API integration returning 500 errors',      'Third-party API returning 500 on every call since last night.',     'email',    NULL, week_start + INTERVAL '26 hours',NULL,                              FALSE, week_start + INTERVAL '24 hours' + INTERVAL '45 minutes'),
    ('TKT-W00016', cust_ids[16], agent_ids[5], 'technical', 'medium', 'resolved', 'Mobile app crashes on iOS 17',              'App crashes immediately after opening on iOS 17.4.',                'web_form', 5,    week_start + INTERVAL '52 hours',week_start + INTERVAL '56 hours',  FALSE, week_start + INTERVAL '50 hours');

  -- return (3 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00017', cust_ids[17], agent_ids[2], 'return',    'medium', 'open',     'Return label not received',                 'Return was approved 5 days ago but no shipping label was sent.',    'email',    NULL, week_start + INTERVAL '3 hours', NULL,                              FALSE, week_start + INTERVAL '2 hours' + INTERVAL '10 minutes'),
    ('TKT-W00018', cust_ids[18], agent_ids[2], 'return',    'low',    'resolved', 'Return accepted but refund not issued',     'Item received by warehouse 10 days ago; refund still pending.',     'phone',    3,    week_start + INTERVAL '29 hours',week_start + INTERVAL '32 hours',  FALSE, week_start + INTERVAL '27 hours'),
    ('TKT-W00019', cust_ids[19], agent_ids[2], 'return',    'medium', 'pending_customer','Customer wants exchange not refund',  'Requesting product swap instead of money back.',                    'chat',     NULL, week_start + INTERVAL '76 hours',NULL,                              FALSE, week_start + INTERVAL '74 hours');

  -- complaint (3 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00020', cust_ids[20], agent_ids[4], 'complaint', 'high',   'escalated','Agent was rude during previous call',       'Customer reports unprofessional behaviour by support agent.',       'phone',    NULL, week_start + INTERVAL '1 hour',  NULL,                              TRUE,  week_start + INTERVAL '0 hours' + INTERVAL '15 minutes'),
    ('TKT-W00021', cust_ids[21], agent_ids[4], 'complaint', 'medium', 'open',     'Slow response times on open ticket',        'Ticket open for 4 days with no meaningful update.',                 'email',    NULL, week_start + INTERVAL '98 hours',NULL,                              FALSE, week_start + INTERVAL '96 hours'),
    ('TKT-W00022', cust_ids[22], agent_ids[6], 'complaint', 'low',    'closed',   'Website checkout experience very poor',     'Multiple redirects and session timeouts during checkout.',          'web_form', 2,    week_start + INTERVAL '53 hours',week_start + INTERVAL '55 hours',  FALSE, week_start + INTERVAL '51 hours');

  -- general (2 tickets)
  INSERT INTO support.tickets (ticket_number, customer_id, assigned_agent_id, category, priority, status, subject, description, channel, satisfaction_score, first_response_at, resolved_at, sla_breached, created_at)
  VALUES
    ('TKT-W00023', cust_ids[23], agent_ids[6], 'general',   'low',    'resolved', 'How do I update my billing address?',       'Customer asks how to change their address on file.',                'chat',     5,    week_start + INTERVAL '2 hours', week_start + INTERVAL '3 hours',   FALSE, week_start + INTERVAL '1 hour' + INTERVAL '30 minutes'),
    ('TKT-W00024', cust_ids[24], agent_ids[6], 'general',   'low',    'open',     'Request for itemised invoice copy',         'Customer needs a detailed PDF invoice for their accountant.',       'email',    NULL, week_start + INTERVAL '99 hours',NULL,                              FALSE, week_start + INTERVAL '97 hours');

  -- Insert opening messages for every this-week ticket
  FOR tick_id IN
    SELECT id FROM support.tickets WHERE ticket_number LIKE 'TKT-W%' ORDER BY id
  LOOP
    INSERT INTO support.ticket_messages (ticket_id, sender_type, sender_id, message, is_internal)
    VALUES (tick_id, 'customer', cust_ids[1], 'I need help with this issue, please.', FALSE);
  END LOOP;
END $$;

-- FINANCE: Transactions (200 transactions)
DO $$
DECLARE
  trans_types TEXT[] := ARRAY['revenue','revenue','revenue','expense','expense','payroll','tax'];
  dept_ids INT[];
  emp_ids INT[];
  account_ids INT[];
  i INT;
  t_type TEXT;
  t_amount NUMERIC;
  t_account INT;
  t_dept INT;
  t_date DATE;
  t_ref VARCHAR;
BEGIN
  SELECT ARRAY(SELECT id FROM hr.departments) INTO dept_ids;
  SELECT ARRAY(SELECT id FROM hr.employees WHERE is_active = TRUE LIMIT 15) INTO emp_ids;

  FOR i IN 1..200 LOOP
    t_type := trans_types[1 + floor(random() * array_length(trans_types,1))::INT];
    t_date := CURRENT_DATE - (random() * 730)::INT;
    t_dept := dept_ids[1 + floor(random() * array_length(dept_ids,1))::INT];
    t_ref := 'TXN-' || TO_CHAR(t_date,'YYYYMMDD') || '-' || LPAD(i::TEXT,5,'0');

    IF t_type = 'revenue' THEN
      t_amount := round((1000 + random() * 49000)::NUMERIC, 2);
      t_account := 15; -- Product Sales Revenue
    ELSIF t_type = 'payroll' THEN
      t_amount := round((20000 + random() * 80000)::NUMERIC, 2);
      t_account := 19; -- Payroll Expense
    ELSIF t_type = 'tax' THEN
      t_amount := round((5000 + random() * 25000)::NUMERIC, 2);
      t_account := 22; -- Admin
    ELSE
      t_amount := round((500 + random() * 19500)::NUMERIC, 2);
      t_account := CASE t_type WHEN 'expense' THEN 20 ELSE 21 END;
    END IF;

    INSERT INTO finance.transactions (transaction_ref, transaction_date, description, transaction_type, amount, currency, account_id, department_id, status)
    VALUES (t_ref, t_date, t_type || ' transaction for ' || TO_CHAR(t_date,'Month YYYY'), t_type, t_amount, 'EUR', t_account, t_dept, 'posted');
  END LOOP;
END $$;

-- FINANCE: Invoices (50 customer invoices)
DO $$
DECLARE
  cust_ids INT[];
  i INT;
  inv_date DATE;
  due_date DATE;
  sub NUMERIC;
  tax NUMERIC;
  tot NUMERIC;
  stat TEXT;
  paid NUMERIC;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers LIMIT 50) INTO cust_ids;

  FOR i IN 1..50 LOOP
    inv_date := CURRENT_DATE - (random() * 365)::INT;
    due_date := inv_date + 30;
    sub := round((100 + random() * 4900)::NUMERIC, 2);
    tax := round(sub * 0.24, 2);
    tot := sub + tax;
    stat := CASE
      WHEN inv_date < CURRENT_DATE - 60 THEN 'paid'
      WHEN inv_date < CURRENT_DATE - 30 THEN (ARRAY['paid','overdue'])[1+floor(random()*2)::INT]
      ELSE (ARRAY['sent','paid','partial'])[1+floor(random()*3)::INT]
    END;
    paid := CASE stat WHEN 'paid' THEN tot WHEN 'partial' THEN round(tot * 0.5, 2) ELSE 0 END;

    INSERT INTO finance.invoices (invoice_number, invoice_type, customer_id, issue_date, due_date, paid_date, subtotal, tax_amount, total_amount, paid_amount, currency, status)
    VALUES (
      'INV-' || TO_CHAR(inv_date,'YYYYMMDD') || '-' || LPAD(i::TEXT,4,'0'),
      'customer',
      cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT],
      inv_date, due_date,
      CASE WHEN stat = 'paid' THEN inv_date + floor(random() * 28)::INT ELSE NULL END,
      sub, tax, tot, paid, 'EUR', stat
    );
  END LOOP;
END $$;

-- FINANCE: Budgets — 2024 (all quarters, complete)
INSERT INTO finance.budgets (department_id, account_id, fiscal_year, fiscal_quarter, budgeted_amount, actual_amount)
SELECT
  d.id,
  a.id,
  2024,
  q.q,
  CASE a.account_type
    WHEN 'expense' THEN round((d.budget / 4 * (0.8 + random() * 0.4))::NUMERIC, 2)
    WHEN 'revenue' THEN round((500000 * (0.8 + random() * 0.4))::NUMERIC, 2)
    ELSE round((100000 * (0.8 + random() * 0.4))::NUMERIC, 2)
  END,
  CASE a.account_type
    WHEN 'expense' THEN round((d.budget / 4 * (0.7 + random() * 0.6))::NUMERIC, 2)
    WHEN 'revenue' THEN round((500000 * (0.9 + random() * 0.3))::NUMERIC, 2)
    ELSE round((100000 * (0.7 + random() * 0.5))::NUMERIC, 2)
  END
FROM hr.departments d
CROSS JOIN (SELECT id, account_type FROM finance.accounts WHERE account_type IN ('expense','revenue') LIMIT 5) a
CROSS JOIN (SELECT generate_series(1,4) AS q) q;

-- FINANCE: Budgets — 2025 (all quarters, complete)
INSERT INTO finance.budgets (department_id, account_id, fiscal_year, fiscal_quarter, budgeted_amount, actual_amount)
SELECT
  d.id,
  a.id,
  2025,
  q.q,
  CASE a.account_type
    WHEN 'expense' THEN round((d.budget / 4 * (0.85 + random() * 0.3))::NUMERIC, 2)
    WHEN 'revenue' THEN round((520000 * (0.85 + random() * 0.3))::NUMERIC, 2)
    ELSE round((105000 * (0.85 + random() * 0.3))::NUMERIC, 2)
  END,
  CASE a.account_type
    WHEN 'expense' THEN round((d.budget / 4 * (0.75 + random() * 0.5))::NUMERIC, 2)
    WHEN 'revenue' THEN round((520000 * (0.88 + random() * 0.25))::NUMERIC, 2)
    ELSE round((105000 * (0.75 + random() * 0.45))::NUMERIC, 2)
  END
FROM hr.departments d
CROSS JOIN (SELECT id, account_type FROM finance.accounts WHERE account_type IN ('expense','revenue') LIMIT 5) a
CROSS JOIN (SELECT generate_series(1,4) AS q) q;

-- FINANCE: Budgets — 2026 Q1 (complete) and Q2 (in progress: actual reflects ~5 weeks of 13-week quarter)
INSERT INTO finance.budgets (department_id, account_id, fiscal_year, fiscal_quarter, budgeted_amount, actual_amount)
SELECT
  d.id,
  a.id,
  2026,
  q.q,
  CASE a.account_type
    WHEN 'expense' THEN round((d.budget / 4 * (0.9 + random() * 0.2))::NUMERIC, 2)
    WHEN 'revenue' THEN round((540000 * (0.9 + random() * 0.2))::NUMERIC, 2)
    ELSE round((110000 * (0.9 + random() * 0.2))::NUMERIC, 2)
  END,
  CASE
    -- Q1 is complete
    WHEN q.q = 1 THEN
      CASE a.account_type
        WHEN 'expense' THEN round((d.budget / 4 * (0.8 + random() * 0.4))::NUMERIC, 2)
        WHEN 'revenue' THEN round((540000 * (0.9 + random() * 0.2))::NUMERIC, 2)
        ELSE round((110000 * (0.8 + random() * 0.4))::NUMERIC, 2)
      END
    -- Q2 is in progress: ~38% of quarter elapsed (5 of 13 weeks)
    WHEN q.q = 2 THEN
      CASE a.account_type
        WHEN 'expense' THEN round((d.budget / 4 * 0.38 * (0.9 + random() * 0.2))::NUMERIC, 2)
        WHEN 'revenue' THEN round((540000 * 0.38 * (0.85 + random() * 0.3))::NUMERIC, 2)
        ELSE round((110000 * 0.38 * (0.85 + random() * 0.3))::NUMERIC, 2)
      END
  END
FROM hr.departments d
CROSS JOIN (SELECT id, account_type FROM finance.accounts WHERE account_type IN ('expense','revenue') LIMIT 5) a
CROSS JOIN (SELECT q FROM (VALUES (1),(2)) AS quarters(q)) q;

-- ANALYTICS: Page Views (1000 page views)
DO $$
DECLARE
  page_types TEXT[] := ARRAY['home','category','product','product','product','cart','checkout','confirmation','search'];
  devices TEXT[] := ARRAY['desktop','desktop','mobile','mobile','tablet'];
  browsers TEXT[] := ARRAY['Chrome','Chrome','Safari','Firefox','Edge'];
  countries TEXT[] := ARRAY['Greece','Greece','Greece','Germany','UK','France','Italy','Cyprus'];
  cust_ids INT[];
  prod_ids INT[];
  i INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.products) INTO prod_ids;

  FOR i IN 1..1000 LOOP
    INSERT INTO analytics.page_views (session_id, customer_id, page_url, page_type, product_id, device_type, browser, country, duration_seconds, created_at)
    VALUES (
      md5(random()::TEXT || i::TEXT),
      CASE WHEN random() > 0.3 THEN cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT] ELSE NULL END,
      '/page/' || i,
      page_types[1 + floor(random() * array_length(page_types,1))::INT],
      CASE WHEN random() > 0.5 THEN prod_ids[1 + floor(random() * array_length(prod_ids,1))::INT] ELSE NULL END,
      devices[1 + floor(random() * array_length(devices,1))::INT],
      browsers[1 + floor(random() * array_length(browsers,1))::INT],
      countries[1 + floor(random() * array_length(countries,1))::INT],
      (10 + floor(random() * 290))::INT,
      NOW() - (random() * 90)::INT * INTERVAL '1 day' - (random() * 23)::INT * INTERVAL '1 hour'
    );
  END LOOP;
END $$;

-- STOCK MOVEMENTS (sample)
INSERT INTO inventory.stock_movements (product_id, warehouse_id, movement_type, quantity, unit_cost, notes, created_at)
SELECT
  p.id,
  w.id,
  mt.movement_type,
  (mt.base_qty + floor(random() * mt.range_qty))::INT,
  p.cost_price,
  mt.movement_type || ' movement',
  NOW() - (random() * 90)::INT * INTERVAL '1 day'
FROM ecommerce.products p
CROSS JOIN inventory.warehouses w
CROSS JOIN (VALUES
  ('purchase'::TEXT,   50::INT, 100::INT),
  ('sale',             10,       50),
  ('adjustment',        1,       10)
) AS mt(movement_type, base_qty, range_qty)
WHERE p.id % 3 = 0 AND w.id <= 3
LIMIT 150;

-- PURCHASE ORDERS
INSERT INTO inventory.purchase_orders (po_number, supplier_id, warehouse_id, status, total_amount, expected_date, received_date, created_by)
SELECT
  'PO-' || TO_CHAR(NOW() - (random()*180)::INT * INTERVAL '1 day', 'YYYYMMDD') || '-' || LPAD(s.id::TEXT, 4, '0'),
  s.id,
  (ARRAY[1,2,3])[1 + floor(random()*3)::INT],
  (ARRAY['received','received','confirmed','sent','partial'])[1+floor(random()*5)::INT],
  round((5000 + random() * 45000)::NUMERIC, 2),
  CURRENT_DATE + (floor(random() * 30))::INT,
  CASE WHEN random() > 0.4 THEN CURRENT_DATE - (floor(random() * 30))::INT ELSE NULL END,
  (SELECT id FROM hr.employees WHERE department_id = 9 LIMIT 1)
FROM inventory.suppliers s;

-- ============================================================
-- VIEWS (useful for analytics)
-- ============================================================

CREATE OR REPLACE VIEW analytics.vw_sales_summary AS
SELECT
  DATE_TRUNC('month', o.ordered_at) AS month,
  COUNT(DISTINCT o.id)              AS total_orders,
  COUNT(DISTINCT o.customer_id)     AS unique_customers,
  SUM(o.total_amount)               AS gross_revenue,
  SUM(o.discount_amount)            AS total_discounts,
  SUM(o.total_amount - o.discount_amount) AS net_revenue,
  AVG(o.total_amount)               AS avg_order_value,
  COUNT(CASE WHEN o.status = 'cancelled' THEN 1 END) AS cancelled_orders,
  COUNT(CASE WHEN o.status = 'returned'  THEN 1 END) AS returned_orders
FROM ecommerce.orders o
GROUP BY 1
ORDER BY 1 DESC;

CREATE OR REPLACE VIEW analytics.vw_product_performance AS
SELECT
  p.id,
  p.sku,
  p.name,
  c.name  AS category,
  b.name  AS brand,
  p.base_price,
  p.cost_price,
  p.base_price - p.cost_price  AS gross_margin,
  ROUND((p.base_price - p.cost_price) / p.base_price * 100, 2) AS margin_pct,
  p.rating_avg,
  p.rating_count,
  COALESCE(SUM(oi.quantity), 0)       AS total_units_sold,
  COALESCE(SUM(oi.line_total), 0)     AS total_revenue,
  COALESCE(SUM(oi.quantity) * p.cost_price, 0) AS total_cost,
  COALESCE(SUM(oi.line_total) - SUM(oi.quantity) * p.cost_price, 0) AS gross_profit
FROM ecommerce.products p
LEFT JOIN ecommerce.categories c ON p.category_id = c.id
LEFT JOIN ecommerce.brands b ON p.brand_id = b.id
LEFT JOIN ecommerce.order_items oi ON p.id = oi.product_id
LEFT JOIN ecommerce.orders o ON oi.order_id = o.id AND o.status NOT IN ('cancelled','returned','refunded')
GROUP BY p.id, p.sku, p.name, c.name, b.name, p.base_price, p.cost_price, p.rating_avg, p.rating_count
ORDER BY total_revenue DESC;

CREATE OR REPLACE VIEW analytics.vw_customer_ltv AS
SELECT
  c.id,
  c.email,
  c.first_name || ' ' || c.last_name AS full_name,
  c.country,
  c.is_vip,
  c.acquisition_channel,
  c.created_at             AS customer_since,
  COUNT(DISTINCT o.id)     AS total_orders,
  COALESCE(SUM(o.total_amount), 0) AS lifetime_value,
  COALESCE(AVG(o.total_amount), 0) AS avg_order_value,
  MAX(o.ordered_at)        AS last_order_date,
  DATE_PART('day', NOW() - MAX(o.ordered_at)) AS days_since_last_order
FROM ecommerce.customers c
LEFT JOIN ecommerce.orders o ON c.id = o.customer_id AND o.status NOT IN ('cancelled','returned','refunded')
GROUP BY c.id, c.email, c.first_name, c.last_name, c.country, c.is_vip, c.acquisition_channel, c.created_at
ORDER BY lifetime_value DESC;

CREATE OR REPLACE VIEW hr.vw_headcount_summary AS
SELECT
  d.name   AS department,
  d.code,
  COUNT(e.id)                      AS active_headcount,
  d.head_count_max                 AS max_headcount,
  d.head_count_max - COUNT(e.id)   AS open_positions,
  ROUND(AVG(e.salary), 0)          AS avg_salary,
  MIN(e.salary)                    AS min_salary,
  MAX(e.salary)                    AS max_salary,
  SUM(e.salary)                    AS total_payroll,
  ROUND(AVG(e.performance_score),2) AS avg_performance,
  COUNT(CASE WHEN e.remote_work THEN 1 END) AS remote_workers
FROM hr.departments d
LEFT JOIN hr.employees e ON d.id = e.department_id AND e.is_active = TRUE
GROUP BY d.id, d.name, d.code, d.head_count_max
ORDER BY active_headcount DESC;

CREATE OR REPLACE VIEW inventory.vw_stock_alerts AS
SELECT
  p.id        AS product_id,
  p.sku,
  p.name      AS product_name,
  w.name      AS warehouse,
  s.quantity_on_hand,
  s.quantity_reserved,
  s.quantity_available,
  s.reorder_point,
  CASE
    WHEN s.quantity_available = 0 THEN 'OUT_OF_STOCK'
    WHEN s.quantity_available <= s.reorder_point THEN 'LOW_STOCK'
    ELSE 'OK'
  END AS stock_status,
  s.last_restocked_at
FROM inventory.stock s
JOIN ecommerce.products p ON s.product_id = p.id
JOIN inventory.warehouses w ON s.warehouse_id = w.id
ORDER BY s.quantity_available ASC;

CREATE OR REPLACE VIEW support.vw_ticket_metrics AS
SELECT
  DATE_TRUNC('week', t.created_at) AS week,
  t.category,
  t.priority,
  COUNT(*)                          AS total_tickets,
  COUNT(CASE WHEN t.status IN ('resolved','closed') THEN 1 END) AS resolved,
  COUNT(CASE WHEN t.sla_breached THEN 1 END) AS sla_breaches,
  ROUND(AVG(EXTRACT(EPOCH FROM (t.resolved_at - t.created_at))/3600), 2) AS avg_resolution_hours,
  ROUND(AVG(t.satisfaction_score), 2) AS avg_satisfaction
FROM support.tickets t
GROUP BY 1, 2, 3
ORDER BY 1 DESC;

-- ============================================================
-- FINAL: Update product ratings from actual reviews
-- ============================================================
UPDATE ecommerce.products p
SET
  rating_avg   = sub.avg_rating,
  rating_count = sub.cnt
FROM (
  SELECT product_id, ROUND(AVG(rating)::NUMERIC, 2) AS avg_rating, COUNT(*) AS cnt
  FROM ecommerce.reviews
  GROUP BY product_id
) sub
WHERE p.id = sub.product_id;

-- ============================================================
-- ADDITIONAL TABLES
-- ============================================================

-- HR: Employee training and certification records
CREATE TABLE hr.training_records (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    training_name   VARCHAR(100) NOT NULL,
    category        VARCHAR(50) CHECK (category IN ('technical','compliance','leadership','product','sales','security','health_safety')),
    provider        VARCHAR(100),
    started_at      DATE,
    completed_at    DATE,
    score           NUMERIC(5,2) CHECK (score BETWEEN 0 AND 100),
    passed          BOOLEAN,
    certification   VARCHAR(100),
    expires_at      DATE,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Analytics: Granular cart/funnel events for abandonment analysis
CREATE TABLE analytics.cart_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(50) NOT NULL,
    customer_id     INT REFERENCES ecommerce.customers(id),
    product_id      INT REFERENCES ecommerce.products(id),
    event_type      VARCHAR(30) CHECK (event_type IN ('add_to_cart','remove_from_cart','checkout_started','checkout_completed','abandoned')),
    quantity        INT DEFAULT 1,
    unit_price      NUMERIC(12,2),
    device_type     VARCHAR(20) CHECK (device_type IN ('desktop','mobile','tablet')),
    traffic_source  VARCHAR(30) CHECK (traffic_source IN ('organic','paid_search','social','email','direct','referral')),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Ecommerce: Customer wishlists
CREATE TABLE ecommerce.wishlists (
    id              SERIAL PRIMARY KEY,
    customer_id     INT REFERENCES ecommerce.customers(id),
    product_id      INT REFERENCES ecommerce.products(id),
    added_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE(customer_id, product_id)
);

-- Finance: Employee expense submissions
CREATE TABLE finance.expense_reports (
    id              SERIAL PRIMARY KEY,
    employee_id     INT REFERENCES hr.employees(id),
    department_id   INT REFERENCES hr.departments(id),
    report_date     DATE NOT NULL,
    description     TEXT,
    category        VARCHAR(50) CHECK (category IN ('travel','meals','equipment','software','training','office','other')),
    amount          NUMERIC(12,2) NOT NULL,
    currency        VARCHAR(3) DEFAULT 'EUR',
    status          VARCHAR(20) CHECK (status IN ('draft','submitted','approved','rejected','paid')),
    approved_by     INT REFERENCES hr.employees(id),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_training_employee ON hr.training_records(employee_id);
CREATE INDEX idx_training_expires  ON hr.training_records(expires_at);
CREATE INDEX idx_cart_events_session ON analytics.cart_events(session_id);
CREATE INDEX idx_cart_events_product ON analytics.cart_events(product_id, event_type);
CREATE INDEX idx_cart_events_created ON analytics.cart_events(created_at);
CREATE INDEX idx_wishlists_customer ON ecommerce.wishlists(customer_id);
CREATE INDEX idx_wishlists_product  ON ecommerce.wishlists(product_id);
CREATE INDEX idx_expense_employee   ON finance.expense_reports(employee_id);
CREATE INDEX idx_expense_status     ON finance.expense_reports(status);

-- ============================================================
-- ADDITIONAL DATA
-- ============================================================

-- HR: Training records (~60 explicit rows covering all scenarios)
INSERT INTO hr.training_records (employee_id, training_name, category, provider, started_at, completed_at, score, passed, certification, expires_at) VALUES
(1,  'Advanced Leadership Program',           'leadership',    'Harvard Online',        '2023-01-10','2023-03-15', 94.0, TRUE,  'ALP-2023',       '2026-03-15'),
(2,  'CFO Strategy & Finance Masterclass',    'compliance',    'ICFM',                  '2023-02-01','2023-04-30', 91.5, TRUE,  'ICFM-CFO',       '2026-04-30'),
(3,  'AWS Solutions Architect Professional',  'technical',     'AWS Training',           '2023-01-20','2023-03-10', 88.0, TRUE,  'AWS-SAP-C02',    '2026-03-10'),
(3,  'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(5,  'Product Management Certification',      'product',       'Product School',         '2023-03-01','2023-05-31', 93.0, TRUE,  'CPM-2023',       '2026-05-31'),
(6,  'Kubernetes Administrator (CKA)',        'technical',     'CNCF',                   '2023-04-01','2023-05-20', 82.0, TRUE,  'CKA-2023',       '2026-05-20'),
(6,  'CKAD App Developer (expired)',          'technical',     'CNCF',                   '2022-05-01','2022-06-30', 79.0, TRUE,  'CKAD-2022',      '2025-06-30'),
(6,  'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(7,  'Docker & Container Security',           'security',      'Docker Inc',             '2023-06-01','2023-07-15', 79.5, TRUE,  NULL,              NULL),
(7,  'Google Cloud Professional',             'technical',     'Google Cloud',           '2024-11-01', NULL,         NULL,  NULL,  NULL,              NULL),
(8,  'Backend Engineering Best Practices',    'technical',     'O''Reilly Online',       '2023-08-01','2023-09-10', 85.0, TRUE,  NULL,              NULL),
(9,  'PostgreSQL Advanced Administration',    'technical',     'EnterpriseDB',           '2023-09-01','2023-10-15', 77.0, TRUE,  'PGCA',           '2026-10-15'),
(9,  'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(10, 'React & TypeScript Deep Dive',          'technical',     'Frontend Masters',       '2023-10-01','2023-11-30', 90.0, TRUE,  NULL,              NULL),
(11, 'Clean Code & Refactoring',              'technical',     'Pluralsight',            '2023-11-01','2024-01-15', 72.0, TRUE,  NULL,              NULL),
(11, 'AWS Cloud Practitioner (failed)',       'technical',     'AWS Training',           '2023-02-01','2023-03-15', 58.0, FALSE, NULL,              NULL),
(12, 'GDPR Developer Compliance',             'compliance',    'IAPP',                   '2023-05-01','2023-06-10', 88.5, TRUE,  'CDPP-E',         '2026-06-10'),
(13, 'Agile Product Owner (CSPO)',            'product',       'Scrum Alliance',         '2023-06-01','2023-07-20', 95.0, TRUE,  'CSPO',           '2025-07-20'),
(13, 'CSPO Renewal (in progress)',            'product',       'Scrum Alliance',         '2025-06-01', NULL,          NULL, NULL,  NULL,              NULL),
(13, 'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(14, 'UX Research Methods',                   'product',       'Nielsen Norman',         '2023-09-01','2023-11-15', 87.5, TRUE,  'UX-Cert',        '2026-11-15'),
(15, 'Enterprise Sales Methodology',          'sales',         'Miller Heiman',          '2023-07-01','2023-08-31', 88.0, TRUE,  'ESM',            '2026-08-31'),
(16, 'CRM Mastery — Salesforce',              'sales',         'Salesforce Trailhead',   '2023-10-01','2023-11-30', 94.0, TRUE,  'SFDC-Certified', '2026-11-30'),
(17, 'Consultative Selling Skills',           'sales',         'Challenger Inc',         '2024-01-01','2024-02-28', 76.0, TRUE,  NULL,              NULL),
(18, 'GDPR & Data Privacy Awareness',         'compliance',    'IAPP',                   '2023-05-15','2023-06-30', 81.0, TRUE,  NULL,              NULL),
(19, 'Salesforce Administrator (failed)',     'technical',     'Salesforce Trailhead',   '2023-09-01','2023-10-31', 64.0, FALSE, NULL,              NULL),
(20, 'CMO Executive Program',                 'leadership',    'Kellogg Online',         '2023-09-01','2023-12-15', 90.0, TRUE,  NULL,              NULL),
(21, 'Google Ads Certification',              'technical',     'Google Skillshop',       '2023-06-01','2023-06-15', 96.0, TRUE,  'Google-Ads',     '2025-06-15'),
(21, 'HubSpot Email Marketing (expired)',     'sales',         'HubSpot Academy',        '2022-03-01','2022-04-15', 91.0, TRUE,  'HubSpot-EM',     '2024-04-15'),
(22, 'Content Marketing Strategy',            'sales',         'HubSpot Academy',        '2023-08-01','2023-09-30', 83.0, TRUE,  'HubSpot-CM',     '2025-09-30'),
(22, 'Google Analytics IQ (expired)',         'technical',     'Google',                 '2022-06-01','2022-06-20', 88.0, TRUE,  'GA-IQ',          '2024-06-20'),
(23, 'Technical SEO Masterclass',             'technical',     'SEMrush Academy',        '2023-11-01','2023-12-15', 87.0, TRUE,  'SEMrush-SEO',    '2025-12-15'),
(24, 'Strategic HR Leadership',               'leadership',    'SHRM',                   '2023-05-01','2023-07-31', 89.5, TRUE,  'SHRM-SCP',       '2026-07-31'),
(24, 'GDPR Data Controller Certification',    'compliance',    'IAPP',                   '2023-04-01','2023-06-30', 92.5, TRUE,  'CDPSE',          '2026-06-30'),
(26, 'IFRS Accounting Standards',             'compliance',    'ACCA',                   '2023-03-01','2023-06-30', 84.0, TRUE,  'ACCA-IFRS',      '2026-06-30'),
(26, 'AML Anti-Money Laundering',             'compliance',    'ACAMS',                  '2023-08-01','2023-09-30', 85.0, TRUE,  'CAMS',           '2026-09-30'),
(27, 'Financial Modelling Excel',             'technical',     'Corporate Finance Inst', '2023-09-01','2023-10-31', 88.5, TRUE,  'CFI-FM',          NULL),
(28, 'Customer Service Excellence',           'leadership',    'HDI',                    '2023-06-01','2023-08-31', 91.0, TRUE,  'HDI-DST',        '2026-08-31'),
(29, 'ITIL 4 Foundation',                     'technical',     'AXELOS',                 '2023-07-01','2023-08-15', 83.0, TRUE,  'ITIL-4',         '2026-08-15'),
(29, 'CompTIA Security+ (expired)',           'security',      'CompTIA',                '2022-01-10','2022-03-31', 82.0, TRUE,  'CompTIA-Sec+',   '2025-03-31'),
(30, 'Conflict Resolution Training',          'compliance',    'Internal L&D',           '2023-10-01','2023-11-15', 76.0, TRUE,  NULL,              NULL),
(33, 'Supply Chain Management (CPIM)',        'product',       'APICS',                  '2023-04-01','2023-06-30', 85.5, TRUE,  'CPIM-Part1',     '2026-06-30'),
(34, 'Warehouse Safety & Operations',         'health_safety', 'NEBOSH',                 '2023-08-01','2023-09-30', 79.0, TRUE,  'NEBOSH-Gen',     '2026-09-30'),
(36, 'Data Leadership & Strategy',            'leadership',    'MIT Sloan Online',       '2023-10-01','2024-01-31', 93.0, TRUE,  NULL,              NULL),
(36, 'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(37, 'Apache Spark & Databricks',             'technical',     'Databricks',             '2023-05-01','2023-07-15', 87.0, TRUE,  'Databricks-DE',  '2026-07-15'),
(38, 'Python Data Analysis (pandas/numpy)',   'technical',     'Coursera',               '2023-08-01','2023-10-31', 92.0, TRUE,  NULL,              NULL),
(38, 'Annual Cybersecurity Awareness',        'security',      'Internal IT',            '2024-01-15','2024-01-15',100.0, TRUE,  NULL,             '2025-01-15'),
(39, 'Machine Learning Engineering',          'technical',     'DeepLearning.AI',        '2023-06-01','2023-10-31', 95.0, TRUE,  'MLE-Cert',        NULL),
(40, 'AWS DevOps Professional',               'technical',     'AWS Training',           '2023-07-01','2023-09-30', 86.0, TRUE,  'AWS-DOP-C02',    '2026-09-30'),
(41, 'Python for Data Engineering',           'technical',     'Coursera',               '2023-11-01','2024-01-31', 91.0, TRUE,  NULL,              NULL),
(42, 'Design Systems & Figma',                'product',       'Figma Academy',          '2024-01-15','2024-03-10', 89.0, TRUE,  NULL,              NULL),
(43, 'Sales Negotiation Workshop',            'sales',         'Internal L&D',           '2024-02-01','2024-03-15', 78.0, TRUE,  NULL,              NULL),
(44, 'Meta Ads Advanced',                     'technical',     'Meta Blueprint',         '2024-01-15','2024-02-28', 91.0, TRUE,  'Meta-Ads-Pro',   '2025-02-28'),
(46, 'ISTQB Software Testing Foundation',     'technical',     'ISTQB',                  '2024-01-01','2024-03-15', 84.0, TRUE,  'ISTQB-CTFL',     '2027-03-15'),
(47, 'Junior Accountant Essentials',          'compliance',    'AAT Online',             '2024-01-01','2024-03-31', 80.0, TRUE,  NULL,              NULL),
(4,  'Sales Leadership Excellence',           'sales',         'Sandler Training',       '2023-04-01','2023-06-30', 92.0, TRUE,  'SLE-2023',       '2026-06-30');

-- Analytics: Cart events (500 records)
DO $$
DECLARE
  event_types TEXT[] := ARRAY['add_to_cart','add_to_cart','add_to_cart','remove_from_cart',
                               'checkout_started','checkout_completed','abandoned','abandoned'];
  devices     TEXT[] := ARRAY['desktop','desktop','mobile','mobile','tablet'];
  sources     TEXT[] := ARRAY['organic','organic','paid_search','social','email','direct','referral'];
  cust_ids    INT[];
  prod_ids    INT[];
  i           INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.products WHERE is_active = TRUE) INTO prod_ids;

  FOR i IN 1..500 LOOP
    INSERT INTO analytics.cart_events
        (session_id, customer_id, product_id, event_type, quantity, unit_price, device_type, traffic_source, created_at)
    VALUES (
      md5(random()::TEXT || i::TEXT || 'cart'),
      CASE WHEN random() > 0.25 THEN cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT] ELSE NULL END,
      prod_ids[1 + floor(random() * array_length(prod_ids,1))::INT],
      event_types[1 + floor(random() * array_length(event_types,1))::INT],
      (1 + floor(random() * 3))::INT,
      round((20 + random() * 500)::NUMERIC, 2),
      devices[1 + floor(random() * array_length(devices,1))::INT],
      sources[1 + floor(random() * array_length(sources,1))::INT],
      NOW() - (random() * 90)::INT * INTERVAL '1 day' - (random() * 23)::INT * INTERVAL '1 hour'
    );
  END LOOP;
END $$;

-- Ecommerce: Wishlists (~200 records)
DO $$
DECLARE
  cust_ids INT[];
  prod_ids INT[];
  i        INT;
BEGIN
  SELECT ARRAY(SELECT id FROM ecommerce.customers) INTO cust_ids;
  SELECT ARRAY(SELECT id FROM ecommerce.products)  INTO prod_ids;

  FOR i IN 1..200 LOOP
    BEGIN
      INSERT INTO ecommerce.wishlists (customer_id, product_id, added_at)
      VALUES (
        cust_ids[1 + floor(random() * array_length(cust_ids,1))::INT],
        prod_ids[1 + floor(random() * array_length(prod_ids,1))::INT],
        NOW() - (random() * 180)::INT * INTERVAL '1 day'
      );
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
  END LOOP;
END $$;

-- Finance: Expense reports (100 records)
DO $$
DECLARE
  categories TEXT[] := ARRAY['travel','travel','meals','equipment','software','training','office','other'];
  statuses   TEXT[] := ARRAY['submitted','submitted','approved','approved','paid','paid','rejected','draft'];
  emp_ids    INT[];
  mgr_ids    INT[];
  i          INT;
  emp        INT;
  dept       INT;
  cat        TEXT;
  stat       TEXT;
  amt        NUMERIC;
  rdate      DATE;
  appr       INT;
BEGIN
  SELECT ARRAY(SELECT id FROM hr.employees WHERE is_active = TRUE) INTO emp_ids;
  SELECT ARRAY(SELECT DISTINCT manager_id FROM hr.employees WHERE manager_id IS NOT NULL) INTO mgr_ids;

  FOR i IN 1..100 LOOP
    emp   := emp_ids[1 + floor(random() * array_length(emp_ids,1))::INT];
    cat   := categories[1 + floor(random() * array_length(categories,1))::INT];
    stat  := statuses[1 + floor(random() * array_length(statuses,1))::INT];
    rdate := CURRENT_DATE - (random() * 365)::INT;
    amt   := CASE cat
               WHEN 'travel'    THEN round((200  + random() * 1800)::NUMERIC, 2)
               WHEN 'equipment' THEN round((100  + random() * 2400)::NUMERIC, 2)
               WHEN 'software'  THEN round((50   + random() * 950)::NUMERIC,  2)
               WHEN 'training'  THEN round((200  + random() * 2800)::NUMERIC, 2)
               ELSE                  round((20   + random() * 280)::NUMERIC,  2)
             END;
    appr  := CASE WHEN stat IN ('approved','paid')
               THEN mgr_ids[1 + floor(random() * array_length(mgr_ids,1))::INT]
               ELSE NULL END;

    SELECT department_id INTO dept FROM hr.employees WHERE id = emp;

    INSERT INTO finance.expense_reports
        (employee_id, department_id, report_date, description, category, amount, currency, status, approved_by)
    VALUES (
      emp, dept, rdate,
      cat || ' expense — ' || TO_CHAR(rdate, 'Month YYYY'),
      cat, amt, 'EUR', stat, appr
    );
  END LOOP;
END $$;

-- Analytics: Cart funnel view
CREATE OR REPLACE VIEW analytics.vw_cart_funnel AS
SELECT
  DATE_TRUNC('week', ce.created_at)          AS week,
  p.name                                      AS product,
  cat.name                                    AS category,
  ce.device_type,
  ce.traffic_source,
  COUNT(CASE WHEN ce.event_type = 'add_to_cart'         THEN 1 END) AS add_to_cart,
  COUNT(CASE WHEN ce.event_type = 'remove_from_cart'    THEN 1 END) AS removed,
  COUNT(CASE WHEN ce.event_type = 'checkout_started'    THEN 1 END) AS checkout_started,
  COUNT(CASE WHEN ce.event_type = 'checkout_completed'  THEN 1 END) AS checkout_completed,
  COUNT(CASE WHEN ce.event_type = 'abandoned'           THEN 1 END) AS abandoned,
  ROUND(
    100.0 * COUNT(CASE WHEN ce.event_type = 'checkout_completed' THEN 1 END) /
    NULLIF(COUNT(CASE WHEN ce.event_type = 'add_to_cart' THEN 1 END), 0), 2
  ) AS cart_to_purchase_pct
FROM analytics.cart_events ce
JOIN ecommerce.products    p   ON ce.product_id   = p.id
JOIN ecommerce.categories  cat ON p.category_id   = cat.id
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1 DESC;

-- HR: Training completion view (full-access only — hr schema blocked for analyst)
CREATE OR REPLACE VIEW hr.vw_training_completion AS
SELECT
  d.name                                                                AS department,
  tr.category                                                           AS training_category,
  COUNT(DISTINCT e.id)                                                  AS enrolled_employees,
  COUNT(tr.id)                                                          AS total_trainings,
  COUNT(tr.id) FILTER (WHERE tr.passed = TRUE)                         AS passed,
  COUNT(tr.id) FILTER (WHERE tr.passed = FALSE)                        AS failed,
  COUNT(tr.id) FILTER (WHERE tr.completed_at IS NULL)                  AS in_progress,
  ROUND(AVG(tr.score) FILTER (WHERE tr.score IS NOT NULL), 1)          AS avg_score,
  COUNT(tr.id) FILTER (WHERE tr.expires_at < CURRENT_DATE AND tr.passed = TRUE) AS expired_certs
FROM hr.departments d
JOIN hr.employees e      ON d.id = e.department_id AND e.is_active = TRUE
LEFT JOIN hr.training_records tr ON e.id = tr.employee_id
GROUP BY d.id, d.name, tr.category
ORDER BY d.name, tr.category;

-- Final summary
DO $$
BEGIN
  RAISE NOTICE '====================================';
  RAISE NOTICE 'Savvina AI Test Database Ready!';
  RAISE NOTICE '====================================';
  RAISE NOTICE 'Schemas: ecommerce, hr, finance, inventory, support, analytics';
  RAISE NOTICE 'Tables: 35+ tables with complex relationships';
  RAISE NOTICE 'Sample data loaded successfully';
  RAISE NOTICE '====================================';
END $$;
