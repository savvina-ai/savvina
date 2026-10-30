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

from app.datasources.adapters.postgresql import close_all_pools, evict_pool
from app.datasources.errors import ExplainError
from app.datasources.models import PrivacySettings, PrivilegeGrant, RoleMembership
from app.datasources.registry import create_datasource
from app.services.privilege_audit import Finding, evaluate, has_findings
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


_PG_ANALYST = {**_PG, "username": "savvina_analyst", "password": "analyst_readonly_2024"}
_MYSQL_ANALYST = {**_MYSQL, "username": "savvina_analyst", "password": "analyst_readonly_2024"}
_MYSQL_ROOT = {
    **_MYSQL,
    "username": "root",
    "password": os.environ.get("SAMPLE_MYSQL_ROOT_PASSWORD", "savvina_demo_root"),
}
_PROBE_PASSWORD = "audit_probe_2026"  # a throwaway role of the sample database


async def _audit(config: dict, source_type: str, tables):
    adapter = create_datasource(source_type)
    result = await adapter.connect(config)
    assert result is None or result.success, getattr(result, "message", "")
    try:
        return await adapter.audit_privileges(tables, timeout=10)
    finally:
        await adapter.disconnect()


async def _pg_admin(statements: list[str]) -> None:
    """Run *statements* as the sample superuser, each on its own, outside a transaction."""
    import asyncpg

    conn = await asyncpg.connect(
        host=_PG["host"],
        port=_PG["port"],
        database=_PG["database"],
        user=_PG["username"],
        password=_PG["password"],
    )
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def test_postgresql_sample_role_is_a_superuser_and_rls_is_not_effective() -> None:
    facts = await _audit(_PG, "postgresql", [("ecommerce", "orders"), (None, "customers")])
    assert facts.role == "savvina"
    assert facts.is_superuser is True
    assert facts.bypasses_rls is True  # the bootstrap superuser carries BYPASSRLS as well
    keys = {(t.schema_name, t.table_name) for t in facts.tables if t.exists}
    assert ("ecommerce", "orders") in keys and ("ecommerce", "customers") in keys
    audit = evaluate(
        facts,
        PrivacySettings(
            row_filters={"ecommerce.orders": "customer_id = 1"}, excluded_tables=["customers"]
        ),
        "postgresql",
    )
    # A superuser skips tier 2 (READS_EXCLUDED_TABLES would only repeat it); tier 1 stays.
    assert [f.code for f in audit.findings] == [Finding.SUPERUSER, Finding.BYPASSRLS]
    assert audit.rls_effective == {"ecommerce.orders": False}
    assert audit.rls_in_use is False
    # The superuser remediation is the dedicated read-only role script over every schema.
    script = audit.findings[0].remediation
    assert script.startswith("-- Create a read-only role")
    assert "ecommerce" in facts.schemas
    lines = script.splitlines()
    for schema in facts.schemas:
        assert f'GRANT USAGE ON SCHEMA "{schema}" TO savvina_reader;' in lines
    # The excluded bare "customers" is revoked from the new role after the schema grants.
    assert 'REVOKE SELECT ON "ecommerce"."customers" FROM savvina_reader;' in lines


async def test_postgresql_analyst_reports_what_the_grants_say() -> None:
    facts = await _audit(_PG_ANALYST, "postgresql", [("ecommerce", "orders"), ("hr", "employees")])
    assert facts.is_superuser is False and facts.bypasses_rls is False
    by_key = {t.key: t for t in facts.tables}
    assert by_key["ecommerce.orders"].can_select is True
    assert by_key["ecommerce.orders"].can_write is False
    assert by_key["ecommerce.orders"].is_owner is False
    assert by_key["hr.employees"].exists is True
    # The analyst holds neither USAGE on hr nor SELECT on the table: this proves only that a
    # table without any grant reads False (the USAGE rule has its own probe scenario).
    assert by_key["hr.employees"].can_select is False
    # pg_catalog's own query_to_xml and friends are never listed; no extension is installed.
    assert facts.sql_from_string_functions == []
    # 02_readonly_user.sql grants SELECT only, and no predefined role.
    assert facts.writes == [] and facts.writes_truncated is False
    assert facts.predefined_roles == []


