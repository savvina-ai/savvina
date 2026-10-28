# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""unwrap_row_filter: stored SQL loses exactly the Phase 1 wrap put on it."""

from __future__ import annotations

import re

import pytest

from app.services.execution import unwrap_row_filter

_F = "tenant_id = 7"

_PG_TRAILING = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?|\s+OFFSET\s+\d+\s+LIMIT\s+\d+)$", re.IGNORECASE
)
_MYSQL_TRAILING = re.compile(
    r"(\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?|\s+LIMIT\s+\d+\s*,\s*\d+)$", re.IGNORECASE
)


def _new_wrap(sql: str, row_filter_sql: str, dialect: str) -> str:
    """The Phase 1 wrap, copied from the removed ``_inject_row_filter``: the query on its own
    lines, the engine's trailing LIMIT/OFFSET shape hoisted outside."""
    sql = sql.rstrip().rstrip(";").rstrip()
    pattern = _PG_TRAILING if dialect == "PostgreSQL" else _MYSQL_TRAILING
    match = pattern.search(sql)
    tail = ""
    if match:
        tail = match.group(1)
        sql = sql[: match.start()]
    return f"SELECT * FROM (\n{sql}\n) AS _sift_q WHERE {row_filter_sql}{tail}"  # noqa: S608


def _old_wrap(sql: str, row_filter_sql: str) -> str:
    """The wrap the non-streaming path stored before Task 9: no newlines, no balance check."""
    sql = sql.rstrip(";").rstrip()
    limit_match = re.search(r"(\s+LIMIT\s+\d+)\s*$", sql, re.IGNORECASE)
    limit_clause = ""
    if limit_match:
        limit_clause = limit_match.group(1)
        sql = sql[: limit_match.start()]
    return f"SELECT * FROM ({sql}) AS _sift_q WHERE {row_filter_sql}{limit_clause}"  # noqa: S608


_QUERIES = [
    "SELECT id, tenant_id FROM t",
    "SELECT id, tenant_id FROM t LIMIT 10",
    "SELECT id, tenant_id FROM t\nLIMIT 10",
    "SELECT id, tenant_id FROM t LIMIT 10 OFFSET 20",
    "SELECT id, tenant_id FROM t OFFSET 20 LIMIT 10",
    "SELECT id, tenant_id FROM t LIMIT 20, 10",
    "SELECT id, tenant_id FROM t\nlimit 10  offset 20",
    "WITH x AS (SELECT id, tenant_id FROM t) SELECT * FROM x",
    "SELECT id, tenant_id FROM t WHERE name = ')' ORDER BY id",
    'SELECT `id`, tenant_id FROM `t` WHERE note = "a ) b"',
    "SELECT id, tenant_id FROM t -- every row",
    "\n  SELECT id, tenant_id FROM t",
]

_FILTERS = [
    _F,
    "tenant_id IN (SELECT id FROM tenants WHERE name = 'a ) b')",
    "org_id = 42 AND region = 'EU'",
]


