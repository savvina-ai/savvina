# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The DISTINCT (#35) and unnamed-subquery-item (#36) strict rules, checked on the real
engines: each attack runs on the engine (so the guard is what stops it) and the guard
refuses it; each legitimate query of the same shape runs on the engine and passes.

Opt-in: ``SAVVINA_LIVE_TEST_DBS=1`` with the ``test-dbs`` compose profile up
(``docker compose --profile test-dbs up -d`` from the repo root: ``sample-postgres`` on
5435, ``sample-mysql`` on 3307). Skipped otherwise. Every statement reads only literals
and derived tables, so no table of the sample databases is needed and nothing is written.
"""

# ruff: noqa: S608 — literal-only test statements.

from __future__ import annotations

import os

import pytest

from app.datasources.models import PrivacySettings
from app.services.row_filter_errors import SensitiveColumnRejectedError
from app.services.sensitive_guard import check_strict_sensitive

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
}
_MYSQL = {
    "host": "localhost",
    "port": 3307,
    "database": "sample_delivery",
    "username": "savvina",
    "password": os.environ.get("SAMPLE_MYSQL_PASSWORD", "savvina_demo"),
}

_EXACT = PrivacySettings(
    strict_sensitive_columns=True, sensitive_column_patterns=[], sensitive_columns=["nick"]
)

_DISTINCT = "removes duplicates of the column nick"
_UNNAMED = "an unnamed subquery item that reads a sensitive column"
_ALIAS_OUTSIDE = "the column n outside its SELECT list"

# A scalar subquery item that reads the sensitive column.
_NICK_SUB = "(SELECT nick FROM (SELECT 'b' AS nick) u LIMIT 1)"
_COLUMN_USE = "the column nick outside its SELECT list"


async def _engine_rows(dialect: str, sql: str) -> list[tuple]:
    """Run *sql* on the engine over a plain driver connection and return its rows."""
    if dialect == "postgresql":
        import asyncpg

        conn = await asyncpg.connect(
            host=_PG["host"],
            port=_PG["port"],
            database=_PG["database"],
            user=_PG["username"],
            password=_PG["password"],
            ssl=False,
        )
        try:
            return [tuple(r) for r in await conn.fetch(sql)]
        finally:
            await conn.close()
    if dialect != "mysql":
        raise ValueError(f"unknown dialect {dialect!r}")
    import aiomysql

    conn = await aiomysql.connect(
        host=_MYSQL["host"],
        port=_MYSQL["port"],
        db=_MYSQL["database"],
        user=_MYSQL["username"],
        password=_MYSQL["password"],
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return [tuple(r) for r in await cur.fetchall()]
    finally:
        conn.close()


def _guard_verdict(sql: str, dialect: str) -> str | None:
    """The guard's refusal message, or None when it passes *sql*."""
    try:
        check_strict_sensitive(sql, _EXACT, dialect)
    except SensitiveColumnRejectedError as exc:
        return str(exc)
    return None


_BOTH = ("postgresql", "mysql")

_DISTINCT_TOP = "SELECT DISTINCT nick FROM (SELECT 'a' AS nick UNION ALL SELECT 'a') u"
_DISTINCT_DERIVED = (
    "SELECT count(*) FROM (SELECT DISTINCT nick FROM (SELECT 'a' AS nick UNION ALL SELECT 'a') u) s"
)
_DISTINCT_CTE = (
    "WITH c AS (SELECT DISTINCT nick FROM (SELECT 'a' AS nick) u)"
    " SELECT count(*) FROM (SELECT * FROM c) d"
)

# (dialect, engine statement, expected engine rows): the same statement goes to the guard.
_DISTINCT_ATTACKS = [
    pytest.param(d, sql, rows, id=f"{d}-{name}")
    for d in _BOTH
    for name, sql, rows in (
        ("top", _DISTINCT_TOP, [("a",)]),
        # The oracle: the duplicate collapsed to one row inside the derived table.
        ("derived", _DISTINCT_DERIVED, [(1,)]),
        ("cte-under-derived", _DISTINCT_CTE, [(1,)]),
    )
]

_FROM_TWO = "FROM (SELECT 1 AS x UNION ALL SELECT 2) t"