def _pg_teardown(
    tables: list[str] = (), schemas: list[str] = (), roles: list[str] = ()
) -> list[str]:
    """Statements that remove a probe scenario whatever state it reached: each step is a
    no-op when its object is absent. DROP OWNED BY raises for a role that does not exist and
    a role that still holds a privilege cannot be dropped, so it runs guarded, per role."""
    statements = [f"DROP TABLE IF EXISTS {table}" for table in tables]
    statements += [f"DROP SCHEMA IF EXISTS {schema} CASCADE" for schema in schemas]
    for role in roles:
        statements.append(
            "DO $$ BEGIN"
            f" IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')"
            f" THEN EXECUTE 'DROP OWNED BY {role}'; END IF; END $$"
        )
    statements += [f"DROP ROLE IF EXISTS {role}" for role in roles]
    return statements


async def _pg_count(config: dict, sql: str) -> int:
    """What the engine returns to *config*'s role, on a connection of its own (no pool)."""
    import asyncpg

    conn = await asyncpg.connect(
        host=config["host"],
        port=config["port"],
        database=config["database"],
        user=config["username"],
        password=config["password"],
    )
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


_PG_PROBE_TEARDOWN = _pg_teardown(
    tables=["ecommerce.audit_probe", "ecommerce.audit_probe_group"],
    roles=["savvina_audit_probe", "savvina_audit_group"],
)


async def test_postgresql_select_policy_to_public_applies_and_noinherit_membership_does_not() -> (
    None
):
    """A SELECT policy TO PUBLIC on a table the analyst reads: rls_effective. The same policy
    TO a group the probe role holds NOINHERIT: pg_has_role(..., 'USAGE') says no, so the
    engine filters nothing for it and the audit must not claim otherwise."""
    setup = [
        "CREATE TABLE ecommerce.audit_probe (id int, tenant_id int)",
        "ALTER TABLE ecommerce.audit_probe ENABLE ROW LEVEL SECURITY",
        "CREATE POLICY everyone ON ecommerce.audit_probe FOR SELECT TO PUBLIC"
        " USING (tenant_id = 1)",
        "GRANT SELECT ON ecommerce.audit_probe TO savvina_analyst",
        "CREATE ROLE savvina_audit_group",
        f"CREATE ROLE savvina_audit_probe LOGIN NOINHERIT PASSWORD '{_PROBE_PASSWORD}'",
        # No INHERIT option: the grant takes the member's NOINHERIT as its default (PG 16).
        "GRANT savvina_audit_group TO savvina_audit_probe",
        "GRANT USAGE ON SCHEMA ecommerce TO savvina_audit_probe",
        "GRANT SELECT ON ecommerce.audit_probe TO savvina_audit_probe",
        "CREATE TABLE ecommerce.audit_probe_group (id int, tenant_id int)",
        "ALTER TABLE ecommerce.audit_probe_group ENABLE ROW LEVEL SECURITY",
        "CREATE POLICY members ON ecommerce.audit_probe_group FOR SELECT TO savvina_audit_group"
        " USING (tenant_id = 1)",
        "GRANT SELECT ON ecommerce.audit_probe_group TO savvina_audit_probe",
    ]
    probe = {**_PG, "username": "savvina_audit_probe", "password": _PROBE_PASSWORD}
    try:
        # Inside the try: a setup that fails half-way is removed by the same teardown, and
        # the teardown first clears what an interrupted earlier run left behind.
        await _pg_admin([*_PG_PROBE_TEARDOWN, *setup])
        facts = await _audit(_PG_ANALYST, "postgresql", [("ecommerce", "audit_probe")])
        (table,) = facts.tables
        assert table.rls_enabled is True and table.rls_forced is False
        assert table.select_policy_applies is True
        audit = evaluate(
            facts,
            PrivacySettings(row_filters={"ecommerce.audit_probe": "tenant_id = 1"}),
            "postgresql",
        )
        assert audit.rls_in_use is True
        assert audit.rls_effective == {"ecommerce.audit_probe": True}
        assert audit.findings == []

        facts = await _audit(probe, "postgresql", [("ecommerce", "audit_probe_group")])
        (table,) = facts.tables
        assert table.rls_enabled is True
        assert table.select_policy_applies is False  # NOINHERIT: MEMBER yes, USAGE no
        audit = evaluate(
            facts,
            PrivacySettings(row_filters={"ecommerce.audit_probe_group": "tenant_id = 1"}),
            "postgresql",
        )
        assert audit.rls_effective == {"ecommerce.audit_probe_group": False}
    finally:
        # The probe role's cached pool must be closed before the role can be dropped.
        await evict_pool(probe)
        await _pg_admin(_PG_PROBE_TEARDOWN)


_PG_OWNER_TEARDOWN = _pg_teardown(
    tables=["ecommerce.audit_probe_owned"],
    roles=["savvina_audit_heir", "savvina_audit_noheir", "savvina_audit_owner"],
)