class TestRoundTrip:
    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL"])
    @pytest.mark.parametrize("sql", _QUERIES)
    @pytest.mark.parametrize("row_filter_sql", _FILTERS)
    def test_new_form_round_trips(self, dialect: str, sql: str, row_filter_sql: str) -> None:
        assert unwrap_row_filter(_new_wrap(sql, row_filter_sql, dialect)) == sql

    @pytest.mark.parametrize("sql", [q for q in _QUERIES if "--" not in q])
    @pytest.mark.parametrize("row_filter_sql", _FILTERS)
    def test_old_form_round_trips(self, sql: str, row_filter_sql: str) -> None:
        assert unwrap_row_filter(_old_wrap(sql, row_filter_sql)) == sql

    def test_old_form_literal(self) -> None:
        stored = (
            "SELECT * FROM (SELECT id, tenant_id FROM t) AS _sift_q WHERE tenant_id = 7 LIMIT 5"
        )
        assert unwrap_row_filter(stored) == "SELECT id, tenant_id FROM t LIMIT 5"

    @pytest.mark.parametrize(
        ("tail", "expected"),
        [
            (" LIMIT 10 OFFSET 20", "SELECT id, tenant_id FROM t LIMIT 10 OFFSET 20"),
            (" OFFSET 20 LIMIT 10", "SELECT id, tenant_id FROM t OFFSET 20 LIMIT 10"),
            (" LIMIT 20, 10", "SELECT id, tenant_id FROM t LIMIT 20, 10"),
            (" LIMIT 20,10", "SELECT id, tenant_id FROM t LIMIT 20,10"),
        ],
    )
    def test_hoisted_shapes_literal(self, tail: str, expected: str) -> None:
        stored = (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n) AS _sift_q WHERE tenant_id = 7" + tail  # noqa: S608
        )
        assert unwrap_row_filter(stored) == expected

    def test_new_form_literal(self) -> None:
        stored = (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n) AS _sift_q WHERE tenant_id = 7 LIMIT 5"
        )
        assert unwrap_row_filter(stored) == "SELECT id, tenant_id FROM t LIMIT 5"

    def test_trailing_semicolon_is_not_restored(self) -> None:
        wrapped = _new_wrap("SELECT id, tenant_id FROM t;", _F, "PostgreSQL")
        assert unwrap_row_filter(wrapped) == "SELECT id, tenant_id FROM t"

    @pytest.mark.parametrize("wrap", ["new", "old"])
    def test_double_wrap_from_an_old_cache_hit_is_fully_removed(self, wrap: str) -> None:
        inner = "SELECT id, tenant_id FROM t LIMIT 3"
        if wrap == "new":
            twice = _new_wrap(_new_wrap(inner, _F, "PostgreSQL"), _F, "MySQL")
        else:
            twice = _old_wrap(_old_wrap(inner, _F), _F)
        assert unwrap_row_filter(twice) == inner


class TestUntouched:
    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "SELECT 1",
            "SELECT id FROM t AS _sift_q WHERE id = 1",
            "SELECT * FROM t _sift_q WHERE tenant_id = 7",
            "select * from (SELECT 1) AS _sift_q WHERE x = 1",
            " SELECT * FROM (SELECT 1) AS _sift_q WHERE x = 1",
            "SELECT * FROM (SELECT 1) AS _sift_q WHERE ",
            "SELECT * FROM (SELECT 1) AS _sift_q",
            "SELECT * FROM (SELECT 1) AS _sift_q2 WHERE x = 1",
            "SELECT * FROM (SELECT 1) AS other WHERE x = 1",
            "SELECT * FROM (SELECT 1) AS _sift_q JOIN u ON true WHERE x = 1",
            "SELECT * FROM (SELECT 1) x, (SELECT 2) AS _sift_q WHERE x = 1",
            "SELECT * FROM (SELECT 1 AS a) AS _sift_q WHERE a = 1) AS y",
            "SELECT * FROM (SELECT 1 -- ) AS _sift_q WHERE x = 1",
            "SELECT * FROM (SELECT ')' AS a",
            "SELECT * FROM (SELECT 1\n) AS _sift_q WHERE x = 1",
            "SELECT * FROM (\n) AS _sift_q WHERE x = 1",
            "SELECT * FROM (\n\n) AS _sift_q WHERE x = 1",
            "SELECT * FROM (SELECT 1 ) AS _sift_q WHERE x = 1",
            "SELECT * FROM (SELECT 1) AS _sift_q WHERE (x = 1",
            "SELECT * FROM (SELECT 1) AS _sift_q WHERE  LIMIT 5",
            "SELECT * FROM () AS _sift_q WHERE x = 1",
            "SELECT note FROM t WHERE note = 'SELECT * FROM (x) AS _sift_q WHERE y'",
        ],
    )
    def test_anything_else_is_returned_unchanged(self, sql: str) -> None:
        assert unwrap_row_filter(sql) is sql

    def test_exact_shape_is_a_wrap_whoever_wrote_it(self) -> None:
        """Accepted limit: a query written in exactly the wrap's shape (the model copying
        it from old history) cannot be told from a wrap, and loses its outer layer. Run
        under the current filter, it shows no row the filter hides."""
        sql = "SELECT * FROM (SELECT id, tenant_id FROM t) AS _sift_q WHERE id > 1"
        assert unwrap_row_filter(sql) == "SELECT id, tenant_id FROM t"