# (dialect, statement, expected engine rows, expected guard fragment). The engine runs the
# very statement the guard judges. How each engine names an unaliased item (verified on
# MySQL 8.0.46 and PostgreSQL 16, 2026-11-03): MySQL after its SQL text, which ORDER BY,
# GROUP BY, HAVING and a derived table's outer query can use; PostgreSQL after the
# subquery's own output column (``nick`` here), or ``?column?`` for an unnamed expression.
# The PostgreSQL names are refused already as uses of the column; the MySQL text name only
# by the alias rule (#36).
_NICK_TEXT = f"`{_NICK_SUB}`"
_UNNAMED_ATTACKS = [
    pytest.param(
        "mysql",
        f"SELECT {_NICK_SUB} {_FROM_TWO} ORDER BY {_NICK_TEXT}",
        [("b",), ("b",)],
        _UNNAMED,
        id="mysql-order-by",
    ),
    pytest.param(
        "mysql",
        f"SELECT {_NICK_SUB} {_FROM_TWO} GROUP BY {_NICK_TEXT}",
        [("b",)],
        _UNNAMED,
        id="mysql-group-by",
    ),
    pytest.param(
        "mysql",
        f"SELECT {_NICK_SUB} {_FROM_TWO} GROUP BY {_NICK_TEXT} HAVING {_NICK_TEXT} LIKE 'b%'",
        [("b",)],
        _UNNAMED,
        id="mysql-group-by-having",
    ),
    pytest.param(
        "mysql",
        f"SELECT 1 FROM (SELECT {_NICK_SUB} FROM (SELECT 1 AS x) t) d WHERE d.{_NICK_TEXT} = 'b'",
        [(1,)],
        _UNNAMED,
        id="mysql-derived-where",
    ),
    pytest.param(
        "postgresql",
        f"SELECT {_NICK_SUB} {_FROM_TWO} ORDER BY nick",
        [("b",), ("b",)],
        _UNNAMED,
        id="postgresql-order-by-inner-name",
    ),
    pytest.param(
        "postgresql",
        f"SELECT {_NICK_SUB} {_FROM_TWO} GROUP BY nick",
        [("b",)],
        _UNNAMED,
        id="postgresql-group-by-inner-name",
    ),
    pytest.param(
        "postgresql",
        f"SELECT 1 FROM (SELECT {_NICK_SUB} FROM (SELECT 1 AS x) t) d WHERE d.nick = 'b'",
        [(1,)],
        _COLUMN_USE,
        id="postgresql-derived-inner-name",
    ),
    pytest.param(
        "postgresql",
        "SELECT (SELECT nick || '' FROM (SELECT 'b' AS nick) u LIMIT 1)"
        f' {_FROM_TWO} ORDER BY "?column?"',
        [("b",), ("b",)],
        _COLUMN_USE,
        id="postgresql-order-by-question-column",
    ),
]

_NICK_ALIAS = f"SELECT {_NICK_SUB} AS n FROM (SELECT 1 AS x) t"

# (dialect, statement, expected engine rows, expected guard fragment or None for a pass).
_SAME_SHAPE_CONTROLS = [
    *(
        pytest.param(d, sql, rows, expect, id=f"{d}-{name}")
        for d in _BOTH
        for name, sql, rows, expect in (
            (
                "distinct-plain-column",
                "SELECT DISTINCT x FROM (SELECT 1 AS x UNION ALL SELECT 1) t",
                [(1,)],
                None,
            ),
            # An alias of a sensitive item is sensitive: ORDER BY on it is a column use.
            ("alias-order-by", f"{_NICK_ALIAS} ORDER BY n", [("b",)], _ALIAS_OUTSIDE),
            ("aliased-subquery-item", _NICK_ALIAS, [("b",)], None),
            ("plain-select", "SELECT nick FROM (SELECT 'a' AS nick) u", [("a",)], None),
        )
    ),
    pytest.param(
        "postgresql",
        "SELECT DISTINCT ON (x) x, nick FROM (SELECT 1 AS x, 'a' AS nick) t",
        [(1, "a")],
        None,
        id="postgresql-distinct-on",
    ),
]


@pytest.mark.parametrize(("dialect", "sql", "rows"), _DISTINCT_ATTACKS)
async def test_distinct_over_sensitive_runs_on_engine_and_guard_refuses(dialect, sql, rows):
    assert await _engine_rows(dialect, sql) == rows
    verdict = _guard_verdict(sql, dialect)
    assert verdict is not None, "the guard passed a DISTINCT over a sensitive column"
    assert _DISTINCT in verdict


@pytest.mark.parametrize(("dialect", "sql", "rows", "fragment"), _UNNAMED_ATTACKS)
async def test_unnamed_subquery_item_runs_on_engine_and_guard_refuses(dialect, sql, rows, fragment):
    assert await _engine_rows(dialect, sql) == rows
    verdict = _guard_verdict(sql, dialect)
    assert verdict is not None, "the guard passed an unnamed subquery item over nick"
    assert fragment in verdict, verdict


@pytest.mark.parametrize(("dialect", "sql", "rows", "expect"), _SAME_SHAPE_CONTROLS)
async def test_same_shape_queries_run_on_engine_with_expected_verdict(dialect, sql, rows, expect):
    assert await _engine_rows(dialect, sql) == rows
    verdict = _guard_verdict(sql, dialect)
    if expect is None:
        assert verdict is None, verdict
    else:
        assert verdict is not None and expect in verdict, verdict
