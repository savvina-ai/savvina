# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""apply_row_filters: every filtered base-table reference becomes a filtered derived table,
or the statement is rejected before it runs."""

# ruff: noqa: S608 — every string here is a test query, never executed.

from __future__ import annotations

import contextlib

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import UnsupportedError

from app.services import row_filter_rewrite
from app.services.row_filter_errors import RowFilterRejectedError
from app.services.row_filter_rewrite import apply_row_filters, parse_condition

_DIALECTS = ["PostgreSQL", "MySQL"]
_READ = {"PostgreSQL": "postgres", "MySQL": "mysql"}
_SCHEMA = {"PostgreSQL": "public", "MySQL": "sample_delivery"}
_COND = "tenant_id = 7"


def _filters(dialect: str, *tables: str, cond: str = _COND) -> dict[str, str]:
    return {f"{_SCHEMA[dialect]}.{t}": cond for t in tables}


def _tree(out: str, dialect: str) -> exp.Expression:
    return sqlglot.parse_one(out, read=_READ[dialect])


def _derived(out: str, dialect: str) -> dict[str, exp.Select]:
    """alias -> the SELECT inside every aliased derived table of *out*."""
    return {s.alias: s.this for s in _tree(out, dialect).find_all(exp.Subquery) if s.alias}


def _inner_table(select: exp.Select) -> exp.Table:
    table = select.args["from_"].this  # sqlglot 30 keys the FROM clause "from_"
    assert isinstance(table, exp.Table)
    return table


def _condition(select: exp.Select, dialect: str) -> str:
    where = select.args["where"].this
    assert isinstance(where, exp.Paren), "the condition is parenthesised"
    return where.this.sql(dialect=_READ[dialect])


def _expected(cond: str, dialect: str) -> str:
    return parse_condition(cond, _READ[dialect]).sql(dialect=_READ[dialect])


def _assert_plain(exc: RowFilterRejectedError) -> None:
    assert "'" not in str(exc) and '"' not in str(exc)


def _assert_every_reference_filtered(out: str, dialect: str, names: set[str]) -> None:
    """Every table in *names* sits alone in the FROM of a SELECT whose WHERE is the
    parenthesised condition, directly under an aliased derived table: no reference to a
    filtered table is left bare anywhere in *out*."""
    for table in _tree(out, dialect).find_all(exp.Table):
        if table.name.casefold() not in names:
            continue
        select = table.parent.parent if isinstance(table.parent, exp.From) else None
        assert isinstance(select, exp.Select), f"bare reference to {table.name}: {out}"
        where = select.args.get("where")
        assert where is not None and isinstance(where.this, exp.Paren), out
        assert isinstance(select.parent, exp.Subquery) and select.parent.alias, out


@pytest.mark.parametrize("dialect", _DIALECTS)
class TestRewrites:
    def test_simple_from_without_alias_is_aliased_with_the_table_name(self, dialect: str) -> None:
        out = apply_row_filters("SELECT id FROM orders", _filters(dialect, "orders"), dialect)
        derived = _derived(out, dialect)
        assert list(derived) == ["orders"]
        assert _inner_table(derived["orders"]).name == "orders"
        assert _condition(derived["orders"], dialect) == _expected(_COND, dialect)

    def test_qualified_name_and_alias_are_kept(self, dialect: str) -> None:
        sql = f"SELECT o.id FROM {_SCHEMA[dialect]}.orders AS o"
        derived = _derived(apply_row_filters(sql, _filters(dialect, "orders"), dialect), dialect)
        assert list(derived) == ["o"]
        inner = _inner_table(derived["o"])
        assert (inner.db, inner.name) == (_SCHEMA[dialect], "orders")
        assert inner.alias == ""

    def test_both_sides_of_a_join_are_filtered(self, dialect: str) -> None:
        sql = (
            "SELECT o.id FROM orders o INNER JOIN customers c ON c.id = o.customer_id"
            " LEFT JOIN items i ON i.order_id = o.id"
        )
        out = apply_row_filters(sql, _filters(dialect, "orders", "customers"), dialect)
        derived = _derived(out, dialect)
        assert set(derived) == {"o", "c"}
        assert "i" not in derived  # the unfiltered side is still a plain table
        assert all(_condition(s, dialect) == _expected(_COND, dialect) for s in derived.values())

    def test_implicit_comma_join(self, dialect: str) -> None:
        sql = "SELECT * FROM orders o, customers c WHERE o.customer_id = c.id"
        out = apply_row_filters(sql, _filters(dialect, "orders", "customers"), dialect)
        assert set(_derived(out, dialect)) == {"o", "c"}

    def test_subquery_in_where(self, dialect: str) -> None:
        sql = "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers)"
        out = apply_row_filters(sql, _filters(dialect, "customers"), dialect)
        derived = _derived(out, dialect)
        assert list(derived) == ["customers"]

    def test_cte_with_another_name_has_its_body_rewritten_and_its_reference_left_alone(
        self, dialect: str
    ) -> None:
        sql = "WITH recent AS (SELECT * FROM orders WHERE status = 'x') SELECT id FROM recent"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        tree = _tree(out, dialect)
        assert list(_derived(out, dialect)) == ["orders"]
        outer_from = tree.args["from_"].this
        assert isinstance(outer_from, exp.Table) and outer_from.name == "recent"

    @pytest.mark.parametrize(
        "sql",
        [
            # Inside a non-recursive CTE body the bare name is the base table; the rewriter
            # does not track that scope, so a CTE named like a filtered table is rejected.
            "WITH orders AS (SELECT * FROM orders) SELECT id FROM orders",
            "WITH RECURSIVE orders AS (SELECT 1 AS id) SELECT id FROM orders",
            # Exact after PostgreSQL folding, case-insensitive on MySQL: rejected on both.
            "WITH Orders AS (SELECT 1 AS id) SELECT id FROM Orders",
        ],
    )
    def test_cte_named_like_a_filtered_table_is_rejected(self, dialect: str, sql: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        _assert_plain(excinfo.value)
        assert "defines a CTE named like the filtered table" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None and "Rename the CTE" in excinfo.value.llm_hint

    def test_union_with_one_filtered_branch(self, dialect: str) -> None:
        sql = "SELECT id FROM orders UNION ALL SELECT id FROM archive"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert list(_derived(out, dialect)) == ["orders"]

    def test_aggregate_with_group_by(self, dialect: str) -> None:
        sql = "SELECT status, COUNT(*) FROM orders GROUP BY status"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert list(_derived(out, dialect)) == ["orders"]

    def test_limit_and_offset_survive(self, dialect: str) -> None:
        sql = "SELECT id FROM orders ORDER BY id LIMIT 3 OFFSET 2"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        tree = _tree(out, dialect)
        assert tree.args.get("limit") is not None and tree.args.get("offset") is not None
        assert list(_derived(out, dialect)) == ["orders"]

    def test_self_referencing_condition_is_inserted_once(self, dialect: str) -> None:
        cond = "id IN (SELECT id FROM orders WHERE tenant_id = 7)"
        out = apply_row_filters(
            "SELECT * FROM orders", _filters(dialect, "orders", cond=cond), dialect
        )
        tree = _tree(out, dialect)
        assert len([s for s in tree.find_all(exp.Subquery) if s.alias == "orders"]) == 1

    def test_trailing_semicolon_is_accepted(self, dialect: str) -> None:
        out = apply_row_filters("SELECT id FROM orders;", _filters(dialect, "orders"), dialect)
        assert list(_derived(out, dialect)) == ["orders"]

    def test_empty_map_returns_the_input_object(self, dialect: str) -> None:
        sql = "SELECT id FROM orders"
        assert apply_row_filters(sql, {}, dialect) is sql


class TestDialectSpecificShapes:
    def test_mysql_comma_limit(self) -> None:
        out = apply_row_filters(
            "SELECT id FROM orders LIMIT 2, 3", _filters("MySQL", "orders"), "MySQL"
        )
        tree = _tree(out, "MySQL")
        assert tree.args.get("limit") is not None
        assert list(_derived(out, "MySQL")) == ["orders"]

    def test_tablesample_survives(self) -> None:
        sql = "SELECT * FROM orders TABLESAMPLE SYSTEM (10)"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        derived = _derived(out, "PostgreSQL")
        assert _inner_table(derived["orders"]).args.get("sample") is not None

    def test_table_valued_function_is_left_alone(self) -> None:
        sql = "SELECT * FROM generate_series(1, 3) AS g, orders"
        filters = {"public.generate_series": _COND, "public.orders": _COND}
        out = apply_row_filters(sql, filters, "PostgreSQL")
        assert list(_derived(out, "PostgreSQL")) == ["orders"]


class TestMatching:
    def test_postgres_unquoted_name_folds_to_lowercase(self) -> None:
        out = apply_row_filters("SELECT id FROM Orders", {"public.orders": _COND}, "PostgreSQL")
        assert list(_derived(out, "PostgreSQL")) == ["Orders"]

    def test_postgres_unquoted_name_does_not_match_a_mixed_case_key(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM Orders", {"public.Orders": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert "does not match the filtered table public.Orders exactly" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None and "public.Orders" in excinfo.value.llm_hint

    def test_postgres_quoted_name_matches_the_mixed_case_key_only(self) -> None:
        out = apply_row_filters('SELECT id FROM "Orders"', {"public.Orders": _COND}, "PostgreSQL")
        assert list(_derived(out, "PostgreSQL")) == ["Orders"]
        with pytest.raises(RowFilterRejectedError):
            apply_row_filters('SELECT id FROM "Orders"', {"public.orders": _COND}, "PostgreSQL")

    def test_postgres_two_keys_differing_in_case(self) -> None:
        filters = {"public.orders": "a = 1", "public.Orders": "b = 2"}
        lower = _derived(
            apply_row_filters("SELECT id FROM Orders", filters, "PostgreSQL"), "PostgreSQL"
        )
        assert _condition(lower["Orders"], "PostgreSQL") == "a = 1"
        upper = _derived(
            apply_row_filters('SELECT id FROM "Orders"', filters, "PostgreSQL"), "PostgreSQL"
        )
        assert _condition(upper["Orders"], "PostgreSQL") == "b = 2"

    def test_mysql_keeps_case_and_rejects_a_case_variant(self) -> None:
        filters = {"sample_delivery.Orders": _COND}
        out = apply_row_filters("SELECT id FROM Orders", filters, "MySQL")
        assert list(_derived(out, "MySQL")) == ["Orders"]
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM orders", filters, "MySQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is not None

    def test_qualified_reference_matches_only_its_schema(self) -> None:
        filters = {"a.orders": "x = 1", "b.orders": "x = 2"}
        out = apply_row_filters("SELECT id FROM a.orders", filters, "PostgreSQL")
        assert _condition(_derived(out, "PostgreSQL")["orders"], "PostgreSQL") == "x = 1"

    def test_bare_name_present_in_two_schemas_is_ambiguous(self) -> None:
        filters = {"a.orders": "x = 1", "b.orders": "x = 2"}
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM orders", filters, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert "more than one schema" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None and "schema.table" in excinfo.value.llm_hint

    def test_unlisted_table_is_left_unfiltered(self) -> None:
        out = apply_row_filters("SELECT id FROM other", {"public.orders": _COND}, "PostgreSQL")
        assert _derived(out, "PostgreSQL") == {}


class TestRejections:
    def test_parse_failure_carries_a_hint_quoting_the_model_sql_only(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(
                "SELECT id FROM orders WHERE (", {"public.orders": _COND}, "PostgreSQL"
            )
        _assert_plain(excinfo.value)
        assert "could not be parsed" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None
        assert excinfo.value.llm_hint.startswith(
            "The row-filter rewriter could not parse the query:"
        )

    def test_two_statements(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT 1; SELECT 2", {"public.orders": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is None

    def test_unknown_dialect_with_a_non_empty_map(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT 1", {"a.b": _COND}, "Snowflake")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is None

    def test_generator_limitation_is_a_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(tree: exp.Expression, dialect: str) -> str:
            raise UnsupportedError("x")

        monkeypatch.setattr(row_filter_rewrite, "_generate", _boom)
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM orders", {"public.orders": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is None

    def test_unterminated_string_is_a_rejection_with_a_hint(self) -> None:
        # sqlglot raises TokenError here, which is not a ParseError: both are caught.
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT 'abc FROM orders", {"public.orders": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert "could not be parsed" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None

    def test_nested_join_inside_a_filtered_table_reference_is_rejected(self) -> None:
        sql = "SELECT * FROM orders o JOIN (customers c JOIN items i ON i.c = c.id) ON c.id = o.c"
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, {"public.customers": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert "nests a join" in str(excinfo.value)

    def test_nested_join_on_an_unfiltered_table_is_left_alone(self) -> None:
        # Only the node that matched a filter is checked; the tables inside the nested join
        # are visited on their own.
        sql = "SELECT * FROM orders o JOIN (customers c JOIN items i ON i.c = c.id) ON c.id = o.c"
        filters = {"public.orders": _COND, "public.items": _COND}
        out = apply_row_filters(sql, filters, "PostgreSQL")
        assert set(_derived(out, "PostgreSQL")) == {"o", "i"}

    @pytest.mark.parametrize("cond", ["tenant_id = (", "name = 'x"])
    def test_unparsable_stored_condition_is_a_rejection_without_hint(self, cond: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM orders", {"public.orders": cond}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is None


class TestUnfilterableShapes:
    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("PostgreSQL", "SELECT query_to_xml('SELECT * FROM orders', true, false, '')"),
            (
                "PostgreSQL",
                "SELECT pg_catalog.QUERY_TO_XML('SELECT * FROM orders', true, false, '')",
            ),
            ("PostgreSQL", "SELECT table_to_xml('public.orders', true, false, '')"),
            ("PostgreSQL", "SELECT table_to_xml_and_xmlschema('public.orders', true, false, '')"),
            ("PostgreSQL", "SELECT cursor_to_xml('c', 10, true, false, '')"),
            ("PostgreSQL", "SELECT schema_to_xml('public', true, false, '')"),
            ("PostgreSQL", "SELECT database_to_xml(true, false, '')"),
            ("PostgreSQL", "SELECT * FROM ts_stat('SELECT to_tsvector(note) FROM orders')"),
            ("PostgreSQL", "SELECT * FROM crosstab('SELECT a, b, c FROM orders') AS ct(a int)"),
            ("PostgreSQL", "SELECT * FROM crosstab3('SELECT a, b, c FROM orders')"),
            ("PostgreSQL", "SELECT * FROM connectby('orders', 'id', 'parent', '1', 0) AS t(a int)"),
            ("MySQL", "SELECT query_to_xml('SELECT * FROM orders', true, false, '')"),
            (
                "PostgreSQL",
                "SELECT ts_rewrite('x'::tsquery,"
                " 'SELECT ''x''::tsquery, plainto_tsquery(''simple'', email) FROM orders')",
            ),
            ("PostgreSQL", "SELECT dblink_connect('c', 'dbname=x')"),
            ("PostgreSQL", "SELECT * FROM dblink('c', 'SELECT 1') AS t(a int)"),
        ],
    )
    def test_function_reading_rows_from_a_string_is_rejected(self, dialect: str, sql: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        _assert_plain(excinfo.value)
        assert "reads rows the row filter cannot see" in str(excinfo.value)
        assert excinfo.value.llm_hint is not None

    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("PostgreSQL", "SELECT most_common_vals FROM pg_stats WHERE tablename = 'orders'"),
            ("PostgreSQL", "SELECT * FROM pg_catalog.pg_statistic"),
            ("PostgreSQL", "SELECT * FROM pg_stats_ext"),
            ("MySQL", "SELECT histogram FROM information_schema.column_statistics"),
        ],
    )
    def test_value_statistics_view_is_rejected(self, dialect: str, sql: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        _assert_plain(excinfo.value)
        assert "exposes values the row filter cannot see" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("PostgreSQL", "SELECT * FROM public.pg_stats"),
            ("PostgreSQL", "SELECT * FROM public.column_statistics"),
            ("MySQL", "SELECT * FROM column_statistics"),
            ("MySQL", "SELECT * FROM sample_delivery.pg_stats"),
        ],
    )
    def test_a_user_table_named_like_a_statistics_view_runs(self, dialect: str, sql: str) -> None:
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _derived(out, dialect) == {}

    def test_a_table_named_describe_is_rejected_not_left_unfiltered(self) -> None:
        # sqlglot parses this as a DESCRIBE; PostgreSQL reads the table describe aliased d.
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT * FROM describe d", {"public.describe": _COND}, "PostgreSQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint is None

    def test_ordinary_functions_are_not_affected(self) -> None:
        sql = "SELECT xmlelement(name x, id), to_tsvector(note) FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        _assert_every_reference_filtered(out, "PostgreSQL", {"orders"})

    def test_nothing_is_checked_without_filters(self) -> None:
        sql = "SELECT query_to_xml('SELECT * FROM orders', true, false, '')"
        assert apply_row_filters(sql, {}, "PostgreSQL") is sql


class TestHintsAreDropped:
    def test_a_hint_cannot_carry_a_comment_end_into_the_output(self) -> None:
        # sqlglot reads the escaped slash as "/": written back, the hint would end at the
        # first "*/" on a server that reads "/*+" as a plain comment (MariaDB), running the
        # subquery on orders unfiltered.
        sql = r"SELECT /*+ X('*\/ (SELECT email FROM orders LIMIT 1) AS e, /\*') */ id FROM t"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "/*" not in out and "*/" not in out
        assert _tree(out, "MySQL").find(exp.Hint) is None

    def test_an_ordinary_hint_is_dropped_and_the_query_still_rewritten(self) -> None:
        sql = "SELECT /*+ MAX_EXECUTION_TIME(1000) */ id FROM orders"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "/*" not in out
        _assert_every_reference_filtered(out, "MySQL", {"orders"})


class TestCommentsAreDropped:
    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("PostgreSQL", "SELECT id FROM orders -- */ UNION SELECT * FROM orders /*"),
            ("PostgreSQL", "SELECT id /* outer /* nested */ still */ FROM orders"),
            ("MySQL", "SELECT id /*!50000 , email */ FROM orders"),
            ("MySQL", "SELECT id FROM orders # note"),
        ],
    )
    def test_output_has_no_comment(self, dialect: str, sql: str) -> None:
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert "/*" not in out and "--" not in out and "#" not in out
        _assert_every_reference_filtered(out, dialect, {"orders"})


@pytest.mark.parametrize(
    "key", ["orders", "public.orders ", " public.orders", "a.b.c", ".orders", "public."]
)
def test_malformed_key_is_a_rejection(key: str) -> None:
    with pytest.raises(RowFilterRejectedError) as excinfo:
        apply_row_filters("SELECT id FROM orders", {key: _COND}, "PostgreSQL")
    _assert_plain(excinfo.value)
    assert "not in the form schema.table" in str(excinfo.value)


@pytest.mark.parametrize(
    "cond",
    [
        "tenant_id = 7 UNION SELECT 1",
        "tenant_id = 7 LIMIT 5",
        "tenant_id = 7 OFFSET 3",
        "tenant_id = 7 ORDER BY 1",
        "tenant_id = 7 FOR UPDATE",
        "1=1 INTO OUTFILE '/x'",
        "AND x = 1",
        "a = 1 b = 2",
        "tenant_id IN (1, 2",
        "name = 'abc",  # TokenError, not ParseError
    ],
)
@pytest.mark.parametrize("read", ["postgres", "mysql"])
def test_parse_condition_rejects_anything_but_one_expression(cond: str, read: str) -> None:
    with pytest.raises((sqlglot.errors.ParseError, sqlglot.errors.TokenError)):
        parse_condition(cond, read)


_PG_CORPUS = [
    "SELECT id::text, total_amount::numeric(12,2) FROM orders",
    "SELECT DISTINCT ON (customer_id) customer_id, id FROM orders"
    " ORDER BY customer_id, ordered_at DESC",
    "SELECT COUNT(*) FILTER (WHERE status = 'paid') AS paid FROM orders",
    "SELECT ordered_at AT TIME ZONE 'UTC' FROM orders",
    "SELECT id FROM orders WHERE order_number ILIKE 'ord-%'",
    "SELECT meta->'a', meta->>'b', meta#>>'{c,d}' FROM orders",
    "SELECT id FROM orders WHERE status = ANY(ARRAY['paid', 'pending'])",
    "SELECT id FROM orders WHERE ordered_at > NOW() - INTERVAL '1 day'",
    "SELECT id, SUM(total_amount) OVER (PARTITION BY customer_id ORDER BY ordered_at"
    " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM orders",
    "SELECT date_trunc('month', ordered_at) AS m, COUNT(*) FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT EXTRACT(YEAR FROM ordered_at) FROM orders",
    "SELECT COALESCE(NULLIF(status, ''), 'none') FROM orders",
    "SELECT CASE WHEN total_amount > 100 THEN 'big' ELSE 'small' END FROM orders",
    "SELECT o.id FROM orders o LEFT JOIN order_items i ON i.order_id = o.id WHERE i.id IS NULL",
    "WITH recent AS (SELECT * FROM orders WHERE ordered_at > CURRENT_DATE - 7)"
    " SELECT COUNT(*) FROM recent",
]

_MYSQL_CORPUS = [
    "SELECT GROUP_CONCAT(status ORDER BY status SEPARATOR ', ') FROM orders",
    "SELECT DATE_FORMAT(order_date, '%Y-%m') AS m, COUNT(*) FROM orders GROUP BY m",
    "SELECT id FROM orders WHERE order_date > STR_TO_DATE('01/01/2026', '%d/%m/%Y')",
    "SELECT IF(status = 'delivered', 1, 0) AS done, IFNULL(driver_id, 0) FROM orders",
    "SELECT `id`, `status` FROM `orders` WHERE `status` = 'placed'",
    "SELECT id FROM orders ORDER BY id LIMIT 2, 3",
    "SELECT id FROM orders WHERE order_date >= DATE_SUB(NOW(), INTERVAL 7 DAY)",
    "SELECT JSON_EXTRACT(meta, '$.a') FROM orders",
    "SELECT o.id FROM orders o JOIN restaurants r ON r.id = o.restaurant_id"
    " WHERE r.status = 'active'",
    "SELECT restaurant_id, COUNT(*) AS n FROM orders GROUP BY restaurant_id HAVING n > 1",
]


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [("PostgreSQL", s) for s in _PG_CORPUS] + [("MySQL", s) for s in _MYSQL_CORPUS],
)
def test_corpus_round_trips_with_orders_filtered(dialect: str, sql: str) -> None:
    out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
    derived = _derived(out, dialect)
    assert "orders" in {_inner_table(s).name for s in derived.values()}
    _assert_every_reference_filtered(out, dialect, {"orders"})


class TestPostgresIdentifierTruncation:
    """PostgreSQL truncates every identifier, quoted or not, to 63 bytes."""

    _NAME = "t" * 63

    def test_a_longer_reference_that_truncates_to_a_filtered_table_is_filtered(self) -> None:
        sql = f"SELECT * FROM s.{self._NAME}zzz"
        out = apply_row_filters(sql, {f"s.{self._NAME}": _COND}, "PostgreSQL")
        assert "WHERE (tenant_id = 7)" in out

    def test_a_longer_schema_is_truncated_too(self) -> None:
        sql = f"SELECT * FROM {'s' * 70}.orders"
        out = apply_row_filters(sql, {f"{'s' * 63}.orders": _COND}, "PostgreSQL")
        assert "WHERE (tenant_id = 7)" in out

    def test_truncation_counts_bytes_and_keeps_whole_characters(self) -> None:
        name = "é" * 31  # 62 bytes; one more é would be 64
        sql = f'SELECT * FROM s."{name}éé"'
        out = apply_row_filters(sql, {f"s.{name}": _COND}, "PostgreSQL")
        assert "WHERE (tenant_id = 7)" in out

    def test_mysql_does_not_truncate(self) -> None:
        sql = f"SELECT * FROM s.{self._NAME}zzz"
        out = apply_row_filters(sql, {f"s.{self._NAME}": _COND}, "MySQL")
        assert "WHERE (" not in out


def test_a_query_too_deep_to_parse_is_a_rejection() -> None:
    sql = "SELECT " + "(" * 2000 + "1" + ")" * 2000 + " FROM orders"
    with pytest.raises(RowFilterRejectedError) as excinfo:
        apply_row_filters(sql, {"public.orders": _COND}, "PostgreSQL")
    _assert_plain(excinfo.value)
    assert "nested too deeply" in str(excinfo.value)


def test_a_lone_surrogate_in_a_reference_does_not_raise_unicode_encode_error() -> None:
    with contextlib.suppress(RowFilterRejectedError):
        apply_row_filters('SELECT * FROM s."\ud800"', {"s.t": "x = 1"}, "PostgreSQL")


class TestFence:
    """The derived table is fenced so the engine evaluates the admin's condition before any
    predicate the model wrote: a data-dependent error can otherwise tell whether a hidden row
    exists."""

    def test_postgres_derived_table_ends_in_offset_0(self) -> None:
        out = apply_row_filters(
            "SELECT id FROM orders", _filters("PostgreSQL", "orders"), "postgresql"
        )
        select = _derived(out, "PostgreSQL")["orders"]
        assert select.args["offset"].sql(dialect="postgres") == "OFFSET 0"
        assert select.args.get("limit") is None

    def test_mysql_derived_table_ends_in_the_all_rows_limit(self) -> None:
        out = apply_row_filters("SELECT id FROM orders", _filters("MySQL", "orders"), "mysql")
        select = _derived(out, "MySQL")["orders"]
        assert select.args["limit"].sql(dialect="mysql") == "LIMIT 18446744073709551615"
        assert select.args.get("offset") is None

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_the_fence_is_inside_the_derived_table_not_on_the_statement(self, dialect: str) -> None:
        sql = "SELECT id FROM orders ORDER BY id LIMIT 3 OFFSET 2"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect.lower())
        tree = _tree(out, dialect)
        assert tree.args["limit"].sql() == "LIMIT 3"
        assert tree.args["offset"].sql() == "OFFSET 2"

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_every_filtered_reference_is_fenced(self, dialect: str) -> None:
        sql = "SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id"
        out = apply_row_filters(sql, _filters(dialect, "orders", "items"), dialect.lower())
        derived = _derived(out, dialect)
        assert len(derived) == 2
        for select in derived.values():
            if dialect == "PostgreSQL":
                assert select.args["offset"].sql(dialect="postgres") == "OFFSET 0"
                assert select.args.get("limit") is None
            else:
                assert select.args["limit"].sql(dialect="mysql") == "LIMIT 18446744073709551615"
                assert select.args.get("offset") is None


class TestFenceMap:
    def test_every_supported_dialect_has_a_fence(self) -> None:
        assert set(row_filter_rewrite._FENCE) == set(row_filter_rewrite._SQLGLOT_DIALECT.values())

    def test_an_unknown_dialect_is_rejected_not_fenced_like_mysql(self) -> None:
        table = sqlglot.parse_one("SELECT * FROM t", read="sqlite").find(exp.Table)
        with pytest.raises(RowFilterRejectedError, match="no fence"):
            row_filter_rewrite._filtered(table, parse_condition("x = 1", "sqlite"), "sqlite")

    @pytest.mark.parametrize(
        ("read", "fence"),
        [("postgres", " OFFSET 0)"), ("mysql", " LIMIT 18446744073709551615)")],
    )
    def test_the_fence_per_dialect(self, read: str, fence: str) -> None:
        table = sqlglot.parse_one("SELECT * FROM t", read=read).find(exp.Table)
        out = row_filter_rewrite._filtered(table, parse_condition("x = 1", read), read)
        assert fence in out.sql(dialect=read)
