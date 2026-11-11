# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Sensitive values are redacted by what the SELECT list exposes, not by what it is called."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from app.datasources.models import PrivacySettings
from app.schemas.chat import QueryResultsResponse
from app.services import result_masking
from app.services.result_masking import (
    MASK_ALL_COLUMNS,
    _backslash_is_escape,
    _classify_select_columns,
    _mask_sensitive_result_columns,
    _plan_result_mask,
    _resolve_mask_indices,
)

_PRIVACY = PrivacySettings()  # default patterns include salary, email, phone …


# What ``_masked`` returns for ``MASK_ALL_COLUMNS``: the sentinel iterates as empty, so it
# compares equal to ``set()``, and an expected ``set()`` must not pass on it.
_ALL = "MASK_ALL_COLUMNS"


def _masked(sql: str, privacy: PrivacySettings = _PRIVACY, **kw) -> set[str] | str | None:
    result = _classify_select_columns(sql, privacy, **kw)
    if result is None:
        return None
    return _ALL if result[0] is MASK_ALL_COLUMNS else result[0]


def _response(columns: list[str], rows: list[list]) -> QueryResultsResponse:
    return QueryResultsResponse(
        columns=columns,
        column_types=["text"] * len(columns),
        rows=rows,
        row_count=len(rows),
        truncated=False,
        execution_time_ms=0.0,
    )


class TestClassifySelectColumns:
    def test_direct_sensitive_column(self) -> None:
        assert _masked("SELECT salary FROM employees") == {"salary"}

    def test_an_explicit_sensitive_column_is_masked_like_a_pattern(self) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=["name"])
        assert _masked("SELECT name, username FROM t", privacy) == {"name"}

    def test_aliased_sensitive_column(self) -> None:
        assert _masked("SELECT email AS contact FROM users") == {"contact"}

    def test_scalar_expression_over_sensitive_with_alias(self) -> None:
        assert _masked("SELECT lower(email) AS contact FROM users") == {"contact"}

    def test_unaliased_scalar_expression_gets_the_engine_default_name(self) -> None:
        # PostgreSQL names it "lower". Elsewhere (MySQL: "lower(email)") the guess is missing
        # from the result columns and every column is masked — see TestMaskEndToEnd.
        assert _masked("SELECT lower(email) FROM users") == {"lower"}

    def test_aggregate_is_not_masked(self) -> None:
        assert _masked("SELECT AVG(salary) AS avg_salary FROM employees") == set()

    def test_aggregate_alias_is_reported_exempt(self) -> None:
        result = _classify_select_columns("SELECT SUM(salary) AS salary FROM employees", _PRIVACY)
        assert result == (set(), {"salary"})

    def test_count_is_not_masked(self) -> None:
        assert _masked("SELECT COUNT(email) AS n FROM users") == set()

    def test_count_star_is_not_a_wildcard(self) -> None:
        assert _classify_select_columns("SELECT COUNT(*) AS n FROM users", _PRIVACY) == (
            set(),
            {"n"},
        )

    def test_value_returning_aggregates_are_masked(self) -> None:
        # Only statistical aggregates are exempt. MAX/MIN/string_agg … return a stored
        # value, so over a sensitive column they are masked like the column itself.
        assert _masked("SELECT MAX(email) AS email FROM users") == {"email"}
        assert _masked("SELECT string_agg(email, ',') AS everyone FROM users") == {"everyone"}

    def test_mixed_columns(self) -> None:
        assert _masked("SELECT id, name, salary, email AS e FROM employees") == {"salary", "e"}

    def test_table_qualified_column(self) -> None:
        assert _masked("SELECT e.salary FROM employees e") == {"salary"}

    def test_double_quoted_column(self) -> None:
        assert _masked('SELECT "salary" FROM employees') == {"salary"}

    def test_backtick_quoted_column(self) -> None:
        # The output name is unquoted: the driver returns `salary`, not `` `salary` ``.
        assert _masked("SELECT `salary` FROM employees") == {"salary"}

    def test_double_quoted_literal_dialect_ignores_a_string(self) -> None:
        sql = 'SELECT IF(kind = "salary", amount, 0) AS v FROM pay'
        assert _masked(sql, double_quote_is_string_literal=True) == set()
        assert _masked(sql, double_quote_is_string_literal=False) == {"v"}

    def test_ranking_window_over_sensitive_not_masked(self) -> None:
        sql = "SELECT name, RANK() OVER (ORDER BY salary DESC) AS rnk FROM employees"
        assert _masked(sql) == set()

    def test_value_window_over_sensitive_still_masked(self) -> None:
        sql = "SELECT LAG(salary) OVER (ORDER BY id) AS prev FROM employees"
        assert _masked(sql) == {"prev"}

    def test_wildcard_is_uncertain(self) -> None:
        assert _masked("SELECT * FROM employees") is None
        assert _masked("SELECT e.* FROM employees e") is None

    def test_unparseable_fails_closed(self) -> None:
        # whether it projects a whole row cannot be told (the database would refuse it anyway)
        assert _masked("not sql at all") == _ALL
        assert _masked("not sql at all", double_quote_is_string_literal=True) == _ALL

    def test_cte_classifies_the_outer_select(self) -> None:
        sql = "WITH x AS (SELECT id, salary FROM employees) SELECT salary AS pay FROM x"
        assert _masked(sql) == {"pay"}

    def test_distinct_named_column(self) -> None:
        assert _masked("SELECT DISTINCT email FROM users") == {"email"}


class TestMaskSensitiveResultColumns:
    def test_no_privacy_returns_same_object(self) -> None:
        resp = _response(["email"], [["a@x"]])
        assert _mask_sensitive_result_columns(resp, None, sql="SELECT email FROM u") is resp

    def test_direct_column_masked_with_sql(self) -> None:
        resp = _response(["salary"], [[100]])
        out = _mask_sensitive_result_columns(resp, _PRIVACY, sql="SELECT salary FROM e")
        assert out.rows == [["[REDACTED]"]]

    def test_alias_masked_by_classifier(self) -> None:
        resp = _response(["contact"], [["a@x"]])
        out = _mask_sensitive_result_columns(resp, _PRIVACY, sql="SELECT email AS contact FROM u")
        assert out.rows == [["[REDACTED]"]]

    def test_aggregate_not_masked_even_when_alias_is_sensitive(self) -> None:
        resp = _response(["salary"], [[55000.0]])
        out = _mask_sensitive_result_columns(
            resp, _PRIVACY, sql="SELECT AVG(salary) AS salary FROM e"
        )
        assert out.rows == [[55000.0]]

    def test_aggregate_alias_colliding_with_excluded_column_not_masked(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.sales.revenue"])
        resp = _response(["revenue"], [[9.0]])
        out = _mask_sensitive_result_columns(
            resp, privacy, sql="SELECT SUM(revenue) AS revenue FROM sales"
        )
        assert out.rows == [[9.0]]

    def test_plain_excluded_column_masked_as_backstop(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.users.internal_code"])
        resp = _response(["id", "internal_code"], [[1, "ABC"]])
        out = _mask_sensitive_result_columns(
            resp, privacy, sql="SELECT id, internal_code FROM users"
        )
        assert out.rows == [[1, "[REDACTED]"]]

    def test_select_star_falls_back_to_name_matching(self) -> None:
        resp = _response(["id", "email"], [[1, "a@x"]])
        out = _mask_sensitive_result_columns(resp, _PRIVACY, sql="SELECT * FROM users")
        assert out.rows == [[1, "[REDACTED]"]]

    def test_without_sql_falls_back_to_name_matching(self) -> None:
        resp = _response(["id", "email"], [[1, "a@x"]])
        out = _mask_sensitive_result_columns(resp, _PRIVACY)
        assert out.rows == [[1, "[REDACTED]"]]

    def test_original_sql_wins_over_row_filter_wrapped_sql(self) -> None:
        resp = _response(["avg_salary"], [[1.0]])
        wrapped = "SELECT * FROM (SELECT AVG(salary) AS avg_salary FROM e) AS _sift_q WHERE t = 1"
        out = _mask_sensitive_result_columns(
            resp, _PRIVACY, sql=wrapped, original_sql="SELECT AVG(salary) AS avg_salary FROM e"
        )
        assert out.rows == [[1.0]]

    def test_excluded_column_entry_may_be_quoted_or_spaced(self) -> None:
        privacy = PrivacySettings(excluded_columns=['"public"."users"."secret_code" '])
        resp = _response(["secret_code"], [["x"]])
        out = _mask_sensitive_result_columns(resp, privacy, sql="SELECT * FROM users")
        assert out.rows == [["[REDACTED]"]]


class TestScopedClassification:
    """A column keeps its sensitivity through a rename inside a CTE, a derived table, a
    set operation or a scalar subquery. Anything not understood is None (name matching)."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            # CTEs
            ("WITH x AS (SELECT email AS c FROM users) SELECT c FROM x", {"c"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT x.c FROM x", {"c"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT c AS d FROM x", {"d"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT * FROM x", {"c"}),
            ("WITH x AS (SELECT id, email AS c FROM users) SELECT x.* FROM x", {"c"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT c FROM x x2", {"c"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT x2.c FROM x AS x2", {"c"}),
            ("WITH x AS (SELECT email AS c FROM users) SELECT y.c FROM x y, t", {"c"}),
            (
                "WITH x AS (SELECT email AS c FROM users), y AS (SELECT c AS d FROM x) "
                "SELECT d FROM y",
                {"d"},
            ),
            ("WITH x AS (SELECT email AS c FROM users) SELECT COUNT(c) AS n FROM x", set()),
            (
                "WITH x AS (SELECT id, COUNT(email) AS n FROM users GROUP BY id) SELECT n FROM x",
                set(),
            ),
            # ``c`` is no column of ``x``: misread, not understood
            ("WITH x AS (SELECT id FROM users) SELECT c FROM x", None),
            (
                "WITH x AS (SELECT email AS c, id FROM users) "
                "SELECT x.c, COUNT(*) AS n FROM x GROUP BY x.c",
                {"c"},
            ),
            (
                "WITH x AS (SELECT email AS c FROM users) "
                "SELECT c FROM x WHERE id IN (SELECT id FROM t)",
                {"c"},
            ),
            (
                "with x as (select email as c from users) "
                "select c from x where c <> 'a' order by c limit 10",
                {"c"},
            ),
            (
                "WITH x AS (SELECT a.email AS c FROM users a JOIN orders o ON o.uid = a.id) "
                "SELECT c FROM x",
                {"c"},
            ),
            (
                "WITH x AS (SELECT id FROM users UNION SELECT email FROM other) SELECT id FROM x",
                {"id"},
            ),
            ("WITH x AS (SELECT email AS c FROM users) SELECT /* note */ c FROM x -- t", {"c"}),
            # derived tables
            ("SELECT s.x FROM (SELECT email AS x FROM u) s", {"x"}),
            ("SELECT x FROM (SELECT email AS x FROM u) AS s", {"x"}),
            ("SELECT * FROM (SELECT email AS x FROM u) s", {"x"}),
            ("SELECT s.x, t.id FROM (SELECT email AS x FROM u) s JOIN t ON t.id = s.id", {"x"}),
            ("SELECT t.id, s.x FROM t JOIN (SELECT id, email AS x FROM u) s ON t.id = s.id", {"x"}),
            ("SELECT a FROM (SELECT b AS a FROM (SELECT email AS b FROM u) i) o", {"a"}),
            # set operations
            ("SELECT id FROM a UNION SELECT email FROM b", {"id"}),
            ("SELECT id FROM a UNION ALL SELECT email FROM b", {"id"}),
            ("SELECT id, name FROM a INTERSECT SELECT id, email FROM b", {"name"}),
            ("SELECT id FROM a EXCEPT SELECT email FROM b", {"id"}),
            ("(SELECT id FROM a) UNION (SELECT email FROM b)", {"id"}),
            ("SELECT id FROM a UNION SELECT id FROM b", set()),
            (
                "WITH x AS (SELECT id FROM users) SELECT c FROM x union all select email from y",
                _ALL,
            ),
            (
                "WITH x AS (SELECT id AS c FROM users) "
                "SELECT c FROM x union all select email from y",
                {"c"},
            ),
            # scalar subqueries
            ("SELECT (SELECT email FROM u LIMIT 1) AS c, id FROM t", {"c"}),
            ("SELECT (SELECT COUNT(email) FROM u) AS n, id FROM t", set()),
            (
                "SELECT u.id, (SELECT c FROM x WHERE x.id = u.id) AS cc "
                "FROM u, (SELECT email AS c, id FROM users) x",
                {"cc"},
            ),
            # not understood -> None -> name matching (today's behaviour)
            # c is a real users column under its own name: name matching decides it
            ("WITH x AS (SELECT * FROM users) SELECT c FROM x", set()),
            # `*` over a scoped and a real source: the scoped column is still known
            ("SELECT * FROM (SELECT email AS x FROM u) s JOIN t ON 1=1", {"x"}),
            # branches of different widths: not understood, and the SQL names a sensitive
            # column
            ("SELECT id FROM a UNION SELECT id, email FROM b", _ALL),
            ("SELECT id FROM a UNION SELECT id, name FROM b", None),
            # unaffected
            ("SELECT x.c FROM x", set()),
            ("SELECT id FROM t WHERE email = 'a'", set()),
            ("SELECT t.id FROM t JOIN u ON t.email = u.email", set()),
        ],
    )
    def test_scoped(self, sql: str, expected: set[str] | None) -> None:
        assert _masked(sql) == expected

    def test_set_operation_exempt_only_when_every_branch_is(self) -> None:
        both = "SELECT SUM(salary) AS s FROM a UNION ALL SELECT SUM(salary) AS s FROM b"
        assert _classify_select_columns(both, _PRIVACY) == (set(), {"s"})
        mixed = "SELECT SUM(salary) AS s FROM a UNION ALL SELECT amount AS s FROM b"
        assert _classify_select_columns(mixed, _PRIVACY) == (set(), set())

    def test_cte_rename_is_masked_end_to_end(self) -> None:
        resp = _response(["c"], [["a@x.com"]])
        out = _mask_sensitive_result_columns(
            resp, _PRIVACY, sql="WITH x AS (SELECT email AS c FROM users) SELECT c FROM x"
        )
        assert out.rows == [["[REDACTED]"]]

    def test_union_branch_is_masked_end_to_end(self) -> None:
        resp = _response(["id"], [[1], ["a@x.com"]])
        out = _mask_sensitive_result_columns(
            resp, _PRIVACY, sql="SELECT id FROM a UNION ALL SELECT email FROM b"
        )
        assert out.rows == [["[REDACTED]"], ["[REDACTED]"]]


class TestFallbackFailsClosed:
    """A FROM shape the scoped resolver does not parse masks every column when the SQL names
    a sensitive column: which column carries the value is unknown. A shape it does parse
    keeps the precise answer."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT email AS contact FROM users JOIN t USING (id)", {"contact"}),
            ("SELECT email AS contact FROM users u JOIN t ON (u.id = t.id)", {"contact"}),
            (
                "SELECT email AS contact FROM users u JOIN t ON u.id = t.id "
                "AND (t.a = 1 OR t.b = 2)",
                {"contact"},
            ),
            (
                "SELECT email AS contact FROM users u JOIN t ON u.id = t.id AND t.k IN (1, 2)",
                {"contact"},
            ),
            (
                "SELECT email AS contact FROM users u JOIN t ON u.id = t.id AND t.k IS NOT NULL",
                {"contact"},
            ),
            (
                "SELECT email AS contact FROM users u JOIN t ON u.id = t.id "
                "JOIN v ON v.id BETWEEN 1 AND 2",
                {"contact"},
            ),
            ("SELECT email AS contact FROM users u JOIN t ON NOT t.flag", {"contact"}),
            ("SELECT email AS contact FROM users TABLESAMPLE SYSTEM (10)", {"contact"}),
            (
                "SELECT email AS contact FROM users STRAIGHT_JOIN t ON t.id = users.id",
                {"contact"},
            ),
            ("SELECT email AS contact FROM ONLY users", {"contact"}),
            ("WITH RECURSIVE r AS (SELECT 1) SELECT email AS contact FROM users", {"contact"}),
            ("WITH x AS MATERIALIZED (SELECT 1) SELECT email AS contact FROM users", {"contact"}),
            ("WITH x AS (SELECT * FROM users) SELECT email AS contact FROM x", {"contact"}),
            ("SELECT email AS contact FROM (SELECT * FROM users) s", {"contact"}),
            ("SELECT s.email AS contact FROM (SELECT * FROM users) s", {"contact"}),
            ("SELECT email AS contact FROM users UNION SELECT * FROM v", {"contact"}),
            # a parenthesised join group is read as its sources
            (
                "SELECT email AS contact FROM users u JOIN (t JOIN v ON v.id = t.id) "
                "ON u.id = t.id",
                {"contact"},
            ),
            ("SELECT id FROM users u JOIN (t JOIN v ON v.id = t.id) ON u.email = t.e", set()),
            ("SELECT to_jsonb(u) AS j FROM users u JOIN (t JOIN v ON true) ON true", {"j"}),
            ("SELECT id FROM users u JOIN (t JOIN v ON v.id = t.id) ON u.id = t.id", set()),
            # not understood: every column
            ("SELECT email AS contact FROM users u USE INDEX (ix) JOIN t ON t.id = u.id", _ALL),
            ("SELECT email AS contact FROM users PARTITION (p0)", _ALL),
            ("SELECT email AS contact FROM users FOR UPDATE", _ALL),
            ("SELECT email AS contact FROM users UNION SELECT a, b FROM v", _ALL),
            # a join group under an alias hides its sources: not understood
            ("SELECT j.id FROM (users u JOIN t ON true) AS j WHERE j.email = 'x'", _ALL),
            # a sensitive name anywhere is enough, and so is a record function
            ("SELECT g.id FROM (users u JOIN z ON true) AS g WHERE u.email IS NULL", _ALL),
            ("SELECT to_json(g) AS j FROM (users u JOIN z ON true) AS g", _ALL),
            ("SELECT g.v FROM (users u JOIN z ON true) AS g", None),
            # WITH ORDINALITY is followed
            (
                "SELECT id FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n) "
                "WHERE u.email IS NULL",
                set(),
            ),
            (
                "SELECT to_json(z) AS j FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n)",
                set(),  # a function over harmless arguments yields harmless rows
            ),
            ("SELECT z.v FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n)", set()),
        ],
    )
    def test_fallback(self, sql: str, expected: set[str] | str | None) -> None:
        assert _masked(sql) == expected

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH x AS (SELECT email AS c FROM users) SELECT c FROM x JOIN t USING (id)",
            "WITH x AS (SELECT email AS c FROM users) SELECT c FROM x JOIN t ON (t.id = x.id)",
            "WITH x AS (SELECT email AS c FROM users) "
            "SELECT c FROM x JOIN t ON x.id = t.id AND t.k IN (1, 2)",
            "WITH x AS (SELECT email AS c FROM users) "
            "SELECT c FROM x LEFT JOIN t ON x.id = t.id AND t.k IS NOT NULL",
        ],
    )
    def test_cte_column_through_join_conditions(self, sql: str) -> None:
        assert _masked(sql) == {"c"}

    def test_unaliased_inner_item_of_scalar_subquery(self) -> None:
        assert _masked("SELECT (SELECT lower(email) FROM u LIMIT 1) AS cc FROM t") == {"cc"}