async def test_postgresql_a_role_inheriting_the_owner_bypasses_rls_until_forced() -> None:
    """The engine lets a role that inherits the owner's privileges bypass RLS as the owner
    does (has_privs_of_role), so the audit must read it as the owner: an oid comparison of
    relowner would say no and claim rls_effective while the engine filters nothing. A member
    granted WITH INHERIT FALSE does not inherit, is not the owner and is filtered."""
    table_name = "ecommerce.audit_probe_owned"
    count = f"SELECT count(*) FROM {table_name}"
    setup = [
        "CREATE ROLE savvina_audit_owner NOLOGIN",
        "GRANT USAGE ON SCHEMA ecommerce TO savvina_audit_owner",
        f"CREATE TABLE {table_name} (id int, tenant_id int)",
        f"INSERT INTO {table_name} VALUES (1, 1), (2, 2)",
        f"ALTER TABLE {table_name} OWNER TO savvina_audit_owner",
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY p ON {table_name} FOR SELECT TO PUBLIC USING (tenant_id = 1)",
        f"CREATE ROLE savvina_audit_heir LOGIN INHERIT PASSWORD '{_PROBE_PASSWORD}'",
        "GRANT savvina_audit_owner TO savvina_audit_heir WITH INHERIT TRUE",
        f"CREATE ROLE savvina_audit_noheir LOGIN INHERIT PASSWORD '{_PROBE_PASSWORD}'",
        "GRANT savvina_audit_owner TO savvina_audit_noheir WITH INHERIT FALSE",
        "GRANT USAGE ON SCHEMA ecommerce TO savvina_audit_noheir",
        f"GRANT SELECT ON {table_name} TO savvina_audit_noheir",
    ]
    heir = {**_PG, "username": "savvina_audit_heir", "password": _PROBE_PASSWORD}
    noheir = {**_PG, "username": "savvina_audit_noheir", "password": _PROBE_PASSWORD}
    privacy = PrivacySettings(row_filters={"ecommerce.audit_probe_owned": "tenant_id = 1"})
    try:
        await _pg_admin([*_PG_OWNER_TEARDOWN, *setup])

        facts = await _audit(heir, "postgresql", [("ecommerce", "audit_probe_owned")])
        (table,) = facts.tables
        assert table.is_owner is True
        assert table.rls_enabled is True and table.rls_forced is False
        assert table.select_policy_applies is True
        audit = evaluate(facts, privacy, "postgresql")
        # The heir also inherits the owner's INSERT, UPDATE and DELETE: a tier 2 finding.
        assert [f.code for f in audit.findings] == [
            Finding.OWNER_WITHOUT_FORCE,
            Finding.WRITE_PRIVILEGES,
        ]
        assert audit.rls_in_use is True
        assert audit.rls_effective == {"ecommerce.audit_probe_owned": False}
        assert audit.findings[0].remediation == (
            'ALTER TABLE "ecommerce"."audit_probe_owned" FORCE ROW LEVEL SECURITY;'
        )
        assert await _pg_count(heir, count) == 2  # the engine agrees: RLS bypassed

        facts = await _audit(noheir, "postgresql", [("ecommerce", "audit_probe_owned")])
        (table,) = facts.tables
        assert table.is_owner is False  # MEMBER yes, USAGE no
        assert table.can_select is True and table.select_policy_applies is True
        assert evaluate(facts, privacy, "postgresql").rls_effective == {
            "ecommerce.audit_probe_owned": True
        }
        assert await _pg_count(noheir, count) == 1

        # The sample superuser: pg_has_role answers true for every role it names, yet it
        # owns support.tickets and not the probe table, so only the former reads as owned.
        facts = await _audit(
            _PG, "postgresql", [("ecommerce", "audit_probe_owned"), ("support", "tickets")]
        )
        by_key = {t.key: t for t in facts.tables}
        assert facts.is_superuser is True
        assert by_key["ecommerce.audit_probe_owned"].is_owner is False
        assert by_key["support.tickets"].is_owner is True
        audit = evaluate(facts, privacy, "postgresql")
        assert [f.code for f in audit.findings] == [Finding.SUPERUSER, Finding.BYPASSRLS]

        await _pg_admin([f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"])
        facts = await _audit(heir, "postgresql", [("ecommerce", "audit_probe_owned")])
        (table,) = facts.tables
        assert table.is_owner is True and table.rls_forced is True
        audit = evaluate(facts, privacy, "postgresql")
        assert audit.rls_effective == {"ecommerce.audit_probe_owned": True}
        assert [f.code for f in audit.findings] == [Finding.WRITE_PRIVILEGES]  # tier 1 gone
        assert has_findings(audit, 1) is False
        assert await _pg_count(heir, count) == 1
    finally:
        await evict_pool(heir)
        await evict_pool(noheir)
        await _pg_admin(_PG_OWNER_TEARDOWN)


_PG_SCHEMA_TEARDOWN = _pg_teardown(schemas=["savvina_audit_schema"], roles=["savvina_audit_reader"])


async def test_postgresql_can_select_needs_usage_on_the_schema() -> None:
    """has_table_privilege alone says true for a table whose schema the role cannot reach;
    the audit also requires USAGE, so SELECT without it reads can_select False."""
    setup = [
        "CREATE SCHEMA savvina_audit_schema",
        "CREATE TABLE savvina_audit_schema.t (id int)",
        f"CREATE ROLE savvina_audit_reader LOGIN PASSWORD '{_PROBE_PASSWORD}'",
        "GRANT SELECT ON savvina_audit_schema.t TO savvina_audit_reader",
    ]
    reader = {**_PG, "username": "savvina_audit_reader", "password": _PROBE_PASSWORD}
    try:
        await _pg_admin([*_PG_SCHEMA_TEARDOWN, *setup])
        facts = await _audit(reader, "postgresql", [("savvina_audit_schema", "t")])
        (table,) = facts.tables
        assert table.exists is True
        assert table.can_select is False

        await _pg_admin(["GRANT USAGE ON SCHEMA savvina_audit_schema TO savvina_audit_reader"])
        facts = await _audit(reader, "postgresql", [("savvina_audit_schema", "t")])
        (table,) = facts.tables
        assert table.can_select is True
    finally:
        await evict_pool(reader)
        await _pg_admin(_PG_SCHEMA_TEARDOWN)


_PG_COLS_TEARDOWN = _pg_teardown(
    tables=["ecommerce.audit_probe_cols", "ecommerce.audit_probe_owned_acl"],
    roles=["savvina_audit_colprobe", "savvina_audit_colgroup"],
)


async def test_postgresql_column_level_update_and_public_insert_are_reported_with_grantees() -> (
    None
):
    """A column UPDATE through an inherited group role and a table INSERT through PUBLIC:
    both grants, each REVOKE against the grantee that holds it."""
    setup = [
        "CREATE TABLE ecommerce.audit_probe_cols (id int, status text, note text)",
        "CREATE ROLE savvina_audit_colgroup",
        "GRANT UPDATE (status) ON ecommerce.audit_probe_cols TO savvina_audit_colgroup",
        f"CREATE ROLE savvina_audit_colprobe LOGIN PASSWORD '{_PROBE_PASSWORD}'",
        "GRANT savvina_audit_colgroup TO savvina_audit_colprobe",
        "GRANT USAGE ON SCHEMA ecommerce TO savvina_audit_colprobe",
        "GRANT INSERT ON ecommerce.audit_probe_cols TO PUBLIC",
        # An owned table with a non-null ACL: the ACL lists the owner, which must not come
        # back as a grantee next to the ownership entry.
        "CREATE TABLE ecommerce.audit_probe_owned_acl (id int)",
        "ALTER TABLE ecommerce.audit_probe_owned_acl OWNER TO savvina_audit_colprobe",
        "GRANT SELECT ON ecommerce.audit_probe_owned_acl TO PUBLIC",
    ]
    probe = {**_PG, "username": "savvina_audit_colprobe", "password": _PROBE_PASSWORD}
    try:
        await _pg_admin([*_PG_COLS_TEARDOWN, *setup])
        facts = await _audit(probe, "postgresql", [])
        grants = [g for g in facts.writes if g.table_name == "audit_probe_cols"]
        assert PrivilegeGrant("ecommerce", "audit_probe_cols", ["INSERT"], "PUBLIC") in grants
        assert (
            PrivilegeGrant(
                "ecommerce", "audit_probe_cols", ["UPDATE"], "savvina_audit_colgroup", ["status"]
            )
            in grants
        )
        owned = [g for g in facts.writes if g.table_name == "audit_probe_owned_acl"]
        assert owned == [
            PrivilegeGrant(
                "ecommerce", "audit_probe_owned_acl", ["DELETE", "INSERT", "UPDATE"], None
            )
        ]
        audit = evaluate(facts, PrivacySettings(), "postgresql", database=_PG["database"])
        finding = next(f for f in audit.findings if f.code == Finding.WRITE_PRIVILEGES)
        lines = finding.remediation.splitlines()
        assert 'REVOKE INSERT ON "ecommerce"."audit_probe_cols" FROM PUBLIC;' in lines
        assert (
            'REVOKE UPDATE ("status") ON "ecommerce"."audit_probe_cols"'
            ' FROM "savvina_audit_colgroup";'
        ) in lines
    finally:
        await evict_pool(probe)
        await _pg_admin(_PG_COLS_TEARDOWN)


_PG_PREDEFINED_TEARDOWN = _pg_teardown(roles=["savvina_audit_reader_all"])


async def test_postgresql_pg_read_all_data_is_revoked_as_a_membership() -> None:
    """An excluded table the role reads only through pg_read_all_data: the finding revokes
    the membership, not a table grant the role does not hold."""
    setup = [
        f"CREATE ROLE savvina_audit_reader_all LOGIN PASSWORD '{_PROBE_PASSWORD}'",
        "GRANT pg_read_all_data TO savvina_audit_reader_all",
    ]
    probe = {**_PG, "username": "savvina_audit_reader_all", "password": _PROBE_PASSWORD}
    try:
        await _pg_admin([*_PG_PREDEFINED_TEARDOWN, *setup])
        facts = await _audit(probe, "postgresql", [("hr", "employees")])
        assert facts.predefined_roles == [
            RoleMembership("pg_read_all_data", "savvina_audit_reader_all")
        ]
        (table,) = facts.tables
        assert table.can_select is True and table.select_grants == []
        audit = evaluate(facts, PrivacySettings(excluded_tables=["hr.employees"]), "postgresql")
        finding = next(f for f in audit.findings if f.code == Finding.READS_EXCLUDED_TABLES)
        assert finding.tables == ["hr.employees"]
        assert finding.remediation.splitlines()[-1] == (
            'REVOKE pg_read_all_data FROM "savvina_audit_reader_all";'
        )
        # Reading every table is no write: pg_read_all_data adds no WRITE_PRIVILEGES.
        assert Finding.WRITE_PRIVILEGES not in [f.code for f in audit.findings]
    finally:
        await evict_pool(probe)
        await _pg_admin(_PG_PREDEFINED_TEARDOWN)


async def test_postgresql_owner_without_force_is_reported_only_with_rls() -> None:
    facts = await _audit(_PG, "postgresql", [("ecommerce", "orders")])
    (table,) = facts.tables
    assert table.is_owner is True and table.rls_enabled is False
    # The adapter reads no write grants for a superuser (evaluate skips tier 2 for one), so
    # the owner's own writes are not in the facts; the heir probe above covers that path.
    assert facts.writes == []
    # The superuser flag is cleared so the tier 2 path runs too; the owner rule (no finding
    # while RLS is off) is what this test checks.
    facts.is_superuser = False
    audit = evaluate(facts, PrivacySettings(row_filters={"ecommerce.orders": "1=1"}), "postgresql")
    # No OWNER_WITHOUT_FORCE, and no WRITE_PRIVILEGES since writes is empty: BYPASSRLS only.
    assert [f.code for f in audit.findings] == [Finding.BYPASSRLS]
    assert audit.rls_in_use is False


async def test_postgresql_analyst_on_the_sample_rls_table_tickets_is_effective() -> None:
    """support.tickets: RLS enabled, a SELECT policy TO savvina_analyst (the role by name,
    neither PUBLIC nor a group), the analyst not the owner: rls_effective, no finding."""
    facts = await _audit(_PG_ANALYST, "postgresql", [("support", "tickets")])
    (table,) = facts.tables
    assert table.exists is True and table.can_select is True
    assert table.rls_enabled is True and table.rls_forced is False
    assert table.is_owner is False
    assert table.select_policy_applies is True
    audit = evaluate(
        facts, PrivacySettings(row_filters={"support.tickets": "category <> 'fraud'"}), "postgresql"
    )
    assert audit.rls_in_use is True
    assert audit.rls_effective == {"support.tickets": True}
    assert audit.findings == []


async def test_postgresql_sample_superuser_owns_tickets_without_force() -> None:
    """The same table as the sample superuser, who owns it: SUPERUSER, BYPASSRLS and
    OWNER_WITHOUT_FORCE under the RLS title (rls_in_use), rls_effective False."""
    facts = await _audit(_PG, "postgresql", [("support", "tickets")])
    assert facts.bypasses_rls is True
    (table,) = facts.tables
    assert table.is_owner is True and table.rls_enabled is True and table.rls_forced is False
    assert table.select_policy_applies is True  # tickets_owner_all: FOR ALL TO savvina
    audit = evaluate(facts, PrivacySettings(row_filters={"support.tickets": "1=1"}), "postgresql")
    assert [f.code for f in audit.findings] == [
        Finding.SUPERUSER,
        Finding.BYPASSRLS,
        Finding.OWNER_WITHOUT_FORCE,
    ]
    assert audit.rls_in_use is True
    assert has_findings(audit, 1) is True and has_findings(audit, 2) is False
    assert audit.rls_effective == {"support.tickets": False}
    owner = next(f for f in audit.findings if f.code == Finding.OWNER_WITHOUT_FORCE)
    assert owner.remediation == 'ALTER TABLE "support"."tickets" FORCE ROW LEVEL SECURITY;'


async def test_mysql_analyst_reports_select_only_on_the_granted_tables() -> None:
    facts = await _audit(
        _MYSQL_ANALYST,
        "mysql",
        [("sample_delivery", "orders"), ("sample_delivery", "customers"), (None, "orders")],
    )
    assert facts.role == "savvina_analyst@%"
    assert facts.is_superuser is False and facts.writes == []
    # The bare entry resolves to the same table as the qualified one: one row, not two.
    assert [t.key for t in facts.tables if t.table_name == "orders"] == ["sample_delivery.orders"]
    by_key = {t.key: t for t in facts.tables}
    assert by_key["sample_delivery.orders"].can_select is True
    assert by_key["sample_delivery.orders"].can_write is False
    # information_schema.tables lists only the tables the account has a privilege on: a
    # table the analyst cannot read at all is not visible, so it reads exists False.
    assert by_key["sample_delivery.customers"].exists is False
    assert by_key["sample_delivery.customers"].can_select is False
    audit = evaluate(
        facts,
        PrivacySettings(
            row_filters={"sample_delivery.orders": "restaurant_id = 1"},
            excluded_tables=["customers"],
        ),
        "mysql",
        database="sample_delivery",
    )
    assert audit.findings == []
    assert audit.rls_effective == {"sample_delivery.orders": False}


# What the MySQL adapter reports for ALL PRIVILEGES on a database: the write privileges that
# level can hold, sorted, so the REVOKE leaves SELECT in place.
_MYSQL_SCHEMA_ALL_WRITES = [
    "ALTER",
    "ALTER ROUTINE",
    "CREATE",
    "CREATE ROUTINE",
    "CREATE TEMPORARY TABLES",
    "CREATE VIEW",
    "DELETE",
    "DROP",
    "EVENT",
    "INDEX",
    "INSERT",
    "LOCK TABLES",
    "REFERENCES",
    "TRIGGER",
    "UPDATE",
]


async def test_mysql_sample_user_has_the_schema_grant() -> None:
    facts = await _audit(_MYSQL, "mysql", [("sample_delivery", "orders")])
    assert facts.is_superuser is False
    # As SHOW GRANTS prints it (`sample\_delivery`.*): a REVOKE must name the stored grant.
    # ALL PRIVILEGES there is reported as its writes: revoking ALL would take SELECT too.
    assert facts.writes == [
        PrivilegeGrant("sample\\_delivery", None, _MYSQL_SCHEMA_ALL_WRITES, "savvina@%")
    ]
    assert facts.has_super is False
    (table,) = facts.tables
    assert table.can_select is True and table.can_write is True
    assert table.select_grants == [
        PrivilegeGrant("sample\\_delivery", None, ["SELECT"], "savvina@%")
    ]
    audit = evaluate(facts, PrivacySettings(row_filters={"sample_delivery.orders": "1=1"}), "mysql")
    assert [f.code for f in audit.findings] == [Finding.WRITE_PRIVILEGES]
    (finding,) = audit.findings
    assert finding.tables == []  # a schema-level grant contributes no table key
    assert finding.remediation == (
        f"REVOKE {', '.join(_MYSQL_SCHEMA_ALL_WRITES)} ON `sample\\_delivery`.* FROM `savvina`@`%`;"
    )


async def test_mysql_a_grant_through_a_default_role_is_seen() -> None:
    """The spec's section 3.2 verification: SHOW GRANTS for the current user folds the active
    (default) roles in on MySQL 8, so a SELECT that reaches the account only through a role
    reads can_select True."""
    import aiomysql

    admin = await aiomysql.connect(
        host=_MYSQL_ROOT["host"],
        port=_MYSQL_ROOT["port"],
        user=_MYSQL_ROOT["username"],
        password=_MYSQL_ROOT["password"],
        db=_MYSQL_ROOT["database"],
        autocommit=True,
    )
    probe = {**_MYSQL, "username": "audit_probe", "password": _PROBE_PASSWORD}
    teardown = ["DROP USER IF EXISTS 'audit_probe'@'%'", "DROP ROLE IF EXISTS 'audit_probe_role'"]

    async def run(statements: list[str]) -> None:
        # DROP ... IF EXISTS on an absent account raises Note 3162, which aiomysql passes to
        # warnings.warn as its bare text (no code): expected on every clean run, so that one
        # message is not shown; any other warning still is.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"Authorization ID .* does not exist")
            async with admin.cursor() as cur:
                for statement in statements:
                    await cur.execute(statement)

    try:
        await run(
            [
                *teardown,
                "CREATE ROLE 'audit_probe_role'",
                "GRANT SELECT ON sample_delivery.* TO 'audit_probe_role'",
                f"CREATE USER 'audit_probe'@'%' IDENTIFIED BY '{_PROBE_PASSWORD}'",
                "GRANT 'audit_probe_role' TO 'audit_probe'@'%'",
                "SET DEFAULT ROLE 'audit_probe_role' TO 'audit_probe'@'%'",
            ]
        )
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        (table,) = facts.tables
        assert table.can_select is True, "the role's grant did not reach SHOW GRANTS"
        assert table.can_write is False
    finally:
        try:
            await run(teardown)
        finally:
            admin.close()


async def _mysql_admin():
    """A root connection to the sample MySQL and a ``run(statements)`` helper on it."""
    import aiomysql

    admin = await aiomysql.connect(
        host=_MYSQL_ROOT["host"],
        port=_MYSQL_ROOT["port"],
        user=_MYSQL_ROOT["username"],
        password=_MYSQL_ROOT["password"],
        db=_MYSQL_ROOT["database"],
        autocommit=True,
    )

    async def run(statements: list[str]) -> None:
        # As in test_mysql_a_grant_through_a_default_role_is_seen: Note 3162 on an absent
        # account is expected on every clean run.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"Authorization ID .* does not exist")
            async with admin.cursor() as cur:
                for statement in statements:
                    await cur.execute(statement)

    return admin, run


async def test_mysql_revoking_a_schema_all_grants_writes_keeps_select() -> None:
    """ALL PRIVILEGES on a database is reported as the writes that level can hold: the
    REVOKE built from them runs, and afterwards the account still reads the database (a REVOKE
    ALL PRIVILEGES would have left it with ERROR 1044) and holds no write."""
    admin, run = await _mysql_admin()
    probe = {**_MYSQL, "username": "audit_allprobe", "password": _PROBE_PASSWORD}
    teardown = ["DROP USER IF EXISTS 'audit_allprobe'@'%'"]
    try:
        await run(
            [
                *teardown,
                f"CREATE USER 'audit_allprobe'@'%' IDENTIFIED BY '{_PROBE_PASSWORD}'",
                "GRANT ALL ON `sample\\_delivery`.* TO 'audit_allprobe'@'%'",
            ]
        )
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        (write,) = facts.writes
        assert write == PrivilegeGrant(
            "sample\\_delivery", None, _MYSQL_SCHEMA_ALL_WRITES, "audit_allprobe@%"
        )
        await run(
            [
                f"REVOKE {', '.join(write.privileges)} ON `sample\\_delivery`.*"
                " FROM `audit_allprobe`@`%`"
            ]
        )
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        assert facts.writes == []
        (table,) = facts.tables
        assert table.can_select is True and table.can_write is False
        assert table.select_grants == [
            PrivilegeGrant("sample\\_delivery", None, ["SELECT"], "audit_allprobe@%")
        ]
    finally:
        try:
            await run(teardown)
        finally:
            admin.close()


async def test_mysql_a_column_grant_through_a_default_role_names_the_role() -> None:
    """A column-level UPDATE that reaches the account only through its default role: the
    grant is reported with its column and the role as its grantee."""
    admin, run = await _mysql_admin()
    probe = {**_MYSQL, "username": "audit_colprobe", "password": _PROBE_PASSWORD}
    teardown = [
        "DROP USER IF EXISTS 'audit_colprobe'@'%'",
        "DROP ROLE IF EXISTS 'audit_colprobe_role'",
    ]
    try:
        await run(
            [
                *teardown,
                "CREATE ROLE 'audit_colprobe_role'",
                "GRANT UPDATE (status) ON sample_delivery.orders TO 'audit_colprobe_role'",
                f"CREATE USER 'audit_colprobe'@'%' IDENTIFIED BY '{_PROBE_PASSWORD}'",
                "GRANT 'audit_colprobe_role' TO 'audit_colprobe'@'%'",
                "SET DEFAULT ROLE 'audit_colprobe_role' TO 'audit_colprobe'@'%'",
            ]
        )
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        assert facts.writes == [
            PrivilegeGrant(
                "sample_delivery", "orders", ["UPDATE"], "audit_colprobe_role@%", ["status"]
            )
        ]
        (table,) = facts.tables
        assert table.exists is True and table.can_write is True
        assert table.can_select is False  # a column grant is not SELECT on the table
        audit = evaluate(facts, PrivacySettings(), "mysql", database="sample_delivery")
        (finding,) = audit.findings
        assert finding.code == Finding.WRITE_PRIVILEGES
        assert finding.tables == ["sample_delivery.orders"]
        assert finding.remediation == (
            "REVOKE UPDATE (`status`) ON `sample_delivery`.`orders` FROM `audit_colprobe_role`@`%`;"
        )
    finally:
        try:
            await run(teardown)
        finally:
            admin.close()


async def test_mysql_a_merged_grant_line_is_split_between_the_account_and_its_role() -> None:
    """MySQL prints the account's and its active role's privileges on one line per scope. The
    role's share is the per-privilege, per-column difference: each REVOKE names the grantee
    that holds the privilege and runs as written (no ERROR 1147, no grant on that grantee)."""
    admin, run = await _mysql_admin()
    probe = {**_MYSQL, "username": "audit_mergeprobe", "password": _PROBE_PASSWORD}
    account, role = "audit_mergeprobe@%", "audit_mergeprobe_role@%"
    teardown = [
        "DROP USER IF EXISTS 'audit_mergeprobe'@'%'",
        "DROP ROLE IF EXISTS 'audit_mergeprobe_role'",
    ]
    try:
        await run(
            [
                *teardown,
                "CREATE ROLE 'audit_mergeprobe_role'",
                "GRANT INSERT ON sample_delivery.* TO 'audit_mergeprobe_role'",
                "GRANT UPDATE (status) ON sample_delivery.orders TO 'audit_mergeprobe_role'",
                f"CREATE USER 'audit_mergeprobe'@'%' IDENTIFIED BY '{_PROBE_PASSWORD}'",
                "GRANT SELECT ON sample_delivery.* TO 'audit_mergeprobe'@'%'",
                "GRANT UPDATE (id) ON sample_delivery.orders TO 'audit_mergeprobe'@'%'",
                "GRANT 'audit_mergeprobe_role' TO 'audit_mergeprobe'@'%'",
                "SET DEFAULT ROLE 'audit_mergeprobe_role' TO 'audit_mergeprobe'@'%'",
            ]
        )
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        assert facts.writes == [
            PrivilegeGrant("sample_delivery", None, ["INSERT"], role),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], account, ["id"]),
            PrivilegeGrant("sample_delivery", "orders", ["UPDATE"], role, ["status"]),
        ]
        (table,) = facts.tables
        assert table.can_select is True and table.can_write is True
        assert table.select_grants == [PrivilegeGrant("sample_delivery", None, ["SELECT"], account)]

        audit = evaluate(facts, PrivacySettings(), "mysql", database="sample_delivery")
        finding = next(f for f in audit.findings if f.code == Finding.WRITE_PRIVILEGES)
        lines = finding.remediation.splitlines()
        role_lines = [
            "REVOKE INSERT ON `sample_delivery`.* FROM `audit_mergeprobe_role`@`%`;",
            "REVOKE UPDATE (`status`) ON `sample_delivery`.`orders`"
            " FROM `audit_mergeprobe_role`@`%`;",
        ]
        account_line = (
            "REVOKE UPDATE (`id`) ON `sample_delivery`.`orders` FROM `audit_mergeprobe`@`%`;"
        )
        assert sorted(lines) == sorted([*role_lines, account_line])
        # The remediation runs as root: a REVOKE naming a grantee that does not hold the
        # privilege would raise ERROR 1147 (there is no such grant defined).
        await run(role_lines)
        await run([account_line])
        facts = await _audit(probe, "mysql", [("sample_delivery", "orders")])
        assert facts.writes == []
        (table,) = facts.tables
        assert table.can_select is True and table.can_write is False
    finally:
        try:
            await run(teardown)
        finally:
            admin.close()
