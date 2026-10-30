# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Sensitive values are redacted by what the SELECT list exposes, not by what it is called."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

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


def _masked(sql: str, privacy: PrivacySettings = _PRIVACY, **kw) -> set[str] | None:
    result = _classify_select_columns(sql, privacy, **kw)
    return None if result is None else result[0]


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

    def test_unparseable_is_uncertain(self) -> None:
        assert _masked("not sql at all") is None

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
            ("WITH x AS (SELECT id FROM users) SELECT c FROM x", set()),
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
            ("SELECT id FROM a UNION SELECT id, email FROM b", None),
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


class TestFallbackKeepsOuterSelectListMasking:
    """A FROM shape the scoped resolver does not parse must not drop what the outermost
    SELECT list alone shows: ``email AS contact`` is masked whatever follows it."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT email AS contact FROM users JOIN t USING (id)",
            "SELECT email AS contact FROM users u JOIN t ON (u.id = t.id)",
            "SELECT email AS contact FROM users u JOIN t ON u.id = t.id AND (t.a = 1 OR t.b = 2)",
            "SELECT email AS contact FROM users u JOIN t ON u.id = t.id AND t.k IN (1, 2)",
            "SELECT email AS contact FROM users u JOIN t ON u.id = t.id AND t.k IS NOT NULL",
            "SELECT email AS contact FROM users u JOIN t ON u.id = t.id "
            "JOIN v ON v.id BETWEEN 1 AND 2",
            "SELECT email AS contact FROM users u JOIN t ON NOT t.flag",
            "SELECT email AS contact FROM users u JOIN (t JOIN v ON v.id = t.id) ON u.id = t.id",
            "SELECT email AS contact FROM users TABLESAMPLE SYSTEM (10)",
            "SELECT email AS contact FROM users STRAIGHT_JOIN t ON t.id = users.id",
            "SELECT email AS contact FROM users u USE INDEX (ix) JOIN t ON t.id = u.id",
            "SELECT email AS contact FROM users PARTITION (p0)",
            "SELECT email AS contact FROM ONLY users",
            "SELECT email AS contact FROM users FOR UPDATE",
            "WITH RECURSIVE r AS (SELECT 1) SELECT email AS contact FROM users",
            "WITH x AS MATERIALIZED (SELECT 1) SELECT email AS contact FROM users",
            "WITH x AS (SELECT * FROM users) SELECT email AS contact FROM x",
            "SELECT email AS contact FROM (SELECT * FROM users) s",
            "SELECT s.email AS contact FROM (SELECT * FROM users) s",
            "SELECT email AS contact FROM users UNION SELECT a, b FROM v",
            "SELECT email AS contact FROM users UNION SELECT * FROM v",
        ],
    )
    def test_outer_sensitive_reference_stays_masked(self, sql: str) -> None:
        assert _masked(sql) == {"contact"}

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
        assert _masked("SELECT COUNT(email) AS n FROM users TABLESAMPLE SYSTEM (10)") is None

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
            ("SELECT country FROM country", ["country"], False, set()),
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
        assert _classify_select_columns("SELECT id FROM t", _PRIVACY) is None

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