class TestColumnAliasLists:
    """``alias(a, b)`` renames a source's columns by position."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT s.z FROM (SELECT email AS x FROM u) AS s (z)", {"z"}),
            ("SELECT z FROM (SELECT email AS x FROM u) s(z)", {"z"}),
            ("SELECT z FROM t, (SELECT email AS x FROM u) s(z)", {"z"}),
            ("SELECT z FROM t JOIN (SELECT email AS x FROM u) s(z) ON true", {"z"}),
            ("SELECT * FROM (SELECT email AS x, id FROM u) AS s(a, b)", {"a"}),
            ("SELECT s.b FROM (SELECT email AS x, id FROM u) AS s(a, b)", set()),
            ("WITH a AS (SELECT email AS c, 1 AS id FROM u) SELECT id FROM a AS b(id, k)", {"id"}),
            ("WITH a AS (SELECT email AS c, 1 AS id FROM u) SELECT id FROM a b(id, k)", {"id"}),
            ("WITH a AS (SELECT email AS c, 1 AS id FROM u) SELECT k FROM a AS b(id, k)", set()),
            (
                "WITH a AS (SELECT email AS c, 1 AS id FROM u) SELECT b.id FROM t, a b(id, k)",
                {"id"},
            ),
            # alias list shorter than the source: every column of the source is masked
            ("WITH a AS (SELECT 1 AS id, email AS c FROM u) SELECT z FROM a AS b(z)", {"z"}),
            ("SELECT z FROM (SELECT 1 AS id, email AS c FROM u) AS s(z)", {"z"}),
            ("SELECT c FROM (SELECT 1 AS id, email AS c FROM u) AS s(z)", {"c"}),
            # CTE column lists
            ("WITH x(c) AS (SELECT email FROM users) SELECT c FROM x", {"c"}),
            ("WITH x (c, d) AS (SELECT email, id FROM users) SELECT d FROM x", set()),
            ("WITH x (c, d) AS (SELECT email, id FROM users) SELECT * FROM x", {"c"}),
            ("WITH x(c) AS (SELECT lower(email) FROM users) SELECT c FROM x", {"c"}),
            # a column list over a source of unknown width fails closed
            ("WITH x(c) AS (SELECT * FROM users) SELECT c FROM x", {"c"}),
            # a real table or table function renamed by a column list
            ("SELECT a FROM users AS u(a, b)", {"a"}),
            ("SELECT n FROM generate_series(1, 3) AS g(n)", set()),
            ("SELECT z FROM users, unnest(emails) AS z", {"z"}),
        ],
    )
    def test_alias_list(self, sql: str, expected: set[str] | None) -> None:
        assert _masked(sql) == expected


class TestRecursiveCte:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("WITH RECURSIVE x AS (SELECT email AS c FROM users) SELECT c FROM x", {"c"}),
            (
                "WITH RECURSIVE r AS (SELECT id, 1 AS d FROM t UNION ALL "
                "SELECT r.id, r.d + 1 FROM r JOIN t ON t.parent = r.id) SELECT id, d FROM r",
                set(),
            ),
            # the recursive branch carries a sensitive value into a position the anchor fills
            # with a harmless one: masked once the self-reference is resolved
            (
                "WITH RECURSIVE r(a, b) AS (SELECT 1, email FROM u UNION ALL "
                "SELECT b, a FROM r) SELECT a FROM r",
                {"a"},
            ),
        ],
    )
    def test_recursive(self, sql: str, expected: set[str] | None) -> None:
        assert _masked(sql) == expected


class TestNestedQueriesInSelectItems:
    _CTE = "WITH x AS (SELECT email AS c, id FROM u) "

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("SELECT COALESCE((SELECT c FROM x LIMIT 1), '') AS cc FROM t", {"cc"}),
            ("SELECT (SELECT c FROM x LIMIT 1) || '' AS cc FROM t", {"cc"}),
            ("SELECT ARRAY(SELECT c FROM x) AS cc FROM t", {"cc"}),
            ("SELECT lower((SELECT c FROM x LIMIT 1)) AS cc FROM t", {"cc"}),
            ("SELECT CASE WHEN true THEN (SELECT c FROM x LIMIT 1) END AS cc FROM t", {"cc"}),
            ("SELECT COALESCE((SELECT id FROM x LIMIT 1), 0) AS cc FROM t", set()),
            ("SELECT (SELECT lower(c) FROM x LIMIT 1) AS cc FROM t", {"cc"}),
            ("SELECT (SELECT max(c) FROM x) AS cc FROM t", {"cc"}),
            ("SELECT (SELECT count(c) FROM x) AS cc FROM t", set()),
            ("SELECT z FROM x, LATERAL (SELECT c AS z) l", {"z"}),
        ],
    )
    def test_nested(self, body: str, expected: set[str] | None) -> None:
        assert _masked(self._CTE + body) == expected

    def test_correlated_reference_to_an_outer_derived_table(self) -> None:
        sql = "SELECT (SELECT s.x FROM t LIMIT 1) AS cc FROM (SELECT email AS x FROM u) s"
        assert _masked(sql) == {"cc"}

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            # inner query not understood (derived table without alias): masked when it
            # mentions a sensitive or masked column, otherwise today's None
            (
                "WITH x AS (SELECT email AS c FROM u) "
                "SELECT (SELECT c FROM x, (SELECT 1) LIMIT 1) AS cc FROM t",
                {"cc"},
            ),
            ("SELECT (SELECT email FROM u, (SELECT 1) LIMIT 1) AS cc FROM t", {"cc"}),
            ("SELECT (SELECT id FROM u, (SELECT 1) LIMIT 1) AS cc FROM t", None),
        ],
    )
    def test_unresolved_scalar_subquery(self, sql: str, expected: set[str] | None) -> None:
        assert _masked(sql) == expected


def _item_sql(expr: str) -> str:
    return f"SELECT id, {expr} AS v FROM t"  # noqa: S608 — test input, never executed


class TestAggregateExemption:
    """Only statistical aggregates exempt a sensitive argument; value-returning ones return
    a stored value and are masked."""

    @pytest.mark.parametrize(
        "expr",
        [
            "MIN(email)",
            "MAX(email)",
            "group_concat(email)",
            "array_agg(email)",
            "string_agg(email, ',')",
            "listagg(email, ',')",
            "median(salary)",
            "bit_and(salary)",
            "bit_or(salary)",
            "json_agg(email)",
            "jsonb_agg(email)",
            "json_arrayagg(email)",
            "json_objectagg('k', email)",
            "any_value(email)",
            "first(email)",
            "last(email)",
            "percentile_disc(0.5) WITHIN GROUP (ORDER BY salary)",
            "mode() WITHIN GROUP (ORDER BY email)",
            "MAX(salary) OVER (PARTITION BY dept)",
        ],
    )
    def test_value_aggregate_is_masked(self, expr: str) -> None:
        assert _classify_select_columns(_item_sql(expr), _PRIVACY) == (
            {"v"},
            set(),
        )

    @pytest.mark.parametrize(
        "expr",
        [
            "COUNT(email)",
            "count_if(email IS NULL)",
            "approx_count_distinct(email)",
            "SUM(salary)",
            "AVG(salary)",
            "stddev(salary)",
            "stddev_pop(salary)",
            "stddev_samp(salary)",
            "variance(salary)",
            "var_pop(salary)",
            "var_samp(salary)",
            "corr(salary, age)",
            "covar_pop(salary, age)",
            "covar_samp(salary, age)",
            "regr_slope(salary, age)",
            "regr_intercept(salary, age)",
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY salary)",
            "approx_percentile(salary, 0.5)",
            "bool_and(salary > 0)",
            "bool_or(salary > 0)",
            "every(salary > 0)",
        ],
    )
    def test_statistical_aggregate_is_exempt(self, expr: str) -> None:
        assert _classify_select_columns(_item_sql(expr), _PRIVACY) == (
            set(),
            {"v"},
        )

    def test_value_aggregate_over_plain_column_is_not_exempt(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.sales.revenue"])
        resp = _response(["revenue"], [[9.0]])
        out = _mask_sensitive_result_columns(
            resp, privacy, sql="SELECT MAX(revenue) AS revenue FROM sales"
        )
        assert out.rows == [["[REDACTED]"]]

    def test_value_aggregate_masked_in_the_fallback_scan(self) -> None:
        assert _masked("SELECT MAX(email) AS m FROM users TABLESAMPLE SYSTEM (10)") == {"m"}
        assert _masked("SELECT COUNT(email) AS n FROM users TABLESAMPLE SYSTEM (10)") == set()

    def test_unaliased_value_aggregate_in_a_later_set_operation_branch(self) -> None:
        # Later branches take their names from the first, so an unnamed item there is fine.
        sql = "SELECT SUM(salary) AS s FROM t UNION ALL SELECT MAX(salary) FROM t2"
        assert _classify_select_columns(sql, _PRIVACY) == ({"s"}, set())

    def test_value_aggregate_in_a_cte(self) -> None:
        sql = "WITH x AS (SELECT MAX(email) AS m FROM users) SELECT m FROM x"
        assert _masked(sql) == {"m"}

    def test_within_group_keeps_the_rest_of_the_select_list(self) -> None:
        sql = (
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) AS p, email AS e "
            "FROM t GROUP BY dept"
        )
        assert _classify_select_columns(sql, _PRIVACY) == ({"e"}, {"p"})

    def test_exempt_and_value_sets_are_disjoint(self) -> None:
        from app.services.result_masking import (
            _STATISTICAL_AGGREGATE_FUNCTIONS,
            _VALUE_AGGREGATE_FUNCTIONS,
        )

        assert not _STATISTICAL_AGGREGATE_FUNCTIONS & _VALUE_AGGREGATE_FUNCTIONS


_BIG_IN_LIST = ",".join(str(i) for i in range(10000))
_LONG_ALIAS = "e" * 70


class TestMaskEndToEnd:
    """What actually comes back ``[REDACTED]`` for a given SQL and driver column names."""

    @pytest.mark.parametrize(
        ("sql", "columns", "dq", "redacted"),
        [
            # MySQL single-quoted aliases
            ("SELECT email AS 'Email Address' FROM customers", ["Email Address"], True, {0}),
            (
                "SELECT CONCAT(first_name, ' ', last_name) AS 'Full Name', "
                "email AS 'Email Address' FROM customers",
                ["Full Name", "Email Address"],
                True,
                {1},
            ),
            ("SELECT c.phone AS 'Customer Phone' FROM customers c", ["Customer Phone"], True, {0}),
            ("SELECT email 'c' FROM users", ["c"], True, {0}),
            ("SELECT u.email AS 'contact', u.id FROM users u", ["contact", "id"], True, {0}),
            ("SELECT id, email 'c', name FROM users", ["id", "c", "name"], True, {1}),
            ("SELECT email AS 'it''s' FROM users", ["it's"], True, {0}),
            ("SELECT SUM(salary) AS 'Total Salary' FROM employees", ["Total Salary"], True, set()),
            # type-name and keyword aliases, with and without AS
            ("SELECT email AS number FROM users", ["number"], False, {0}),
            ("SELECT email AS date FROM users", ["date"], False, {0}),
            ("SELECT email AS text FROM users", ["text"], False, {0}),
            ("SELECT email AS timestamp FROM users", ["timestamp"], False, {0}),
            ("SELECT email AS desc FROM users", ["desc"], False, {0}),
            ("SELECT id, email AS interval FROM users", ["id", "interval"], False, {1}),
            ("SELECT email text FROM users", ["text"], False, {0}),
            ("SELECT email desc FROM users", ["desc"], False, {0}),
            ("SELECT email result FROM users", ["result"], False, {0}),
            ("SELECT id, email type FROM users", ["id", "type"], False, {1}),
            ("SELECT email key, id FROM users", ["key", "id"], False, {0}),
            ("SELECT email count FROM users", ["count"], False, {0}),
            ("SELECT email time, id FROM users", ["time", "id"], False, {0}),
            ("SELECT id, email date, name FROM users", ["id", "date", "name"], False, {1}),
            ("SELECT email IS NULL AS e FROM users", ["e"], False, {0}),
            # duplicate output names: masked by position
            ("SELECT a.email AS x, b.name AS x FROM a JOIN b ON true", ["x", "x"], False, {0}),
            # I1: a wildcard next to a scoped source keeps what is known
            (
                "SELECT * FROM users u JOIN (SELECT id AS uid, email AS e FROM contacts) s "
                "ON s.uid = u.id",
                ["id", "name", "uid", "e"],
                False,
                {3},
            ),
            (
                "SELECT u.*, s.e FROM users u JOIN (SELECT id AS uid, email AS e FROM contacts) s "
                "ON s.uid = u.id",
                ["id", "name", "e"],
                False,
                {2},
            ),
            (
                "WITH x AS (SELECT id, email AS c FROM users) "
                "SELECT * FROM x JOIN orders o ON o.uid = x.id",
                ["id", "c", "oid", "uid"],
                False,
                {1},
            ),
            ("SELECT *, email AS contact FROM users", ["id", "email", "contact"], False, {1, 2}),
            # I3: whole-row references
            ("SELECT row_to_json(u) AS j FROM users u", ["j"], False, {0}),
            ("SELECT json_agg(u) FROM users u", ["json_agg"], False, {0}),
            ("SELECT json_agg(u) AS j FROM users u", ["j"], False, {0}),
            ("SELECT to_jsonb(u.*) AS j FROM users u", ["j"], False, {0}),
            ("SELECT u FROM users u", ["u"], False, {0}),
            ("SELECT array_agg(u) AS a FROM users u", ["a"], False, {0}),
            ("SELECT hstore(u) AS h FROM users u", ["h"], False, {0}),
            ("SELECT row_to_json(users) AS j FROM users", ["j"], False, {0}),
            ("SELECT count(u.*) AS n FROM users u", ["n"], False, set()),
            ("SELECT row_to_json(s) AS j FROM (SELECT id FROM users) s", ["j"], False, set()),
            ("SELECT row_to_json(s) AS j FROM (SELECT email FROM users) s", ["j"], False, {0}),
            ("SELECT g FROM generate_series(1, 3) g", ["g"], False, set()),
            # an unaliased table name may be its column or the whole row on PostgreSQL;
            # the columns are unknown, so it is masked (fail closed). MySQL has no
            # whole-row reference.
            ("SELECT country FROM country", ["country"], False, {0}),
            ("SELECT country FROM country", ["country"], True, set()),
            ("SELECT c.country FROM country c", ["country"], False, set()),
            # DISTINCT ON
            (
                "SELECT DISTINCT ON (id) id, email AS contact FROM users",
                ["id", "contact"],
                False,
                {1},
            ),
            ("SELECT DISTINCT ON (id) email AS contact FROM users", ["contact"], False, {0}),
            ("SELECT DISTINCT ON (id, x) name, email AS c FROM users", ["name", "c"], False, {1}),
            # parse error (sqlparse token limit): fail closed when the SQL names something
            # sensitive, name matching otherwise
            (
                f"SELECT email AS e FROM t WHERE id IN ({_BIG_IN_LIST})",  # noqa: S608
                ["e"],
                False,
                {0},
            ),
            (
                f"SELECT id, email AS e FROM t WHERE id IN ({_BIG_IN_LIST})",  # noqa: S608
                ["id", "e"],
                False,
                {0, 1},
            ),
            (
                f"SELECT id AS e FROM t WHERE id IN ({_BIG_IN_LIST})",  # noqa: S608
                ["e"],
                False,
                set(),
            ),
            # a predicted name the driver did not return (PostgreSQL truncates identifiers to
            # 63 bytes): with the outer list fully classified, each column is judged on its
            # own position and the name is not consulted ...
            (
                f"SELECT email AS {_LONG_ALIAS}, id FROM users",  # noqa: S608
                [_LONG_ALIAS[:63], "id"],
                False,
                {0},
            ),
            # ... without positions (a wildcard) the wrong guess still masks everything
            (
                f"SELECT email AS {_LONG_ALIAS}, * FROM users",  # noqa: S608
                [_LONG_ALIAS[:63], "id", "name"],
                False,
                {0, 1, 2},
            ),
            # M1: a windowed statistical aggregate reveals values
            (
                "SELECT SUM(salary) OVER (PARTITION BY id) AS s FROM employees",
                ["s"],
                False,
                {0},
            ),
            ("SELECT AVG(salary) FILTER (WHERE id = 1) AS x FROM employees", ["x"], False, set()),
            ("SELECT COUNT(*) OVER () AS n, id FROM employees", ["n", "id"], False, set()),
            # an unaliased sensitive expression: the engine's default name is guessed; the
            # guess is not consulted when every column has a classified position
            ("SELECT lower(email) FROM users", ["lower"], False, {0}),
            ("SELECT id, lower(email) FROM users", ["id", "lower"], False, {1}),
            (
                "SELECT id, IFNULL(email, '') FROM users",
                ["id", "IFNULL(email, '')"],
                True,
                {1},
            ),
            ("SELECT (SELECT email FROM u LIMIT 1) FROM t", ["email"], False, {0}),
            # name matching still applies when the classifier answers
            ("SELECT id, name AS email FROM users", ["id", "email"], False, {1}),
        ],
    )
    def test_redacted_columns(
        self, sql: str, columns: list[str], dq: bool, redacted: set[int]
    ) -> None:
        resp = _response(columns, [[f"v{i}" for i in range(len(columns))]])
        out = _mask_sensitive_result_columns(
            resp, _PRIVACY, sql=sql, double_quote_is_string_literal=dq
        )
        got = {i for i, v in enumerate(out.rows[0]) if v == "[REDACTED]"}
        assert got == redacted

    def test_excluded_column_counts_as_sensitive_inside_the_classifier(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.users.internal_code"])
        assert _masked("SELECT internal_code AS c FROM users", privacy) == {"c"}
        sql = "WITH x AS (SELECT internal_code AS c FROM users) SELECT c FROM x"
        assert _masked(sql, privacy) == {"c"}
        resp = _response(["c"], [["ABC"]])
        out = _mask_sensitive_result_columns(
            resp, privacy, sql="SELECT internal_code AS c FROM users"
        )
        assert out.rows == [["[REDACTED]"]]


class TestResolveMaskIndices:
    def test_name_matching_applies_alongside_the_classifier(self) -> None:
        assert _resolve_mask_indices(["id", "salary"], set(), frozenset(), _PRIVACY, set()) == {1}

    def test_exempt_alias_is_not_name_matched(self) -> None:
        got = _resolve_mask_indices(["salary"], set(), frozenset(), _PRIVACY, {"salary"})
        assert got == set()

    def test_predicted_name_missing_from_columns_masks_everything(self) -> None:
        got = _resolve_mask_indices(
            ["Email Address", "id"], {"email"}, frozenset(), _PRIVACY, set()
        )
        assert got == {0, 1}

    def test_predicted_names_match_case_insensitively(self) -> None:
        got = _resolve_mask_indices(["Contact", "id"], {"contact"}, frozenset(), _PRIVACY, set())
        assert got == {0}

    def test_mask_all_sentinel(self) -> None:
        got = _resolve_mask_indices(["a", "b"], MASK_ALL_COLUMNS, frozenset(), _PRIVACY, set())
        assert got == {0, 1}
        assert "anything" in MASK_ALL_COLUMNS
        assert MASK_ALL_COLUMNS


class TestClassifierHardening:
    def test_distinct_on(self) -> None:
        sql = "SELECT DISTINCT ON (id) id, email AS contact FROM users"
        assert _classify_select_columns(sql, _PRIVACY) == ({"contact"}, set())

    def test_wildcard_next_to_scoped_source_is_partial(self) -> None:
        sql = (
            "SELECT * FROM users u JOIN (SELECT id AS uid, email AS e FROM contacts) s "
            "ON s.uid = u.id"
        )
        # uncertain: the known masked name, no exemptions, name matching for the rest
        assert _classify_select_columns(sql, _PRIVACY) == ({"e"}, set())

    def test_wildcard_with_named_masked_item(self) -> None:
        assert _classify_select_columns("SELECT *, email AS contact FROM users", _PRIVACY) == (
            {"contact"},
            set(),
        )

    def test_windowed_statistical_aggregate_is_not_exempt(self) -> None:
        sql = "SELECT SUM(salary) OVER (ORDER BY id) AS running FROM employees"
        assert _classify_select_columns(sql, _PRIVACY) == ({"running"}, set())

    def test_exception_fails_closed_when_sql_names_a_sensitive_column(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(result_masking, "_classify_output_items", boom)
        sql = "SELECT email AS e FROM users"
        with caplog.at_level("WARNING"):
            result = _classify_select_columns(sql, _PRIVACY)
        assert result is not None and result[0] is MASK_ALL_COLUMNS
        assert "users" not in caplog.text and str(len(sql)) in caplog.text
        # an unexpected error is not a parse failure: every column, whatever the SQL names
        result = _classify_select_columns("SELECT id FROM t", _PRIVACY)
        assert result is not None and result[0] is MASK_ALL_COLUMNS

    def test_exception_fails_closed_on_excluded_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(result_masking, "_classify_output_items", boom)
        privacy = PrivacySettings(excluded_columns=["public.users.internal_code"])
        result = _classify_select_columns("SELECT internal_code FROM users", privacy)
        assert result is not None and result[0] is MASK_ALL_COLUMNS


_EXCLUDED = PrivacySettings(excluded_columns=["public.users.internal_code"])


def _redacted(sql: str, columns: list[str], privacy: PrivacySettings = _PRIVACY, **kw) -> set[int]:
    resp = _response(columns, [[f"v{i}" for i in range(len(columns))]])
    out = _mask_sensitive_result_columns(resp, privacy, sql=sql, **kw)
    return {i for i, v in enumerate(out.rows[0]) if v == "[REDACTED]"}


class TestPositionalMask:
    """When the outer SELECT list is fully known and lines up with the result columns, each
    position is masked on its own classification — in addition to the name rules."""

    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            ("SELECT lower(email) FROM users", ["lower"], {0}),
            ("SELECT lower(name), lower(email) FROM users", ["lower", "lower"], {1}),
            ("SELECT name, lower(email), lower(name) FROM users", ["name", "lower", "lower"], {1}),
            ("SELECT COALESCE(email, '') FROM users", ["coalesce"], {0}),
            ("SELECT CASE WHEN x THEN email END FROM users", ["case"], {0}),
            ("SELECT id, CASE WHEN x THEN email END FROM users", ["id", "case"], {1}),
            ("SELECT (SELECT email FROM u LIMIT 1) FROM t", ["email"], {0}),
            ("WITH x AS (SELECT email AS c FROM u) SELECT lower(c) FROM x", ["lower"], {0}),
            # an exempt position spares only itself
            (
                "SELECT SUM(salary) AS salary, salary FROM t GROUP BY salary",
                ["salary", "salary"],
                {1},
            ),
            # a wildcard over a real table: no positions, the name rules alone
            ("SELECT * FROM users", ["id", "email"], {1}),
            ("SELECT *, lower(email) FROM users", ["id", "email", "lower"], {1, 2}),
            # counts differ: positions ignored, the name rules alone
            ("SELECT lower(name), lower(email) FROM users", ["lower"], {0}),
        ],
    )
    def test_positions(self, sql: str, columns: list[str], redacted: set[int]) -> None:
        assert _redacted(sql, columns) == redacted

    def test_excluded_column_next_to_an_exempt_alias_of_the_same_name(self) -> None:
        sql = "SELECT COUNT(*) AS internal_code, u.internal_code FROM users u"
        assert _redacted(sql, ["internal_code", "internal_code"], _EXCLUDED) == {1}

    def test_row_filter_wrap_keeps_positions(self) -> None:
        clean = "SELECT lower(name), lower(email) FROM users"
        wrapped = f"SELECT * FROM ({clean}) AS _sift_q WHERE tenant = 1"  # noqa: S608
        cols = ["lower", "lower"]
        assert _redacted(wrapped, cols, original_sql=clean) == {1}
        assert _redacted(wrapped, cols) == {1}

    def test_exempt_position_does_not_cancel_name_matching_elsewhere(self) -> None:
        got = _resolve_mask_indices(
            ["salary", "salary"], set(), frozenset(), _PRIVACY, {"salary"}, ("exempt", "plain")
        )
        assert got == {1}

    def test_positions_replace_the_name_check_and_its_absence_keeps_it(self) -> None:
        # The predicted name "zzz" is not among the columns. With positions the column at the
        # masked position is redacted and nothing else; without them the guess being wrong
        # masks everything.
        with_positions = _resolve_mask_indices(
            ["a", "b"], {"zzz"}, frozenset(), _PRIVACY, set(), ("plain", "masked")
        )
        assert with_positions == {1}
        without = _resolve_mask_indices(["a", "b"], {"zzz"}, frozenset(), _PRIVACY, set(), None)
        assert without == {0, 1}

    def test_return_shape_unchanged(self) -> None:
        sql = "SELECT lower(name), lower(email) FROM users"
        assert _classify_select_columns(sql, _PRIVACY) == ({"lower"}, set())


class TestResultMaskPlan:
    """Computed once per query; applied to every streamed batch."""

    def test_no_privacy_means_no_plan(self) -> None:
        assert _plan_result_mask(None, "SELECT email FROM users") is None

    def test_redacts_batches_by_position(self) -> None:
        plan = _plan_result_mask(_PRIVACY, "SELECT lower(name), lower(email) FROM users")
        assert plan is not None
        cols = ["lower", "lower"]
        assert plan.redacted_indices(cols) == {1}
        assert plan.redact([["ann", "a@x"], ["bob", "b@x"]], cols) == [
            ["ann", "[REDACTED]"],
            ["bob", "[REDACTED]"],
        ]

    def test_rows_untouched_when_nothing_is_redacted(self) -> None:
        plan = _plan_result_mask(_PRIVACY, "SELECT id FROM t")
        assert plan is not None
        rows = [[1], [2]]
        assert plan.redact(rows, ["id"]) is rows

    def test_original_sql_preferred(self) -> None:
        clean = "SELECT AVG(salary) AS avg_salary FROM e"
        wrapped = f"SELECT * FROM ({clean}) AS _sift_q WHERE t = 1"  # noqa: S608
        plan = _plan_result_mask(_PRIVACY, wrapped, clean)
        assert plan is not None and plan.redacted_indices(["avg_salary"]) == set()

    def test_mask_all_sentinel_carries_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(result_masking, "_classify_output_items", boom)
        plan = _plan_result_mask(_PRIVACY, "SELECT email AS e, id FROM users")
        assert plan is not None and plan.redacted_indices(["e", "id"]) == {0, 1}


class TestMaskAfterRowFilterRewrite:
    """The row-filter rewrite regenerates the statement with sqlglot, which renames an
    unaliased function column (``strpos`` -> ``position``, ``now`` -> ``current_timestamp``).
    The plan is built from the clean SQL; the driver returns sqlglot's names; the mask must
    still land on the right column and nowhere else."""

    _FILTERS: ClassVar[dict[str, str]] = {"public.users": "tenant_id = 7"}

    def _rewritten(self, clean: str, dialect: str = "PostgreSQL") -> str:
        from app.services.row_filter_rewrite import apply_row_filters

        out = apply_row_filters(clean, self._FILTERS, dialect)
        assert "WHERE (tenant_id = 7)" in out
        return out

    @pytest.mark.parametrize(
        ("clean", "driver_columns", "redacted"),
        [
            ("SELECT name, strpos(email, '@') FROM users", ["name", "position"], {1}),
            ("SELECT name, char_length(email) FROM users", ["name", "length"], {1}),
            ("SELECT name, now() FROM users", ["name", "current_timestamp"], set()),
            ("SELECT name, COUNT(email) FROM users GROUP BY name", ["name", "count"], set()),
            (
                "SELECT u.name, date_part('year', u.created_at), lower(u.email)"
                " FROM users u JOIN orders o ON o.user_id = u.id",
                ["name", "extract", "lower"],
                {2},
            ),
        ],
    )
    def test_renamed_function_columns_are_judged_by_position(
        self, clean: str, driver_columns: list[str], redacted: set[int]
    ) -> None:
        rewritten = self._rewritten(clean)
        assert _redacted(rewritten, driver_columns, original_sql=clean) == redacted

    def test_mysql_ifnull_becomes_coalesce(self) -> None:
        clean = "SELECT name, IFNULL(email, '') FROM users"
        rewritten = self._rewritten(clean, "MySQL")
        columns = ["name", "COALESCE(email, '')"]
        redacted = _redacted(
            rewritten, columns, original_sql=clean, double_quote_is_string_literal=True
        )
        assert redacted == {1}

    def test_a_wildcard_still_relies_on_the_names(self) -> None:
        clean = "SELECT * FROM users"
        rewritten = self._rewritten(clean)
        assert _redacted(rewritten, ["id", "name", "email"], original_sql=clean) == {2}


class TestClassifierLexesLikeTheEngine:
    """Three shapes the classifier used to misread, each returning a sensitive value
    unmasked (found in the row-filter phase 2 review)."""

    # PostgreSQL: a backslash is an ordinary character in a standard string literal.
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            (
                "SELECT replace(path, '\\', '/') AS p, email AS contact,"
                " replace(home, '\\', '/') AS h FROM users",
                ["p", "contact", "h"],
                {1},
            ),
            ("SELECT 'C:\\' AS root, email FROM users", ["root", "email"], {1}),
            ("SELECT '\\' AS a, lower(email) AS b, '\\' AS c FROM users", ["a", "b", "c"], {1}),
            # an escape string keeps its backslash escapes
            ("SELECT E'it\\'s' AS q, email AS e FROM users", ["q", "e"], {1}),
            # a doubled quote is still one quote
            ("SELECT 'it''s' AS q, email AS e FROM users", ["q", "e"], {1}),
            # a backslash inside a comment or a dollar-quoted string changes nothing
            ("SELECT $$\\$$ AS d, email AS e -- '\\'\nFROM users", ["d", "e"], {1}),
        ],
    )
    def test_postgres_backslash_strings(
        self, sql: str, columns: list[str], redacted: set[int]
    ) -> None:
        assert _redacted(sql, columns) == redacted

    def test_mysql_keeps_backslash_escapes(self) -> None:
        # MySQL (double quotes are strings there) reads \' as an escaped quote, like sqlparse:
        # one literal "x', email AS e, '" and no email column.
        sql = "SELECT 'x\\', email AS e, ' AS s FROM users"
        assert _redacted(sql, ["s"], double_quote_is_string_literal=True) == set()

    def test_postgres_string_the_tokenizer_cannot_read_fails_closed(self) -> None:
        sql = "SELECT 'unterminated, email FROM users"
        assert _redacted(sql, ["a", "b"]) == {0, 1}

    # A statistical aggregate is exempt only when it is the whole item.
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            (
                "SELECT dept, sum(1) FILTER (WHERE active) || ':' || string_agg(email, ',') AS s"
                " FROM employees GROUP BY dept",
                ["dept", "s"],
                {1},
            ),
            (
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) || email AS s"
                " FROM employees",
                ["s"],
                {0},
            ),
            # the aggregate alone, with or without the clause, stays exempt
            (
                "SELECT dept, count(email) FILTER (WHERE active) AS n FROM employees GROUP BY dept",
                ["dept", "n"],
                set(),
            ),
            (
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) AS p FROM employees",
                ["p"],
                set(),
            ),
        ],
    )
    def test_aggregate_with_filter_or_within_group(
        self, sql: str, columns: list[str], redacted: set[int]
    ) -> None:
        assert _redacted(sql, columns) == redacted

    # Two sources whose names fold to the same key never hide each other.
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            (
                'WITH "U" AS (SELECT email AS c FROM users), u AS (SELECT 1 AS z)'
                ' SELECT * FROM "U"',
                ["c"],
                {0},
            ),
            (
                'WITH u AS (SELECT 1 AS z), "U" AS (SELECT email AS c FROM users)'
                ' SELECT c FROM "U"',
                ["c"],
                {0},
            ),
            (
                'SELECT "U".c FROM (SELECT email AS c FROM users) "U", (SELECT 1 AS c) u',
                ["c"],
                {0},
            ),
        ],
    )
    def test_names_that_fold_together(
        self, sql: str, columns: list[str], redacted: set[int]
    ) -> None:
        assert _redacted(sql, columns) == redacted


class TestEngineLexing:
    """Strings, quoted identifiers and comments end where the engine ends them."""

    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            ("SELECT N'\\' AS a, email AS e, N'\\' AS c FROM users", ["a", "e", "c"], {1}),
            ("SELECT U&'\\\\' AS a, email AS e, U&'\\\\' AS c FROM users", ["a", "e", "c"], {1}),
            (
                "SELECT regexp_replace(path, E'\\\\', '/') AS p, email AS contact,"
                " regexp_replace(home, E'\\\\', '/') AS h FROM users",
                ["p", "contact", "h"],
                {1},
            ),
            ("SELECT 5 # 3 AS a, email AS e FROM users", ["a", "e"], {1}),
            ("SELECT meta #>> '{a}' AS j, email AS e FROM users", ["j", "e"], {1}),
            (
                "WITH t AS (SELECT email AS c FROM users)"
                ' SELECT 1 AS "x\\", c AS k, 2 AS "y\\" FROM t',
                ["x\\", "k", "y\\"],
                {1},
            ),
            (
                "SELECT 1 AS a /* /* */ ' */, email AS k, 2 AS b -- '\n FROM users",
                ["a", "k", "b"],
                {1},
            ),
            ("SELECT date '\\' AS d, email AS e FROM users", ["d", "e"], {1}),
        ],
    )
    def test_postgres(self, sql: str, columns: list[str], redacted: set[int]) -> None:
        assert _redacted(sql, columns) == redacted

    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            (
                "SELECT SUBSTRING_INDEX(path, '\\\\', -1) AS file, email AS contact,"
                " SUBSTRING_INDEX(path, '\\\\', 1) AS drive FROM users",
                ["file", "contact", "drive"],
                {1},
            ),
            (
                "SELECT CONCAT(dir, '\\\\') AS d, email AS contact, CONCAT(dir, '\\\\') AS d2"
                " FROM users",
                ["d", "contact", "d2"],
                {1},
            ),
            ("SELECT 1 AS a #'\n, email AS e #'\n FROM users", ["a", "e"], {1}),
            # MySQL runs the content of an executable comment: fail closed.
            ("SELECT 1 AS a, /*! email AS e, */ 2 AS b FROM users", ["a", "e", "b"], {0, 1, 2}),
            ("SELECT 1 AS a, /*M! email AS e, */ 2 AS b FROM users", ["a", "e", "b"], {0, 1, 2}),
            # \' is an escaped quote on MySQL: one literal, no email column.
            ("SELECT 'x\\', email AS e, ' AS s FROM users", ["s"], set()),
            # a string alias still names the column
            ("SELECT email 'Contact' FROM users", ["Contact"], {0}),
        ],
    )
    def test_mysql(self, sql: str, columns: list[str], redacted: set[int]) -> None:
        assert _redacted(sql, columns, double_quote_is_string_literal=True) == redacted

    def test_an_unreadable_statement_fails_closed(self) -> None:
        assert _redacted("SELECT 'open, email FROM users", ["a", "b"]) == {0, 1}


class TestScopeCollisions:
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            # a set operation with opaque columns no longer gives up
            (
                'WITH "U" AS (SELECT email AS c FROM users), u AS (SELECT 1 AS z)'
                " SELECT * FROM \"U\" UNION ALL SELECT 'x'",
                ["c"],
                {0},
            ),
            ("SELECT * FROM users u(k) UNION ALL SELECT 'a'", ["k"], {0}),
            (
                "WITH x(k) AS (SELECT email FROM (users JOIN t ON true))"
                " SELECT * FROM x UNION ALL SELECT 'a'",
                ["k"],
                {0},
            ),
            (
                'WITH "U" AS (SELECT email AS c FROM users), u AS (SELECT 1 AS z)'
                " SELECT * FROM (SELECT * FROM \"U\" UNION ALL SELECT 'x') s",
                ["c"],
                {0},
            ),
            # a FROM-scoped source and a real table under one folded name
            ('SELECT row_to_json("U") AS j FROM users "U", (SELECT 1 AS c) u', ["j"], {0}),
            ('SELECT row_to_json(t) AS j FROM users t, (SELECT 1 AS c) "T"', ["j"], {0}),
            # a CTE and a real table under one folded name
            ('WITH u AS (SELECT 1 AS z) SELECT row_to_json(x) AS j FROM "U" x', ["j"], {0}),
            # an inner CTE that differs from an outer one only by case
            (
                "WITH u AS (SELECT email AS c FROM users)"
                ' SELECT c FROM (WITH "U" AS (SELECT 1 AS c) SELECT c FROM u) t',
                ["c"],
                {0},
            ),
            # a derived-table alias is not a CTE inside a nested subquery
            (
                "WITH u AS (SELECT email AS c FROM users)"
                " SELECT (SELECT c FROM u LIMIT 1) AS x FROM (SELECT 1 AS c) u",
                ["x"],
                {0},
            ),
        ],
    )
    def test_fail_closed(self, sql: str, columns: list[str], redacted: set[int]) -> None:
        assert _redacted(sql, columns) == redacted

    def test_ordinary_shadowing_still_resolves(self) -> None:
        sql = (
            "WITH u AS (SELECT email AS c FROM users)"
            " SELECT c FROM (WITH u AS (SELECT 1 AS c) SELECT c FROM u) t"
        )
        # The inner u shadows the outer one; merging on shadow over-masks, which is the
        # accepted, fail-closed cost.
        assert _redacted(sql, ["c"]) in ({0}, set())


class TestStatisticCasts:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT AVG(salary)::numeric(10,2) AS a FROM e",
            "SELECT avg(salary)::int FROM e",
            "SELECT COUNT(DISTINCT email)::text AS n FROM users",
            "SELECT AVG(salary)::double precision AS a FROM e",
            "SELECT AVG(salary)::numeric[] AS a FROM e",
            "SELECT CAST(SUM(salary) AS int) AS s FROM e",
            "SELECT CAST(AVG(salary) AS numeric(12, 2)) AS a FROM e",
            "SELECT ROUND(AVG(salary), 2) AS a FROM e",
            "SELECT ROUND(AVG(salary)::numeric, 2) AS a FROM e",
            "SELECT ROUND(AVG(salary)) AS a FROM e",
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary)::numeric AS p FROM e",
            "SELECT count(email) FILTER (WHERE active) n FROM users",
            "SELECT sum(salary) FILTER (WHERE active) total FROM e",
        ],
    )
    def test_exempt(self, sql: str) -> None:
        assert _redacted(sql, ["x"]) == set()

    def test_mysql_cast_is_exempt(self) -> None:
        sql = "SELECT CAST(AVG(salary) AS DECIMAL(10,2)) AS a FROM e"
        assert _redacted(sql, ["a"], double_quote_is_string_literal=True) == set()

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT AVG(salary)::numeric || email AS a FROM e",
            "SELECT ROUND(AVG(salary), 2) + salary AS a FROM e",
            "SELECT ROUND(salary, 2) AS a FROM e",
            "SELECT ROUND(AVG(salary), digits) AS a FROM e",
            "SELECT CAST(SUM(salary) OVER () AS int) AS s FROM e",
            "SELECT SUM(salary) OVER ()::int AS s FROM e",
            "SELECT AVG(salary)::numeric AT TIME ZONE 'UTC' AS a FROM e",
            "SELECT COALESCE(SUM(salary), 0) AS s FROM e",
            "SELECT CAST(MAX(salary) AS int) AS s FROM e",
            "SELECT ROUND(MAX(salary), 2) AS s FROM e",
        ],
    )
    def test_still_masked(self, sql: str) -> None:
        assert _redacted(sql, ["x"]) == {0}


class TestSetOperationsFailClosed:
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            # a masked position whose first-branch item has no name is masked by position
            ("SELECT 'x' UNION ALL SELECT email FROM users", ["?column?"], {0}),
            ("SELECT 'x' UNION ALL SELECT * FROM users u(k)", ["?column?"], {0}),
            # a later branch's unknown columns arrive under the first branch's names
            ("SELECT id, name FROM t UNION ALL SELECT * FROM users", ["id", "name"], {0, 1}),
            ("SELECT id, name FROM t UNION SELECT u.* FROM users u", ["id", "name"], {0, 1}),
            # a wildcard in the first branch names the columns itself: name matching works
            ("SELECT * FROM users UNION ALL SELECT id, name FROM t", ["id", "email"], {1}),
            # a wildcard over a CTE with known columns keeps its precise classification
            (
                "WITH c AS (SELECT id, name FROM t)"
                " SELECT id, name FROM t UNION ALL SELECT * FROM c",
                ["id", "name"],
                set(),
            ),
        ],
    )
    def test_set_operation(self, sql: str, columns: list[str], redacted: set[int]) -> None:
        assert _redacted(sql, columns) == redacted


_PAY_EXCLUDED = PrivacySettings(excluded_columns=["hr.pay.pay#rate", "hr.pay.pay\\rate"])


class TestTask4dReview:
    # K1: a first branch of unknown width; a sensitive column in a later branch. Masking
    # every column is acceptable as well.
    @pytest.mark.parametrize(
        ("sql", "columns", "redacted", "dq"),
        [
            (
                "SELECT * FROM orders UNION ALL SELECT email, 1 FROM users",
                ["id", "status"],
                {0},
                False,
            ),
            (
                "SELECT * FROM orders UNION ALL SELECT 1, email FROM users",
                ["id", "status"],
                {1},
                False,
            ),
            (
                "SELECT o.* FROM orders o UNION ALL SELECT lower(email), 'x' FROM users",
                ["id", "status"],
                {0},
                False,
            ),
            (
                "SELECT * FROM orders INTERSECT SELECT email, 1 FROM users",
                ["id", "status"],
                {0},
                False,
            ),
            (
                "SELECT * FROM orders UNION ALL SELECT email, 1 FROM users",
                ["id", "status"],
                {0},
                True,
            ),
            (
                "SELECT * FROM (SELECT * FROM orders UNION ALL SELECT email, 1 FROM users) s",
                ["id", "status"],
                {0},
                False,
            ),
            (
                "WITH s AS (SELECT * FROM orders UNION ALL SELECT email, 1 FROM users)"
                " SELECT * FROM s",
                ["id", "status"],
                {0},
                False,
            ),
        ],
    )
    def test_unknown_width_first_branch(
        self, sql: str, columns: list[str], redacted: set[int], dq: bool
    ) -> None:
        out = _redacted(sql, columns, double_quote_is_string_literal=dq)
        assert out in (redacted, set(range(len(columns))))

    # K2: an escape string continued on the next line stays an escape string: fail closed.
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT E'a'\n'\\'' AS a, email AS e, '' AS c -- '\nFROM users",
            "SELECT E'x' -- note\n'\\'' AS a, email AS e, '' AS c -- '\nFROM users",
        ],
    )
    def test_escape_string_continuation(self, sql: str) -> None:
        assert _redacted(sql, ["a", "e", "c"]) in ({1}, {0, 1, 2})

    # K3: MySQL reads ``--`` without a following space as two minus signs.
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 2--1 AS a, email AS e FROM users",
            "SELECT price--discount AS a, email AS e FROM orders",
        ],
    )
    def test_mysql_double_minus(self, sql: str) -> None:
        assert _redacted(sql, ["a", "e"], double_quote_is_string_literal=True) == {1}

    # I1: rewriting a token for sqlparse never renames an identifier.
    def test_mysql_identifier_with_hash_keeps_its_name(self) -> None:
        sql = "SELECT `pay#rate` AS x FROM pay"
        assert _redacted(sql, ["x"], _PAY_EXCLUDED, double_quote_is_string_literal=True) == {0}

    def test_postgres_identifier_with_backslash_keeps_its_name(self) -> None:
        sql = 'SELECT "pay\\rate" AS x FROM pay'
        assert _redacted(sql, ["x"], _PAY_EXCLUDED) == {0}

    # A qualified aggregate may be a user-defined function returning stored values.
    @pytest.mark.parametrize(
        ("sql", "redacted"),
        [
            ("SELECT x.avg(email) AS a FROM users", {0}),
            ("SELECT myschema.sum(salary) AS s FROM e", {0}),
            ("SELECT pg_catalog.avg(salary) AS a FROM e", set()),
        ],
    )
    def test_qualified_aggregate(self, sql: str, redacted: set[int]) -> None:
        assert _redacted(sql, ["x"]) == redacted


class TestBackslashIsEscapeReader:
    """Read fail-safe: anything but a literal True reads False (on MySQL, a refusal)."""

    @pytest.mark.parametrize(
        "adapter",
        [
            object(),
            MagicMock(),
            SimpleNamespace(backslash_is_escape=1),
            SimpleNamespace(backslash_is_escape="true"),
            SimpleNamespace(backslash_is_escape=None),
            SimpleNamespace(backslash_is_escape=False),
        ],
        ids=["missing", "magicmock", "one", "string", "none", "false"],
    )
    def test_anything_but_true_reads_false(self, adapter: object) -> None:
        assert _backslash_is_escape(adapter) is False

    def test_a_literal_true_reads_true(self) -> None:
        assert _backslash_is_escape(SimpleNamespace(backslash_is_escape=True)) is True


class TestSqlMentionsSensitive:
    """``sql_mentions_sensitive`` decides fail-closed outcomes (mask every column, bypass
    the cache), so its word scan must see every spelling an engine accepts as a name."""

    _GREEK = PrivacySettings(sensitive_column_patterns=["ηλεκτρονικό"], sensitive_columns=["όνομα"])

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT όνομα FROM πελάτες",
            "SELECT ΌΝΟΜΑ FROM πελάτες",
            'SELECT "όνομα" FROM πελάτες',
            "SELECT `όνομα` FROM πελάτες",
            "SELECT ηλεκτρονικό_ταχυδρομείο FROM πελάτες",
        ],
    )
    def test_a_non_ascii_name_is_a_word(self, sql: str) -> None:
        assert result_masking.sql_mentions_sensitive(sql, self._GREEK)

    def test_a_name_that_starts_with_a_digit_is_a_word(self) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=["1email"])
        assert result_masking.sql_mentions_sensitive("SELECT 1email FROM t", privacy)

    def test_an_unrelated_query_does_not_mention(self) -> None:
        assert not result_masking.sql_mentions_sensitive("SELECT id FROM πελάτες", self._GREEK)

    def test_a_failed_classification_on_a_non_ascii_name_masks_every_column(self) -> None:
        with patch.object(result_masking, "_lex_like_the_engine", side_effect=ValueError("x")):
            result = _classify_select_columns("SELECT όνομα FROM πελάτες", self._GREEK)
        assert result is not None and result[0] is MASK_ALL_COLUMNS


class TestRecordFunctionsOnARow:
    """PostgreSQL attribute notation: ``u.to_jsonb`` is ``to_jsonb(u)``, every column of
    ``u``. The item is masked under its output name whatever the source holds."""

    @pytest.mark.parametrize(
        ("sql", "name"),
        [
            ("SELECT u.to_jsonb FROM users u", "to_jsonb"),
            ("SELECT u.to_jsonb AS j FROM users u", "j"),
            ("SELECT users.row_to_json FROM users", "row_to_json"),
            ("SELECT u.concat FROM users u", "concat"),
            ("SELECT id, u.quote_literal AS f FROM users u", "f"),
            ("SELECT d.to_json FROM (SELECT id FROM users) d", "to_json"),
            (
                "WITH c AS (SELECT id FROM users) SELECT c.jsonb_build_array FROM c",
                "jsonb_build_array",
            ),
            ("SELECT u.json_agg FROM users u", "json_agg"),
            ("SELECT u.array_agg AS a FROM users u", "a"),
            ("SELECT u.any_value FROM users u GROUP BY u.id", "any_value"),
        ],
    )
    def test_a_record_function_in_attribute_notation_is_masked(self, sql: str, name: str) -> None:
        assert _masked(sql, _PRIVACY) == {name}

    def test_a_qualifier_that_is_no_source_is_left_to_the_name_rules(self) -> None:
        assert _masked("SELECT x.concat FROM users u", _PRIVACY) == set()

    def test_a_call_is_not_attribute_notation(self) -> None:
        assert _masked("SELECT concat(id, status) AS c FROM users u", _PRIVACY) == set()

    def test_a_schema_qualified_call_is_not_attribute_notation(self) -> None:
        sql = "SELECT public.concat(id, ' ', status) AS c FROM public.users"
        assert _masked(sql, _PRIVACY) == set()

    def test_a_dropped_name_is_a_plain_column(self) -> None:
        assert _masked("SELECT u.num_nulls FROM users u", _PRIVACY) == set()

    def test_the_rows_are_redacted(self) -> None:
        response = _response(["to_jsonb"], [['{"id": 1, "email": "a@x.io"}']])
        masked = _mask_sensitive_result_columns(
            response, _PRIVACY, sql="SELECT u.to_jsonb FROM users u"
        )
        assert masked.rows == [["[REDACTED]"]]


class TestCteAndFromClauseShapes:
    """``[NOT] MATERIALIZED``, ``TABLESAMPLE`` and an unaliased table function are read, so a
    sensitive value they carry stays masked; a shape still not understood masks every column
    when the SQL names a sensitive column."""

    @pytest.mark.parametrize(
        ("sql", "columns", "redacted"),
        [
            (
                "WITH x AS MATERIALIZED (SELECT email AS c FROM users) SELECT c FROM x",
                ["c"],
                {0},
            ),
            (
                "WITH x AS NOT MATERIALIZED (SELECT email AS c FROM users) SELECT c FROM x",
                ["c"],
                {0},
            ),
            ("WITH x(c) AS MATERIALIZED (SELECT email FROM users) SELECT c FROM x", ["c"], {0}),
            (
                "WITH y AS (SELECT 1 AS k), x (c, k) AS NOT MATERIALIZED "
                "(SELECT email, 1 FROM users) SELECT c, k FROM x",
                ["c", "k"],
                {0},
            ),
            ("SELECT a FROM users TABLESAMPLE SYSTEM (10) AS u(a, b)", ["a"], {0}),
            ("SELECT a FROM users AS u(a, b) TABLESAMPLE SYSTEM (10)", ["a"], {0}),
            (
                "SELECT a FROM users AS u(a, b) TABLESAMPLE BERNOULLI (5) REPEATABLE (1) "
                "WHERE a IS NOT NULL",
                ["a"],
                {0},
            ),
            ("SELECT unnest FROM users, unnest(emails)", ["unnest"], {0}),
            # any unqualified name may be a column of a masked table function
            ("SELECT id, unnest FROM users, unnest(emails)", ["id", "unnest"], {0, 1}),
            ("SELECT users.id, unnest FROM users, unnest(emails)", ["id", "unnest"], {1}),
            # a parenthesised join group is read as its sources
            (
                "WITH x AS (SELECT id, email AS c FROM users) "
                "SELECT c, t.id FROM x JOIN (t JOIN v ON v.id = t.id) ON x.id = t.id",
                ["c", "id"],
                {0},
            ),
            (
                "WITH x AS (SELECT id, email AS c FROM users) "
                "SELECT t.id, c FROM t JOIN ((v JOIN x ON x.id = v.id) JOIN w ON true) ON true",
                ["id", "c"],
                {1},
            ),
            ("SELECT u FROM (users u JOIN t ON t.id = u.id)", ["u"], {0}),
            # under an alias the group hides its sources: not understood, every column
            (
                "WITH x AS (SELECT id, email AS c FROM users) "
                "SELECT j.c, j.id FROM (x JOIN t ON true) AS j",
                ["c", "id"],
                {0, 1},
            ),
        ],
    )
    def test_a_sensitive_value_is_masked(
        self, sql: str, columns: list[str], redacted: set[int]
    ) -> None:
        assert _redacted(sql, columns) == redacted

    @pytest.mark.parametrize(
        ("sql", "columns"),
        [
            ("WITH x AS MATERIALIZED (SELECT id AS c FROM users) SELECT c FROM x", ["c"]),
            ("WITH x(c) AS NOT MATERIALIZED (SELECT id FROM users) SELECT c FROM x", ["c"]),
            ("SELECT id, name FROM users TABLESAMPLE SYSTEM (10)", ["id", "name"]),
            ("SELECT u.id FROM users u TABLESAMPLE SYSTEM (10) REPEATABLE (7)", ["id"]),
            (
                "SELECT generate_series FROM users, generate_series(1, 3)",
                ["generate_series"],
            ),
            (
                "SELECT t.id FROM users u JOIN (t JOIN v ON v.id = t.id) ON u.id = t.id",
                ["id"],
            ),
        ],
    )
    def test_a_harmless_query_of_the_same_shape_is_not_masked(
        self, sql: str, columns: list[str]
    ) -> None:
        assert _redacted(sql, columns) == set()

    def test_materialized_keeps_the_classification_precise(self) -> None:
        sql = "WITH x AS MATERIALIZED (SELECT id, email AS c FROM users) SELECT id, c FROM x"
        assert _masked(sql) == {"c"}


class TestPositionCountMismatchFailsClosed:
    """Positions that do not line up with the driver's columns are a misread: every column is
    masked when the SQL names a sensitive column or a position is masked."""

    @staticmethod
    def _plan_with(
        monkeypatch: pytest.MonkeyPatch, sql: str, positions: tuple[str, ...]
    ) -> result_masking._ResultMaskPlan:
        monkeypatch.setattr(
            result_masking, "_classify_result", lambda *_a: (set(), set(), positions)
        )
        plan = _plan_result_mask(_PRIVACY, sql)
        assert plan is not None
        return plan

    def test_a_misread_that_names_a_sensitive_column_masks_every_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = self._plan_with(monkeypatch, "SELECT email AS c, id FROM users", ("plain",))
        assert plan.redacted_indices(["c", "id"]) == {0, 1}

    def test_a_misread_without_a_sensitive_name_keeps_name_matching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = self._plan_with(monkeypatch, "SELECT name AS c, id FROM users", ("plain",))
        assert plan.redacted_indices(["c", "id"]) == set()

    def test_a_masked_position_masks_every_column_on_a_mismatch(self) -> None:
        got = _resolve_mask_indices(
            ["a", "b"], set(), frozenset(), _PRIVACY, set(), ("masked",), mismatch_masks_all=True
        )
        assert got == {0, 1}

    def test_a_using_join_over_ctes_that_carry_a_sensitive_column_is_fully_masked(self) -> None:
        # the documented cost: USING merges the join column, so the counts differ
        sql = (
            "WITH a AS (SELECT 1 AS id, email AS e FROM users), b AS (SELECT 1 AS id, 2 AS n) "
            "SELECT * FROM a JOIN b USING (id)"
        )
        assert _redacted(sql, ["id", "e", "n"]) == {0, 1, 2}

    def test_a_harmless_using_join_over_ctes_is_not_masked(self) -> None:
        sql = (
            "WITH a AS (SELECT 1 AS id, 2 AS m), b AS (SELECT 1 AS id, 2 AS n) "
            "SELECT * FROM a JOIN b USING (id)"
        )
        assert _redacted(sql, ["id", "m", "n"]) == set()


class TestWholeRowReads:
    """A whole-row reference returns every column of a source, sensitive ones included."""

    @pytest.mark.parametrize(
        ("sql", "dq"),
        [
            # an unaliased table read as a whole row on PostgreSQL
            ("SELECT u FROM u", False),
            ("SELECT (SELECT u FROM orders o LIMIT 1) AS x FROM u", False),
            # a correlated reference to an outer real table
            ("SELECT (SELECT u.to_jsonb FROM orders o LIMIT 1) AS x FROM users u", False),
            ("SELECT (SELECT to_jsonb(u) FROM orders o LIMIT 1) AS x FROM users u", False),
            ("SELECT (SELECT u FROM orders o LIMIT 1) AS x FROM users u", False),
            # LATERAL
            ("SELECT l.x FROM users u, LATERAL (SELECT u.to_jsonb AS x) l", False),
            ("SELECT l.x FROM users u CROSS JOIN LATERAL (SELECT to_jsonb(u) AS x) l", False),
            # a schema-qualified table without an alias
            ("SELECT to_jsonb(users) AS x FROM public.users", False),
            ("SELECT users.to_jsonb AS x FROM public.users", False),
            # a keyword alias
            ("SELECT data.to_jsonb AS x FROM users data", False),
            ("SELECT to_jsonb(data) AS x FROM users data", False),
            ("SELECT data AS x FROM users AS data", False),
            # a correlated reference to an outer derived table
            (
                "SELECT (SELECT to_jsonb(d) FROM orders o LIMIT 1) AS x "
                "FROM (SELECT email FROM users) d",
                False,
            ),
        ],
    )
    def test_the_row_is_masked(self, sql: str, dq: bool) -> None:
        cols = ["x"] if " AS x" in sql else ["u"]
        assert _redacted(sql, cols, double_quote_is_string_literal=dq) == {0}

    @pytest.mark.parametrize(
        ("sql", "dq"),
        [
            # MySQL has no whole-row reference: the bare name is the column
            ("SELECT country AS x FROM country", True),
            ("SELECT id AS x FROM u", False),
            (
                "SELECT (SELECT max(o.total) FROM orders o WHERE o.user_id = u.id) AS x "
                "FROM users u",
                False,
            ),
            ("SELECT l.x FROM users u, LATERAL (SELECT u.id AS x) l", False),
            ("SELECT id AS x FROM public.users", False),
            ("SELECT users.id AS x FROM public.users", False),
            ("SELECT data.id AS x FROM users data", False),
            ("SELECT data.id AS x FROM users AS data JOIN orders o ON o.uid = data.id", False),
        ],
    )
    def test_a_harmless_query_of_the_same_shape_is_not_masked(self, sql: str, dq: bool) -> None:
        assert _redacted(sql, ["x"], double_quote_is_string_literal=dq) == set()


class TestKeywordSpelledNames:
    """A name sqlparse reads as a keyword (``comment``, ``data``, ``date``) is still a column
    reference."""

    _COMMENT = PrivacySettings(sensitive_columns=["comment"])

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT comment AS c FROM t",
            "SELECT lower(comment) AS c FROM t",
            "SELECT c FROM (SELECT comment AS c FROM t) s",
        ],
    )
    def test_a_sensitive_keyword_spelled_column_is_masked(self, sql: str) -> None:
        assert _redacted(sql, ["c"], self._COMMENT) == {0}

    def test_a_masked_column_renamed_to_a_keyword_is_masked(self) -> None:
        sql = "SELECT data FROM (SELECT email AS data FROM users) s"
        assert _redacted(sql, ["data"]) == {0}

    def test_a_harmless_column_renamed_to_a_keyword_is_not_masked(self) -> None:
        sql = "SELECT date FROM (SELECT created_at AS date FROM users) s"
        assert _redacted(sql, ["date"]) == set()


class TestIdentifierWordIsWiderThanTheEngines:
    @pytest.mark.parametrize(
        "name", ["mail\u20acx", "e\u0301mail", "\u03c4\u03b1\u03c7\u03c5\u0301"]
    )
    def test_a_non_word_non_ascii_character_does_not_split_a_name(self, name: str) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=[name])
        sql = f"SELECT {name} FROM t"  # noqa: S608 — test input, never executed
        assert result_masking.sql_mentions_sensitive(sql, privacy)

    def test_a_harmless_non_ascii_name_does_not_mention(self) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=["mail€x"])
        assert not result_masking.sql_mentions_sensitive("SELECT mail€y FROM t", privacy)


class TestFallbackWholeRowCandidates:
    """In a shape the resolver cannot follow, a bare name equal to a source's name or alias
    may be the whole row on PostgreSQL: every column is masked."""

    _GIVE_UP = "FROM (users u JOIN z ON true) AS g"

    @pytest.mark.parametrize(
        "sql",
        [
            f"SELECT u {_GIVE_UP}",
            f"SELECT u AS x, z.v {_GIVE_UP}",
            f"SELECT coalesce(u::text, '') AS x {_GIVE_UP}",
            "SELECT users AS x FROM (users JOIN z ON true) AS g",
            "SELECT u FROM users u FOR UPDATE",
            "SELECT (SELECT u FROM t FOR UPDATE) AS x FROM users u",
        ],
    )
    def test_a_whole_row_candidate_masks_every_column(self, sql: str) -> None:
        assert _masked(sql) == _ALL

    @pytest.mark.parametrize(
        "sql",
        [
            f"SELECT g.v, g.id {_GIVE_UP}",
            f"SELECT v, n {_GIVE_UP}",
            "SELECT u.id FROM users u FOR UPDATE",
            f"SELECT id {_GIVE_UP} WHERE u IS NOT NULL",
        ],
    )
    def test_a_harmless_query_of_the_same_shape_is_left_to_name_matching(self, sql: str) -> None:
        assert _masked(sql) is None

    def test_mysql_has_no_whole_row_reference(self) -> None:
        sql = "SELECT u FROM users u FOR UPDATE"
        assert _masked(sql, double_quote_is_string_literal=True) is None


class TestHasEscapeStringContinuation:
    @pytest.mark.parametrize(
        "sql",
        ["SELECT E'a'\n'b' AS x", "SELECT e'a' \n  'b'", "SELECT 1 WHERE x = E'\\''\n'z'"],
    )
    def test_an_escape_string_continued_on_a_later_line(self, sql: str) -> None:
        assert result_masking.has_escape_string_continuation(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "/*!*/ SELECT E'a'\n'b' FROM u",
            "SELECT /*M! x */ E'a'\n'b' FROM u",
            "SELECT 1 /*!50000 2 */, E'a'\n'b'",
        ],
    )
    def test_a_mysql_executable_comment_is_a_plain_comment_on_postgresql(self, sql: str) -> None:
        assert result_masking.has_escape_string_continuation(sql)

    @pytest.mark.parametrize("opener", ["/*!", "/*!50000", "/*M!", "/*m!", "/*m!100000"])
    @pytest.mark.parametrize("dq", [True, False])
    def test_the_opener_is_matched_in_any_case(self, opener: str, dq: bool) -> None:
        # Kept in step with mysql_validator._EXECUTABLE_OPENER, which is case-insensitive.
        sql = f"SELECT {opener} email */ 1"
        with pytest.raises(result_masking._ExecutableCommentError):
            result_masking._lex_like_the_engine(sql, dq)
        assert result_masking.EXECUTABLE_COMMENT.search(sql)

    @pytest.mark.parametrize("dq", [True, False])
    def test_masking_still_refuses_an_executable_comment(self, dq: bool) -> None:
        with pytest.raises(result_masking._ExecutableCommentError):
            result_masking._lex_like_the_engine("SELECT /*! email */ 1", dq)
        result = _classify_select_columns(
            "SELECT 1 AS a, /*! email AS e, */ 2 AS b FROM users",
            _PRIVACY,
            double_quote_is_string_literal=dq,
        )
        assert result is not None and result[0] is MASK_ALL_COLUMNS

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT E'a' 'b'",
            "SELECT 'a'\n'b'",
            "SELECT E'a', 'b'",
            "SELECT /*! 1 */ 2",
            "SELECT 'unterminated",
            "",
        ],
    )
    def test_anything_else_is_not(self, sql: str) -> None:
        assert not result_masking.has_escape_string_continuation(sql)


_EMAIL_ONLY = PrivacySettings(sensitive_column_patterns=["email"])


def _leaks(
    sql: str, columns: list[str], leaked: list[int], privacy: PrivacySettings, dq: bool = False
) -> list[str]:
    """The columns of *leaked* that come back in plaintext."""
    got = _redacted(sql, columns, privacy, double_quote_is_string_literal=dq)
    return [columns[i] for i in leaked if i not in got]


class TestReviewLeaks:
    """Leaks an adversarial review found with strict mode off: each value is masked, and a
    harmless query of the same shape is not."""

    @pytest.mark.parametrize(
        ("sql", "columns", "leaked"),
        [
            # 1. a join group under an alias, read through a qualified star
            ("SELECT ROW(j.*) AS t FROM (users u CROSS JOIN orders o) AS j", ["t"], [0]),
            ("SELECT j.*::text AS t FROM (users u CROSS JOIN orders o) AS j", ["t"], [0]),
            ("SELECT (j.*)::text AS t FROM (users u JOIN orders o ON true) j", ["t"], [0]),
            ("SELECT ROW(x.*) AS t FROM users x, (users u JOIN orders o ON true) j", ["t"], [0]),
            (
                "WITH x AS (SELECT email AS c FROM users) SELECT j.c FROM (x JOIN t ON true) AS j",
                ["c"],
                [0],
            ),
            # 2. a qualified star in an expression, in a shape the resolver cannot follow
            (
                "SELECT ROW(u.*) AS x FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n)",
                ["x"],
                [0],
            ),
            (
                "SELECT (u.*)::text AS x FROM users u, "
                "generate_series(1,1) WITH ORDINALITY AS z(v,n)",
                ["x"],
                [0],
            ),
            ("SELECT q FROM (SELECT ROW(u.*) AS q FROM users u)", ["q"], [0]),
            ("SELECT q FROM (SELECT xmlforest(u.*) AS q FROM users u)", ["q"], [0]),
            (
                "SELECT x FROM (SELECT u.e AS x FROM (SELECT ROW(z.*) AS e FROM users z) u)",
                ["x"],
                [0],
            ),
            ("SELECT q FROM (SELECT * FROM users) s(q) FOR UPDATE", ["q"], [0]),
            # keyword alias misread: the table name still counts
            ("SELECT users AS x FROM users RETURNING 1", ["x"], [0]),
            ("SELECT to_jsonb(users) AS x FROM users AT", ["x"], [0]),
            # 3. a whole row as the argument of a table function or a LATERAL VALUES
            ("SELECT value FROM users u, jsonb_each_text(to_jsonb(u))", ["value"], [0]),
            (
                "SELECT * FROM users u, jsonb_each_text(to_jsonb(u))",
                ["id", "email", "key", "value"],
                [1, 3],
            ),
            ("SELECT t.* FROM users u, jsonb_each_text(to_jsonb(u)) t", ["key", "value"], [1]),
            ("SELECT value FROM users, jsonb_each_text(to_jsonb(users))", ["value"], [0]),
            ("SELECT r FROM users u, regexp_split_to_table(u::text, ',') r", ["r"], [0]),
            ("SELECT r FROM users u, unnest(ARRAY[u.*]) r", ["r"], [0]),
            ("SELECT x FROM users u, LATERAL unnest(ARRAY[u]) x", ["x"], [0]),
            ("SELECT unnest FROM users u, unnest(ARRAY[u])", ["unnest"], [0]),
            (
                "SELECT x.e FROM users u, jsonb_populate_record(null::users, to_jsonb(u)) "
                "AS x(i, e)",
                ["e"],
                [0],
            ),
            ("SELECT r FROM users u, LATERAL (VALUES (u::text)) v(r)", ["r"], [0]),
            ("SELECT r FROM users u, LATERAL (VALUES (ROW(u.*))) v(r)", ["r"], [0]),
            (
                "SELECT r FROM (SELECT email FROM users) s, regexp_split_to_table(s::text, ',') r",
                ["r"],
                [0],
            ),
            (
                "WITH s AS (SELECT * FROM users) "
                "SELECT r FROM s, regexp_split_to_table(s::text, ',') r",
                ["r"],
                [0],
            ),
            (
                "SELECT (SELECT value FROM jsonb_each_text(to_jsonb(u)) LIMIT 1) AS x FROM users u",
                ["x"],
                [0],
            ),
            (
                "SELECT j.v FROM users u, "
                "LATERAL (SELECT value AS v FROM jsonb_each_text(to_jsonb(u))) j",
                ["v"],
                [0],
            ),
            (
                "SELECT j.value FROM users u, "
                "LATERAL (SELECT * FROM jsonb_each_text(to_jsonb(u))) j",
                ["value"],
                [0],
            ),
            # 4. a table function declaring its own columns
            (
                "SELECT e FROM users u, XMLTABLE('/r' PASSING xmlelement(name r, u.email) "
                "COLUMNS e text PATH '.') x",
                ["e"],
                [0],
            ),
            (
                "SELECT e FROM users u, JSON_TABLE(to_jsonb(u.email), '$' "
                "COLUMNS (e text PATH '$')) x",
                ["e"],
                [0],
            ),
            # 6. a Unicode-escape identifier
            ('SELECT U&"em\\0061il" AS x FROM users', ["x"], [0]),
            ("SELECT U&\"em!0061il\" UESCAPE '!' AS x FROM users", ["x"], [0]),
            ('SELECT x FROM (SELECT u&"\\+000065mail" AS x FROM users) s', ["x"], [0]),
        ],
    )
    def test_the_value_is_masked(self, sql: str, columns: list[str], leaked: list[int]) -> None:
        assert _leaks(sql, columns, leaked, _EMAIL_ONLY) == []

    def test_mysql_json_table_columns_are_masked(self) -> None:
        sql = "SELECT e FROM t, JSON_TABLE(JSON_ARRAY(t.email), '$[*]' COLUMNS (e TEXT PATH '$')) j"
        assert _leaks(sql, ["e"], [0], _EMAIL_ONLY, dq=True) == []

    @pytest.mark.parametrize(
        ("sql", "columns"),
        [
            ("SELECT j.id AS t FROM (users u CROSS JOIN orders o) AS j", ["t"]),
            ("SELECT j.n FROM (users u JOIN orders o ON true) AS j", ["n"]),
            ("SELECT u.* FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n)", ["id"]),
            ("SELECT count(*) AS n FROM users u, unnest(u.tags) WITH ORDINALITY AS z(v, n)", ["n"]),
            ("SELECT value FROM users u, jsonb_each_text(u.prefs)", ["value"]),
            ("SELECT r FROM users u, regexp_split_to_table(u.tags_csv, ',') r", ["r"]),
            ("SELECT r FROM users u, LATERAL (VALUES (u.id)) v(r)", ["r"]),
            ("SELECT c FROM (VALUES (1)) v(c), users u", ["c"]),
            (
                "SELECT e FROM users u, XMLTABLE('/r' PASSING u.doc COLUMNS e text PATH '.') x",
                ["e"],
            ),
            ('SELECT U&"n\\0061me" AS x FROM users', ["x"]),
        ],
    )
    def test_a_harmless_query_of_the_same_shape_is_not_masked(
        self, sql: str, columns: list[str]
    ) -> None:
        assert _redacted(sql, columns, _EMAIL_ONLY) == set()

    def test_a_unicode_escape_identifier_is_a_sensitive_mention(self) -> None:
        assert result_masking.sql_mentions_sensitive('SELECT U&"em\\0061il" FROM t', _EMAIL_ONLY)
        assert result_masking.sql_mentions_sensitive(
            "SELECT U&\"em!0061il\" UESCAPE '!' FROM t", _EMAIL_ONLY
        )
        assert not result_masking.sql_mentions_sensitive('SELECT U&"n\\0061me" FROM t', _EMAIL_ONLY)


class TestKeywordColumnWithABareAlias:
    """5. ``SELECT data x``: a keyword-spelled column followed by a bare alias."""

    @pytest.mark.parametrize(
        "name",
        ["data", "comment", "key", "role", "level", "type", "owner", "year", "source", "position"],
    )
    @pytest.mark.parametrize(
        "template",
        [
            "SELECT x FROM (SELECT {n} x FROM t) s",
            "WITH s AS (SELECT {n} x FROM t) SELECT x FROM s",
            "SELECT x FROM (SELECT t.id, {n} x FROM t) s",
        ],
    )
    @pytest.mark.parametrize("dq", [False, True])
    def test_the_value_is_masked(self, name: str, template: str, dq: bool) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=[name])
        assert _leaks(template.format(n=name), ["x"], [0], privacy, dq) == []

    def test_a_harmless_keyword_column_is_not_masked(self) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=[], sensitive_columns=["data"])
        assert _redacted("SELECT x FROM (SELECT type x FROM t) s", ["x"], privacy) == set()

    def test_a_reference_a_known_source_does_not_have_is_not_understood(self) -> None:
        assert _masked("SELECT y FROM (SELECT id AS x FROM t) s") is None
        assert _masked("SELECT s.y FROM (SELECT id AS x FROM t) s") is None
        assert _masked("SELECT x FROM (SELECT id AS x FROM t) s") == set()


class TestReferenceTokens:
    """8. A DML, DDL or CTE keyword is never a column reference."""

    def test_statement_keywords_are_not_references(self) -> None:
        import sqlparse

        stmt = sqlparse.parse("WITH a AS (SELECT data FROM t WHERE x IS NULL) SELECT 1")[0]
        leaves = {t.value.upper(): t for t in stmt.flatten() if not t.is_whitespace}
        for word in ("SELECT", "FROM", "WHERE", "IS", "NULL", "AS"):
            assert not result_masking._is_reference_token(leaves[word], False), word
        assert not result_masking._is_reference_token(leaves["WITH"], False)
        assert result_masking._is_reference_token(leaves["DATA"], False)
        assert result_masking._is_reference_token(leaves["T"], False)


class TestTablesampleScanIsLinear:
    """7. ``_column_neutral_tokens`` matches each parenthesis once."""

    def test_repeated_tablesample_openers(self) -> None:
        import time

        sql = "SELECT email FROM t " + "TABLESAMPLE x (" * 16000  # about 256 KB
        start = time.perf_counter()
        _masked(sql)
        assert time.perf_counter() - start < 5.0


class TestSecondReviewLeaks:
    """Leaks a second adversarial review found: each value is masked, and a harmless query
    of the same shape is not."""

    @pytest.mark.parametrize(
        ("sql", "columns", "leaked", "dq"),
        [
            # A. a column alias spelled as a grammar keyword
            ("SELECT over AS x FROM (SELECT email AS over FROM users) s", ["x"], [0], False),
            (
                "WITH c AS (SELECT email AS between FROM users) SELECT between AS x FROM c",
                ["x"],
                [0],
                False,
            ),
            (
                "WITH a AS (SELECT email AS over FROM users), b AS (SELECT over AS p FROM a), "
                "c AS (SELECT p AS q FROM b) SELECT q AS r FROM c",
                ["r"],
                [0],
                False,
            ),
            ("SELECT s.from AS x FROM (SELECT email AS from FROM users) s", ["x"], [0], False),
            ("SELECT s.using AS x FROM (SELECT email AS using FROM users) s", ["x"], [0], False),
            ("SELECT s.case AS x FROM (SELECT email AS case FROM users) s", ["x"], [0], False),
            ("SELECT s.in AS x FROM (SELECT email AS in FROM users) s", ["x"], [0], False),
            ("SELECT end AS x FROM (SELECT email AS end FROM users) s", ["x"], [0], True),
            ("SELECT offset AS x FROM (SELECT email AS offset FROM users) s", ["x"], [0], True),
            ("SELECT ilike AS x FROM (SELECT email AS ilike FROM users) s", ["x"], [0], True),
            ("SELECT s.from AS x FROM (SELECT email AS `from` FROM users) s", ["x"], [0], True),
            # B. IS [NOT] DISTINCT FROM in the SELECT list
            (
                "SELECT 1 IS DISTINCT FROM 2 AS d, (SELECT u::text FROM users u LIMIT 1) AS x",
                ["d", "x"],
                [1],
                False,
            ),
            (
                "SELECT 1 IS NOT DISTINCT FROM 2 AS d, "
                "(SELECT to_jsonb(u) FROM users u LIMIT 1) AS x",
                ["d", "x"],
                [1],
                False,
            ),
            (
                "SELECT 1 IS DISTINCT FROM 2 AS d, t.x FROM (SELECT u::text AS x FROM users u) t",
                ["d", "x"],
                [1],
                False,
            ),
            (
                "SELECT 1 IS DISTINCT FROM 2 AS d, x FROM users u, LATERAL (SELECT u::text AS x) t",
                ["d", "x"],
                [1],
                False,
            ),
            # C1. TABLE
            ("SELECT x FROM (TABLE users) t(a, x)", ["x"], [0], False),
            ("WITH c(a, b) AS (TABLE users) SELECT b FROM c", ["b"], [0], False),
            ("SELECT b FROM (TABLE users) t(a, b)", ["b"], [0], True),
            # C2. a real table renamed by a column list
            ("SELECT j.b FROM (users AS t(a, b) JOIN orders o ON true) AS j", ["b"], [0], False),
            (
                "SELECT b FROM unnest(ARRAY[1]) WITH ORDINALITY AS z(q, r), users t(a, b)",
                ["b"],
                [0],
                False,
            ),
            # C3. a whole row in a FROM function argument
            (
                "SELECT r.x FROM users u, LATERAL unnest(ARRAY[u::text]) WITH ORDINALITY r(x, n)",
                ["x"],
                [0],
                False,
            ),
            (
                "SELECT r.x FROM users u, "
                "LATERAL regexp_split_to_table(u::text, ',') WITH ORDINALITY AS r(x, n)",
                ["x"],
                [0],
                False,
            ),
            (
                "SELECT j.x FROM (users u CROSS JOIN LATERAL "
                "unnest(ARRAY[concat_ws(',', u)]) r(x)) AS j",
                ["x"],
                [0],
                False,
            ),
            # C4. MySQL ``*`` under a column list
            (
                "SELECT t.b FROM (SELECT * FROM users) t(a, b) "
                "JOIN orders o USE INDEX (PRIMARY) ON true",
                ["b"],
                [0],
                True,
            ),
            (
                "WITH c(a, b) AS (SELECT * FROM users) "
                "SELECT b FROM c JOIN orders o USE INDEX (PRIMARY) ON true",
                ["b"],
                [0],
                True,
            ),
            (
                "SELECT t.b FROM (SELECT * FROM users) t(a, b), orders PARTITION (p0)",
                ["b"],
                [0],
                True,
            ),
        ],
    )
    def test_the_value_is_masked(
        self, sql: str, columns: list[str], leaked: list[int], dq: bool
    ) -> None:
        assert _leaks(sql, columns, leaked, _EMAIL_ONLY, dq) == []

    @pytest.mark.parametrize(
        ("sql", "columns", "dq"),
        [
            (
                "WITH c AS (SELECT id AS between FROM users) SELECT between AS x FROM c",
                ["x"],
                False,
            ),
            ("SELECT s.from AS x FROM (SELECT id AS from FROM users) s", ["x"], False),
            ("SELECT CAST(total AS int) AS c, CAST(n AS text) AS t FROM orders", ["c", "t"], False),
            (
                "WITH m AS (SELECT 1 AS n) SELECT CAST(n AS double precision) AS c FROM m",
                ["c"],
                False,
            ),
            ("SELECT 1 IS DISTINCT FROM 2 AS d, o.total FROM orders o", ["d", "total"], False),
            (
                "SELECT r.x FROM users u, LATERAL unnest(u.tags) WITH ORDINALITY r(x, n)",
                ["x"],
                False,
            ),
            (
                "SELECT c.name, o.total FROM customers c JOIN orders o USE INDEX (PRIMARY) "
                "ON o.customer_id = c.id WHERE c.email <> ''",
                ["name", "total"],
                True,
            ),
            ("SELECT o.id FROM orders o FORCE INDEX FOR JOIN (ix) PARTITION (p0)", ["id"], True),
            (
                "SELECT o.id, sum(o.total) OVER (PARTITION BY o.cid) AS s FROM orders o",
                ["id", "s"],
                False,
            ),
        ],
    )
    def test_a_harmless_query_of_the_same_shape_is_not_masked(
        self, sql: str, columns: list[str], dq: bool
    ) -> None:
        assert _redacted(sql, columns, _EMAIL_ONLY, double_quote_is_string_literal=dq) == set()


class TestFailureModes:
    """D and E: what a failed or too costly classification masks."""

    @pytest.mark.parametrize(
        "sql",
        ["SELECT to_jsonb(u) AS x FROM users u", "SELECT u FROM users u", "SELECT q FROM t s(q)"],
    )
    def test_a_parse_failure_on_a_possible_whole_row_masks_every_column(self, sql: str) -> None:
        with patch.object(result_masking, "_lex_like_the_engine", side_effect=ValueError("x")):
            result = _classify_select_columns(sql, _EMAIL_ONLY)
        assert result is not None and result[0] is MASK_ALL_COLUMNS

    def test_a_harmless_parse_failure_keeps_name_matching(self) -> None:
        with patch.object(result_masking, "_lex_like_the_engine", side_effect=ValueError("x")):
            assert _classify_select_columns("SELECT id FROM t", _EMAIL_ONLY) is None

    def test_an_unexpected_exception_masks_every_column(self) -> None:
        with patch.object(result_masking, "_classify_output_items", side_effect=KeyError("x")):
            result = _classify_select_columns("SELECT id FROM t", _EMAIL_ONLY)
        assert result is not None and result[0] is MASK_ALL_COLUMNS

    def test_an_executable_comment_masks_every_column(self) -> None:
        result = _classify_select_columns(
            "SELECT 1 AS a /*!, id AS b */ FROM t", _EMAIL_ONLY, double_quote_is_string_literal=True
        )
        assert result is not None and result[0] is MASK_ALL_COLUMNS

    def test_an_exhausted_work_budget_masks_every_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(result_masking, "_CLASSIFICATION_BUDGET", 3)
        sql = "SELECT a FROM (SELECT a FROM (SELECT a FROM (SELECT 1 AS a) x) y) z"
        assert _masked(sql, _EMAIL_ONLY) == _ALL

    def test_nested_recursive_ctes_are_bounded(self) -> None:
        import time

        sql = "SELECT 1 AS n, 'a' AS v"
        for i in range(20):
            sql = (
                f"WITH RECURSIVE r{i}(n, v) AS (({sql}) UNION ALL "  # noqa: S608
                f"SELECT n+1, v FROM r{i} WHERE n<2) SELECT n, v FROM r{i}"
            )
        start = time.perf_counter()
        masked = _masked(sql, _EMAIL_ONLY)
        assert time.perf_counter() - start < 2.0
        assert masked in (set(), _ALL)

    def test_a_mismatch_on_a_possible_whole_row_masks_every_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            result_masking, "_classify_result", lambda *_a: (set(), set(), ("plain",))
        )
        for sql in ("SELECT to_jsonb(u) AS c, id FROM users u", "SELECT u, id FROM users u"):
            plan = _plan_result_mask(_EMAIL_ONLY, sql)
            assert plan is not None and plan.redacted_indices(["c", "id"]) == {0, 1}
        plan = _plan_result_mask(_EMAIL_ONLY, "SELECT name AS c, id FROM users u")
        assert plan is not None and plan.redacted_indices(["c", "id"]) == set()
