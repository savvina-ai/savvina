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
import warnings

import pytest

from app.datasources.adapters.postgresql import close_all_pools
from app.datasources.errors import ExplainError
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


@pytest.fixture(autouse=True)
async def _drop_cached_pools():
    """The PostgreSQL adapter keeps its pool in a module cache that outlives ``disconnect``;
    each test runs on its own event loop, so close the pool before that loop goes away."""
    yield
    await close_all_pools()


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


# Rows the side-channel filters let through (both end in ``<= 10`` on an id starting at 1).
_VISIBLE = 10

# (source_type, config, filtered table key, a filter the index cannot serve, the column the
# probe compares, the id of a row the filter hides, the probe predicates that raise on a hit)
_SIDE_CHANNEL = {
    "PostgreSQL": (
        "postgresql",
        _PG,
        "ecommerce.customers",
        f"id IN (SELECT id FROM ecommerce.customers WHERE id <= {_VISIBLE})",
        "email",
        50,
        ["1/(CASE WHEN email = '{value}' THEN 0 ELSE 1 END) = 1"],
    ),
    "MySQL": (
        "mysql",
        _MYSQL,
        "sample_delivery.customers",
        f"id + 0 <= {_VISIBLE}",
        "email",
        50,
        [
            "REGEXP_LIKE('a', IF(email = '{value}', '(', 'a'))",
            "JSON_EXTRACT('{{}}', IF(email = '{value}', '$[', '$')) IS NOT NULL",
        ],
    ),
}


@pytest.mark.parametrize("dialect", list(_SIDE_CHANNEL))
async def test_the_fence_keeps_the_models_predicates_off_hidden_rows(dialect: str) -> None:
    """Without the fence these probes raise a data-dependent error on the hidden row (one
    bit per query, in text the correction prompt sees); with it they return."""
    source_type, config, key, condition, column, hidden_id, probes = _SIDE_CHANNEL[dialect]
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        schema, table = key.split(".")
        hidden = (
            await _run(adapter, f"SELECT {column} FROM {schema}.{table} WHERE id = {hidden_id}")
        ).rows[0][0]
        fence = {"PostgreSQL": " OFFSET 0", "MySQL": " LIMIT 18446744073709551615"}[dialect]
        for probe in probes:
            # Seeded emails hold no ' or {, so the interpolation and the {{}} escape are safe.
            sql = f"SELECT count(*) FROM {schema}.{table} WHERE {probe.format(value=hidden)}"
            fenced = apply_row_filters(sql, {key: condition}, dialect)
            assert fence in fenced
            unfenced = fenced.replace(fence, "")
            # Arm the probe: with the fence stripped the engine must raise on the hidden row.
            with pytest.raises(Exception):  # noqa: B017 - any engine error proves the leak
                await _run(adapter, unfenced)
            out = await _run(adapter, fenced)
            assert out.rows[0][0] == _VISIBLE
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("dialect", list(_CASES))
async def test_explain_refuses_a_column_the_table_does_not_have(dialect: str) -> None:
    source_type, config, key, _column, _statements, _corpus = _CASES[dialect]
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        schema, table = key.split(".")
        quote = {"PostgreSQL": '"', "MySQL": "`"}[dialect]
        q = f"{quote}{schema}{quote}.{quote}{table}{quote}"
        good = f"SELECT * FROM (SELECT * FROM {q} WHERE (id > 0)) AS _t"
        await adapter.explain(good, timeout=10)
        bad = f"SELECT * FROM (SELECT * FROM {q} WHERE (nosuch_col > 0)) AS _t"
        with pytest.raises(ExplainError, match="nosuch_col"):
            await adapter.explain(bad, timeout=10)
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("dialect", list(_CASES))
async def test_the_router_check_accepts_a_real_column_and_refuses_a_missing_one(
    dialect: str,
) -> None:
    """``_verify_row_filters`` end to end: connect, rewrite, plan, classify, disconnect."""
    from fastapi import HTTPException

    from app.routers.connections import _verify_row_filters

    source_type, config, key, column, _statements, _corpus = _CASES[dialect]
    await _verify_row_filters(source_type, config, {key: f"{column} > 0"})
    with pytest.raises(HTTPException) as info:
        await _verify_row_filters(source_type, config, {key: "nosuch_col > 0"})
    assert info.value.status_code == 422
    assert info.value.detail.startswith(f'row_filters["{key}"]: rejected by the database:')
    assert "nosuch_col" in info.value.detail  # an unknown-name error is shown unredacted


async def test_postgresql_explain_refuses_a_second_statement() -> None:
    """Task 2 plans through ``conn.fetch`` (extended protocol), which PostgreSQL parses as
    exactly one statement; the mocked tests only show which method is called."""
    source_type, config, _key, _column, _statements, _corpus = _CASES["PostgreSQL"]
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        with pytest.raises(ExplainError, match="multiple commands"):
            await adapter.explain("SELECT 1; SELECT 2", timeout=10)
    finally:
        await adapter.disconnect()


async def test_mysql_explain_emits_no_warning_with_the_condition() -> None:
    """A successful MySQL EXPLAIN raises Note 1003, which quotes the rewritten statement with
    the condition's literals; aiomysql passes each warning to ``warnings.warn``, so it would
    reach the logs. ``explain`` plans with ``sql_notes`` off, so nothing is emitted."""
    from app.routers.connections import _check_statement

    source_type, config, key, column, _statements, _corpus = _CASES["MySQL"]
    statement = _check_statement(key, f"{column} > 12345", "mysql")
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            await adapter.explain(statement, timeout=10)
        assert not [w for w in recorded if "12345" in str(w.message)]
    finally:
        await adapter.disconnect()
