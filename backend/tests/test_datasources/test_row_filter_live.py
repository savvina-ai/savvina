# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Rewritten statements run on the real engines and return only filtered rows.

Opt-in: ``SAVVINA_LIVE_TEST_DBS=1`` with the ``test-dbs`` compose profile up
(``docker compose --profile test-dbs up -d`` from the repo root: ``sample-postgres`` on
5435, ``sample-mysql`` on 3307). Skipped otherwise.
"""

# ruff: noqa: S608 — test queries against the sample databases.

from __future__ import annotations

import os

import pytest

from app.datasources.registry import create_datasource
from app.services.row_filter_rewrite import apply_row_filters

pytestmark = pytest.mark.skipif(
    os.environ.get("SAVVINA_LIVE_TEST_DBS") != "1",
    reason="set SAVVINA_LIVE_TEST_DBS=1 with the test-dbs compose profile running",
)

_PG = {
    "host": "localhost",
    "port": 5435,
    "database": "savvina_test",
    "username": "savvina",
    "password": os.environ.get("SAMPLE_POSTGRES_PASSWORD", "savvina_demo"),
    "ssl_mode": "disable",
}
_MYSQL = {
    "host": "localhost",
    "port": 3307,
    "database": "sample_delivery",
    "username": "savvina",
    "password": os.environ.get("SAMPLE_MYSQL_PASSWORD", "savvina_demo"),
}

# (source_type, config, filtered table key, tenant column, the three statements)
_CASES = {
    "PostgreSQL": (
        "postgresql",
        _PG,
        "ecommerce.orders",
        "customer_id",
        [
            "SELECT o.id, o.customer_id, i.quantity FROM ecommerce.orders o"
            " JOIN ecommerce.order_items i ON i.order_id = o.id",
            "SELECT COUNT(*) AS n FROM ecommerce.orders",
            "WITH o AS (SELECT * FROM ecommerce.orders) SELECT customer_id FROM o",
        ],
        [
            "SELECT id::text, total_amount::numeric(12,2) FROM ecommerce.orders",
            "SELECT DISTINCT ON (customer_id) customer_id, id FROM ecommerce.orders"
            " ORDER BY customer_id, ordered_at DESC",
            "SELECT COUNT(*) FILTER (WHERE status = 'delivered') AS n FROM ecommerce.orders",
            "SELECT ordered_at AT TIME ZONE 'UTC' FROM ecommerce.orders",
            "SELECT id FROM ecommerce.orders WHERE order_number ILIKE 'ord%'",
            "SELECT id FROM ecommerce.orders WHERE status = ANY(ARRAY['delivered', 'pending'])",
            "SELECT id FROM ecommerce.orders WHERE ordered_at > NOW() - INTERVAL '1000 days'",
            "SELECT id, SUM(total_amount) OVER (PARTITION BY customer_id ORDER BY ordered_at"
            " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM ecommerce.orders",
            "SELECT date_trunc('month', ordered_at) AS m, COUNT(*) FROM ecommerce.orders"
            " GROUP BY 1 ORDER BY 1",
            "SELECT EXTRACT(YEAR FROM ordered_at) FROM ecommerce.orders",
            "SELECT COALESCE(NULLIF(status, ''), 'none') FROM ecommerce.orders",
            "SELECT CASE WHEN total_amount > 100 THEN 'big' ELSE 'small' END FROM ecommerce.orders",
            "SELECT o.id FROM ecommerce.orders o LEFT JOIN ecommerce.order_items i"
            " ON i.order_id = o.id WHERE i.id IS NULL",
        ],
    ),
    "MySQL": (
        "mysql",
        _MYSQL,
        "sample_delivery.orders",
        "restaurant_id",
        [
            "SELECT o.id, o.restaurant_id, i.quantity FROM orders o"
            " JOIN order_items i ON i.order_id = o.id",
            "SELECT COUNT(*) AS n FROM orders",
            "WITH o AS (SELECT * FROM orders) SELECT restaurant_id FROM o",
        ],
        [
            "SELECT GROUP_CONCAT(status ORDER BY status SEPARATOR ', ') FROM orders",
            "SELECT DATE_FORMAT(order_date, '%Y-%m') AS m, COUNT(*) FROM orders GROUP BY m",
            "SELECT id FROM orders WHERE order_date > STR_TO_DATE('01/01/2000', '%d/%m/%Y')",
            "SELECT IF(status = 'delivered', 1, 0) AS done, IFNULL(driver_id, 0) FROM orders",
            "SELECT `id`, `status` FROM `orders` WHERE `status` = 'placed'",
            "SELECT id FROM orders ORDER BY id LIMIT 2, 3",
            "SELECT id FROM orders WHERE order_date >= DATE_SUB(NOW(), INTERVAL 10000 DAY)",
            "SELECT o.id FROM orders o JOIN restaurants r ON r.id = o.restaurant_id"
            " WHERE r.status = 'active'",
            "SELECT restaurant_id, COUNT(*) AS n FROM orders GROUP BY restaurant_id HAVING n > 0",
        ],
    ),
}


async def _run(adapter, sql: str):
    return await adapter.execute_query(sql, timeout=30, max_rows=10_000)


@pytest.mark.parametrize("dialect", list(_CASES))
async def test_rewritten_statements_return_only_filtered_rows(dialect: str) -> None:
    source_type, config, key, column, statements, corpus = _CASES[dialect]
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        schema, table = key.split(".")
        probe = await _run(adapter, f"SELECT {column} FROM {schema}.{table} ORDER BY id LIMIT 1")
        tenant = probe.rows[0][0]
        filters = {key: f"{column} = {tenant}"}
        expected_count = (
            await _run(adapter, f"SELECT COUNT(*) FROM {schema}.{table} WHERE {column} = {tenant}")
        ).rows[0][0]

        join_sql, count_sql, cte_sql = statements
        join = await _run(adapter, apply_row_filters(join_sql, filters, dialect))
        assert join.row_count > 0
        tenant_col = join.columns.index(column)
        assert all(row[tenant_col] == tenant for row in join.rows)

        count = await _run(adapter, apply_row_filters(count_sql, filters, dialect))
        assert count.rows[0][0] == expected_count

        cte = await _run(adapter, apply_row_filters(cte_sql, filters, dialect))
        assert cte.row_count == expected_count
        assert all(row[0] == tenant for row in cte.rows)

        for sql in corpus:
            rewritten = apply_row_filters(sql, filters, dialect)
            await _run(adapter, rewritten)  # executes without an engine error
    finally:
        await adapter.disconnect()
