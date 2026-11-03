# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""apply_row_filters: every filtered base-table reference becomes a filtered derived table,
or the statement is rejected before it runs."""

# ruff: noqa: S608 — every string here is a test query, never executed.

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, UnsupportedError

from app.datasources.errors import ErrorCategory
from app.services import row_filter_rewrite
from app.services.error_feedback import describe_error_for_llm
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


def _assert_advice(exc: RowFilterRejectedError, advice: str) -> None:
    """A refusal the model can fix carries an advice-only, quote-free hint, so the
    correction loop still runs (a hint-less refusal ends it)."""
    hint = exc.llm_hint
    assert hint is not None
    assert advice in hint
    assert str(exc) not in hint
    assert "'" not in hint and '"' not in hint


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
        # The hint carries the quoted form: unquoted, PostgreSQL folds the name and refuses
        # it again.
        assert excinfo.value.llm_hint == (
            'Write the table orders as "public"."Orders", with that exact case and quoting.'
        )

    def test_mysql_case_variant_hint_uses_backticks(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT id FROM orders", {"public.Orders": _COND}, "MySQL")
        _assert_plain(excinfo.value)
        assert excinfo.value.llm_hint == (
            "Write the table orders as `public`.`Orders`, with that exact case and quoting."
        )

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
        _assert_advice(excinfo.value, "exactly one SELECT statement")

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
        _assert_advice(excinfo.value, "standard SELECT constructs")

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
        _assert_advice(excinfo.value, "filtered table public.customers directly")

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
            # Unqualified, the name is a table of the current database, which can be the
            # information_schema or mysql schema itself.
            ("MySQL", "SELECT histogram FROM column_statistics"),
            ("MySQL", "SELECT histogram FROM COLUMN_STATS"),
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
        _assert_advice(excinfo.value, "one plain SELECT statement")

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


class TestAParsedTree:
    def test_a_given_tree_is_rewritten_without_parsing(self) -> None:
        sql = "SELECT id FROM users"
        tree = sqlglot.parse_one(sql, read="postgres", error_level=ErrorLevel.IMMEDIATE)
        with patch("app.services.row_filter_rewrite._parse_statement") as parse:
            out = apply_row_filters(sql, {"public.users": "tenant_id = 7"}, "postgresql", tree=tree)
        parse.assert_not_called()
        assert out == apply_row_filters(sql, {"public.users": "tenant_id = 7"}, "postgresql")

    def test_a_recursion_error_on_a_given_tree_is_still_a_rejection(self) -> None:
        sql = "SELECT id FROM users"
        tree = sqlglot.parse_one(sql, read="postgres", error_level=ErrorLevel.IMMEDIATE)
        with (
            patch(
                "app.services.row_filter_rewrite._reject_unfilterable", side_effect=RecursionError
            ),
            pytest.raises(RowFilterRejectedError, match="nested too deeply"),
        ):
            apply_row_filters(sql, {"public.users": "tenant_id = 7"}, "postgresql", tree=tree)


class TestHintsCarryOnlyTheAdvice:
    """The REJECTED correction prompt already carries the rejection text; a hint that
    repeated it would show the model the same sentence twice. Each hint still reads on its
    own: it names what to change."""

    @pytest.mark.parametrize(
        ("dialect", "sql", "filters", "advice"),
        [
            pytest.param(
                "PostgreSQL",
                "SELECT query_to_xml('SELECT 1', true, false, '')",
                {"public.orders": _COND},
                "without the function query_to_xml",
                id="function",
            ),
            pytest.param(
                "PostgreSQL",
                "SELECT * FROM pg_catalog.pg_stats",
                {"public.orders": _COND},
                "without the catalog view pg_stats",
                id="catalog-view",
            ),
            pytest.param(
                "PostgreSQL",
                "WITH orders AS (SELECT 1 AS id) SELECT id FROM orders",
                {"public.orders": _COND},
                "Rename the CTE",
                id="cte",
            ),
            pytest.param(
                "PostgreSQL",
                "SELECT id FROM orders",
                {"a.orders": _COND, "b.orders": _COND},
                "Qualify the table orders as schema.table",
                id="ambiguous",
            ),
            pytest.param(
                "PostgreSQL",
                "SELECT id FROM Orders",
                {"public.Orders": _COND},
                '"public"."Orders"',
                id="case-variant",
            ),
        ],
    )
    def test_the_hint_does_not_repeat_the_message(
        self, dialect: str, sql: str, filters: dict[str, str], advice: str
    ) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, filters, dialect)
        hint = excinfo.value.llm_hint
        assert hint is not None
        assert str(excinfo.value) not in hint
        assert advice in hint

    def test_the_nesting_hint_does_not_repeat_the_message(self) -> None:
        with (
            patch(
                "app.services.row_filter_rewrite._reject_unfilterable", side_effect=RecursionError
            ),
            pytest.raises(RowFilterRejectedError) as excinfo,
        ):
            apply_row_filters("SELECT id FROM orders", {"public.orders": _COND}, "PostgreSQL")
        hint = excinfo.value.llm_hint
        assert hint is not None
        assert str(excinfo.value) not in hint
        assert "Simplify the query" in hint

    def test_the_correction_description_carries_the_message_once(self) -> None:
        sql = "WITH orders AS (SELECT 1 AS id) SELECT id FROM orders"
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, {"public.orders": _COND}, "PostgreSQL")
        exc = excinfo.value
        described = describe_error_for_llm(
            ErrorCategory.REJECTED, str(exc), sql=sql, schema=None, hint=exc.llm_hint
        )
        assert described.count(str(exc)) == 1
        assert exc.llm_hint is not None and exc.llm_hint in described


def _condition_tree(out: str, dialect: str, alias: str) -> exp.Expression:
    """The admin's condition inside the derived table *alias* of *out*, detached (a copy) so
    that ``find_ancestor`` stops at the condition."""
    select = _derived(out, dialect)[alias]
    where = select.args["where"].this
    assert isinstance(where, exp.Paren)
    return where.this.copy()


def _unqualified(cond: exp.Expression) -> list[str]:
    """Columns of *cond*, outside any nested query, that carry no table qualifier."""
    return [
        c.name
        for c in cond.find_all(exp.Column)
        if not c.table and c.find_ancestor(exp.Query) is None
    ]


@pytest.mark.parametrize("dialect", _DIALECTS)
class TestConditionColumnsAreQualifiedInsideASubquery:
    """Inside a subquery an unqualified column of the condition that the table lacks (a
    column dropped after the save) resolves to an outer query's column of that name, so
    every row passes. Qualified with the table name it errors instead."""

    _OUTER = "SELECT * FROM (SELECT 7 AS org_id) s WHERE EXISTS (SELECT 1 FROM orders)"

    def test_a_correlated_exists_gets_the_qualified_condition(self, dialect: str) -> None:
        out = apply_row_filters(
            self._OUTER, _filters(dialect, "orders", cond="org_id = 7"), dialect
        )
        cond = _condition_tree(out, dialect, "orders")
        assert _unqualified(cond) == []
        column = cond.find(exp.Column)
        assert (column.table, column.name) == ("orders", "org_id")

    def test_the_qualifier_is_the_table_not_the_query_alias(self, dialect: str) -> None:
        sql = "SELECT id FROM t WHERE EXISTS (SELECT 1 FROM orders o WHERE o.id = t.id)"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        cond = _condition_tree(out, dialect, "o")
        assert {c.table for c in cond.find_all(exp.Column)} == {"orders"}

    def test_a_scalar_subquery_in_the_select_list_is_qualified(self, dialect: str) -> None:
        sql = "SELECT (SELECT COUNT(*) FROM orders) AS n FROM t"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _unqualified(_condition_tree(out, dialect, "orders")) == []

    def test_a_cte_body_is_qualified(self, dialect: str) -> None:
        sql = "WITH r AS (SELECT * FROM orders) SELECT * FROM r"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _unqualified(_condition_tree(out, dialect, "orders")) == []

    def test_columns_of_a_nested_select_in_the_condition_are_left_alone(self, dialect: str) -> None:
        cond = "id IN (SELECT order_id FROM grants WHERE tenant_id = 7) AND kind = 'a'"
        sql = "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM orders)"
        out = apply_row_filters(sql, _filters(dialect, "orders", cond=cond), dialect)
        tree = _condition_tree(out, dialect, "orders")
        top = [c for c in tree.find_all(exp.Column) if c.find_ancestor(exp.Query) is None]
        assert {(c.table, c.name) for c in top} == {("orders", "id"), ("orders", "kind")}
        nested = [c for c in tree.find_all(exp.Column) if c.find_ancestor(exp.Query)]
        assert {c.name for c in nested} == {"order_id", "tenant_id"}
        assert all(not c.table for c in nested)

    def test_an_already_qualified_column_is_kept(self, dialect: str) -> None:
        sql = "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM orders)"
        filters = _filters(dialect, "orders", cond="orders.tenant_id = 7")
        out = apply_row_filters(sql, filters, dialect)
        column = _condition_tree(out, dialect, "orders").find(exp.Column)
        assert (column.table, column.name) == ("orders", "tenant_id")

    def test_the_top_level_condition_is_written_as_the_admin_wrote_it(self, dialect: str) -> None:
        # A derived table in the outermost FROM sees no outer query: a missing column errors
        # there anyway, and the save-time EXPLAIN plans this very form.
        out = apply_row_filters("SELECT * FROM orders", _filters(dialect, "orders"), dialect)
        assert _condition(_derived(out, dialect)["orders"], dialect) == _expected(_COND, dialect)


class TestConditionQualificationPerDialect:
    _NESTED = "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM {table})"

    def test_postgres_keeps_the_quoted_mixed_case_table_name(self) -> None:
        sql = self._NESTED.format(table='"Orders"')
        out = apply_row_filters(sql, {"public.Orders": _COND}, "PostgreSQL")
        assert '"Orders".tenant_id = 7' in out

    def test_postgres_value_keywords_are_not_qualified(self) -> None:
        # PostgreSQL reads a bare user, current_role or system_user as the session value,
        # never a column; qualified it would name a column that does not exist.
        sql = self._NESTED.format(table="orders")
        cond = "owner = user OR owner = current_role OR owner = system_user"
        out = apply_row_filters(sql, {"public.orders": cond}, "PostgreSQL")
        cond_tree = _condition_tree(out, "PostgreSQL", "orders")
        assert sorted(_unqualified(cond_tree)) == ["current_role", "system_user", "user"]
        assert "orders.owner" in out

    def test_mysql_qualifies_a_column_named_user(self) -> None:
        # On MySQL user is an ordinary column name (the function needs parentheses).
        sql = self._NESTED.format(table="orders")
        out = apply_row_filters(sql, {"sample_delivery.orders": "user = 'a'"}, "MySQL")
        assert _unqualified(_condition_tree(out, "MySQL", "orders")) == []

    def test_a_quoted_column_named_user_is_qualified_on_postgres(self) -> None:
        sql = self._NESTED.format(table="orders")
        out = apply_row_filters(sql, {"public.orders": "\"user\" = 'a'"}, "PostgreSQL")
        assert 'orders."user"' in out

    def test_the_save_time_check_statement_plans_both_forms(self) -> None:
        # routers/connections._check_statement plans the table at the top level (the
        # condition as written) and inside an EXISTS (the condition qualified), so a
        # condition that only one form accepts is refused at save time.
        from app.routers.connections import _check_statement

        planned = _check_statement("public.orders", "org_id = 7", "postgresql")
        assert planned == (
            'SELECT * FROM (SELECT * FROM "public"."orders" WHERE (org_id = 7) OFFSET 0)'
            ' AS "orders" WHERE EXISTS(SELECT 1 FROM (SELECT * FROM "public"."orders"'
            ' WHERE ("orders".org_id = 7) OFFSET 0) AS "orders")'
        )
        planned = _check_statement("shop.orders", "org_id = 7", "mysql")
        assert "WHERE (org_id = 7) LIMIT 18446744073709551615) AS `orders`" in planned
        assert "WHERE (`orders`.org_id = 7) LIMIT 18446744073709551615) AS `orders`" in planned

    def test_a_postgres_whole_row_reference_is_not_qualified(self) -> None:
        # Unqualified, orders is the whole row of the inner table (no outer query can
        # supply it first); qualified, orders.orders would name a column it does not have.
        sql = self._NESTED.format(table="orders")
        for cond, kept in [
            ("orders IS NOT NULL", "(orders IS NOT NULL)"),
            ("(orders).tenant_id = 7", "((orders).tenant_id = 7)"),
        ]:
            out = apply_row_filters(sql, {"public.orders": cond}, "PostgreSQL")
            assert kept in out and "orders.orders" not in out

    def test_mysql_a_column_named_like_its_table_is_qualified(self) -> None:
        sql = self._NESTED.format(table="orders")
        out = apply_row_filters(sql, {"sample_delivery.orders": "orders = 1"}, "MySQL")
        assert "(orders.orders = 1)" in out


class TestParseFailureHint:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM {x}",
            "SELECT 1 FROM t JOIN ,",
            "SELECT id FROM orders WHERE (",
            "SELECT * FROM t WHERE a = > 1",
        ],
    )
    def test_the_hint_carries_no_sqlglot_internals(self, sql: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, {"public.orders": _COND}, "PostgreSQL")
        hint = excinfo.value.llm_hint
        assert hint is not None
        assert hint.startswith("The row-filter rewriter could not parse the query:")
        for internal in ("<Token", "TokenType", "<class", "sqlglot", "expressions."):
            assert internal not in hint

    def test_the_hint_says_where_the_parser_stopped(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT * FROM {x}", {"public.orders": _COND}, "PostgreSQL")
        assert excinfo.value.llm_hint == (
            "The row-filter rewriter could not parse the query: the parser stopped at line 1,"
            ' column 15, near "{". Write it as one plain SELECT statement.'
        )

    def test_a_tokenizer_failure_gets_a_fixed_hint(self) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT 'abc FROM orders", {"public.orders": _COND}, "PostgreSQL")
        assert excinfo.value.llm_hint == (
            "The row-filter rewriter could not parse the query: a string or quoted name is not"
            " closed, or a character is not valid here. Write it as one plain SELECT statement."
        )


class TestMariaDBColumnStats:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT min_value, max_value FROM mysql.column_stats",
            "SELECT histogram FROM MYSQL.COLUMN_STATS WHERE table_name = 'orders'",
            "SELECT * FROM `mysql`.`column_stats`",
        ],
    )
    def test_mariadb_column_stats_is_rejected(self, sql: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        _assert_plain(excinfo.value)
        assert "catalog view column_stats exposes values" in str(excinfo.value)
        _assert_advice(excinfo.value, "without the catalog view column_stats")

    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("MySQL", "SELECT * FROM sample_delivery.column_stats"),
            ("MySQL", "SELECT * FROM mysql.column_statistics_archive"),
            ("PostgreSQL", "SELECT * FROM mysql.column_stats"),
            ("MySQL", "SELECT * FROM mysql.pg_stats"),
            ("MySQL", "SELECT * FROM information_schema.column_stats"),
        ],
    )
    def test_the_same_name_elsewhere_runs(self, dialect: str, sql: str) -> None:
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _derived(out, dialect) == {}


def _names(out: str, dialect: str) -> list[str]:
    """The output names of the outermost SELECT list of *out*: the alias when there is one,
    else ``""``."""
    tree = _tree(out, dialect)
    while isinstance(tree, exp.SetOperation):
        tree = tree.this
    return [e.alias if isinstance(e, exp.Alias) else "" for e in tree.expressions]


class TestOutputNamesArePreserved:
    """sqlglot writes some functions back under another name (strpos as POSITION, now() as
    CURRENT_TIMESTAMP); the engine would name the column after the new text. An unaliased
    item of the outermost SELECT list is aliased with the name the engine gives the query
    as written, when that differs."""

    def test_postgres_function_columns_keep_the_function_name(self) -> None:
        sql = "SELECT strpos(email, '@'), now(), substr(name, 1, 2), pow(a, 2) FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert _names(out, "PostgreSQL") == ["strpos", "now", "substr", "pow"]
        assert 'AS "strpos"' in out

    def test_postgres_names_that_do_not_change_get_no_alias(self) -> None:
        sql = (
            "SELECT id, o.status, count(*), lower(email), id::text, COALESCE(a, b), 1 + 1,"
            " 'x', CURRENT_DATE, sum(total) OVER (), (id) FROM orders o"
        )
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert set(_names(out, "PostgreSQL")) == {""}

    def test_postgres_an_aliased_item_keeps_its_alias(self) -> None:
        sql = "SELECT strpos(email, '@') AS at_sign, now() AS ts FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert _names(out, "PostgreSQL") == ["at_sign", "ts"]

    def test_postgres_a_quoted_function_name_keeps_its_case(self) -> None:
        sql = "SELECT \"strpos\"(email, '@') FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert _names(out, "PostgreSQL") == ["strpos"]

    def test_postgres_a_function_as_mod_is_named_after_the_function(self) -> None:
        # mod(a, 2) is written back as a % 2, which PostgreSQL names ?column?.
        sql = "SELECT mod(a, 2), a % 2 FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert _names(out, "PostgreSQL") == ["mod", ""]

    def test_postgres_the_first_union_branch_names_the_columns(self) -> None:
        sql = "SELECT now() FROM orders UNION ALL SELECT now() FROM t"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert _names(out, "PostgreSQL") == ["now"]

    def test_postgres_an_order_by_on_the_engine_name_still_resolves(self) -> None:
        sql = "SELECT strpos(email, '@') FROM orders ORDER BY strpos"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert 'AS "strpos"' in out and out.endswith("ORDER BY strpos")

    def test_mysql_function_columns_keep_the_text_as_written(self) -> None:
        sql = "SELECT IFNULL(driver_id, 0), LOCATE('@',email), substr(name,1,2), 1+1 FROM orders"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == [
            "IFNULL(driver_id, 0)",
            "LOCATE('@',email)",
            "substr(name,1,2)",
            "1+1",
        ]

    def test_mysql_names_that_do_not_change_get_no_alias(self) -> None:
        sql = (
            "SELECT id, o.status, `kind`, COUNT(*), NOW(), 'x', \"y\", NULL, (id), -1 FROM orders o"
        )
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert set(_names(out, "MySQL")) == {""}

    def test_mysql_modifiers_and_hints_are_not_part_of_the_name(self) -> None:
        sql = (
            "SELECT /*+ MAX_EXECUTION_TIME(1) */ DISTINCT SQL_NO_CACHE IFNULL(a,0), 1+1 FROM orders"
        )
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == ["IFNULL(a,0)", "1+1"]

    def test_mysql_a_scalar_subquery_is_named_after_its_text(self) -> None:
        sql = "SELECT (SELECT MAX(id) FROM orders), id FROM t"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == ["(SELECT MAX(id) FROM orders)", ""]

    def test_mysql_a_cte_query_names_its_main_select(self) -> None:
        sql = "WITH r AS (SELECT IFNULL(a,0) AS x FROM orders) SELECT IFNULL(x,1), x FROM r"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == ["IFNULL(x,1)", ""]

    def test_mysql_a_parenthesised_first_branch_is_named_from_its_own_text(self) -> None:
        # The first SELECT outside parentheses is the second branch: its text must not name
        # the first branch's items; each branch is named from its own text.
        sql = "(SELECT CURRENT_TIME, IFNULL(b,1) FROM orders) UNION SELECT IFNULL(a,0), 2 FROM t"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert out.count("IFNULL(a,0)`") == 1
        assert "COALESCE(a, 0) AS `IFNULL(a,0)`, 2 FROM t" in out
        assert "CURRENT_TIME() AS `CURRENT_TIME`" in out
        assert "AS `IFNULL(b,1)`" in out

    def test_mysql_a_comment_inside_an_item_leaves_it_unaliased(self) -> None:
        sql = "SELECT IFNULL(a, /* zero */ 0) FROM orders"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == [""]

    def test_mysql_a_backtick_in_the_text_is_escaped(self) -> None:
        sql = "SELECT IFNULL(`a`,0) FROM orders"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "AS `IFNULL(``a``,0)`" in out
        assert _names(out, "MySQL") == ["IFNULL(`a`,0)"]

    def test_mysql_an_item_longer_than_an_alias_may_be_is_left_alone(self) -> None:
        item = "IFNULL(a," + "+".join(["1"] * 200) + ")"
        out = apply_row_filters(f"SELECT {item} FROM orders", _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == [""]

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_a_given_tree_gets_the_same_names(self, dialect: str) -> None:
        sql = "SELECT substr(name,1,2) FROM orders"
        tree = sqlglot.parse_one(sql, read=_READ[dialect], error_level=ErrorLevel.IMMEDIATE)
        filters = _filters(dialect, "orders")
        assert apply_row_filters(sql, filters, dialect, tree=tree) == apply_row_filters(
            sql, filters, dialect
        )


@pytest.mark.parametrize("dialect", _DIALECTS)
class TestSchemaQualifiedColumns:
    """``schema.table.column`` names a table that the rewrite turns into a derived table
    aliased with the bare table name; the column is rewritten to ``table.column``."""

    def test_a_fully_qualified_column_is_rewritten_to_the_alias(self, dialect: str) -> None:
        s = _SCHEMA[dialect]
        sql = f"SELECT {s}.orders.id FROM {s}.orders WHERE {s}.orders.status = 'x'"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        tree = _tree(out, dialect)
        outer = [c for c in tree.find_all(exp.Column) if c.find_ancestor(exp.Subquery) is None]
        assert {(c.db, c.table, c.name) for c in outer} == {
            ("", "orders", "id"),
            ("", "orders", "status"),
        }

    def test_a_bare_table_reference_with_a_qualified_column(self, dialect: str) -> None:
        s = _SCHEMA[dialect]
        out = apply_row_filters(
            f"SELECT {s}.orders.id FROM orders", _filters(dialect, "orders"), dialect
        )
        column = _tree(out, dialect).expressions[0]
        assert (column.db, column.table) == ("", "orders")

    def test_a_qualified_column_of_an_unfiltered_table_is_kept(self, dialect: str) -> None:
        s = _SCHEMA[dialect]
        sql = f"SELECT {s}.items.id FROM {s}.items JOIN {s}.orders o ON o.id = {s}.items.oid"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        dbs = {c.db for c in _tree(out, dialect).find_all(exp.Column) if c.table == "items"}
        assert dbs == {s}

    def test_another_schema_with_the_same_table_name_is_kept(self, dialect: str) -> None:
        sql = "SELECT archive.orders.id FROM archive.orders"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _tree(out, dialect).expressions[0].db == "archive"
        assert _derived(out, dialect) == {}

    def test_an_aliased_filtered_table_is_left_to_the_engine(self, dialect: str) -> None:
        # The engine already refuses schema.table.column against an alias; nothing to save.
        s = _SCHEMA[dialect]
        sql = f"SELECT {s}.orders.id FROM {s}.orders o"
        out = apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        assert _tree(out, dialect).expressions[0].db == s

    def test_another_source_with_the_same_name_is_rejected(self, dialect: str) -> None:
        s = _SCHEMA[dialect]
        sql = (
            f"SELECT {s}.orders.id FROM {s}.orders"
            f" WHERE EXISTS (SELECT 1 FROM customers orders WHERE orders.x = {s}.orders.id)"
        )
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters(dialect, "orders"), dialect)
        _assert_plain(excinfo.value)
        assert "another table in the query has the same name" in str(excinfo.value)
        _assert_advice(excinfo.value, f"Give the table {s}.orders an alias")

    def test_the_condition_is_not_rewritten(self, dialect: str) -> None:
        s = _SCHEMA[dialect]
        filters = _filters(dialect, "orders", cond=f"{s}.orders.tenant_id = 7")
        out = apply_row_filters(f"SELECT {s}.orders.id FROM {s}.orders", filters, dialect)
        cond = _condition_tree(out, dialect, "orders").find(exp.Column)
        assert (cond.db, cond.table) == (s, "orders")


class TestOutputNamesInsideTheQuery:
    """A CTE body, a derived table, a scalar subquery and the first branch of a set
    operation name columns an outer query reads: they keep the engine's names too."""

    def test_postgres_a_cte_body_keeps_its_names(self) -> None:
        sql = "WITH x AS (SELECT now() FROM orders) SELECT now FROM x"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert 'CURRENT_TIMESTAMP AS "now"' in out
        assert out.endswith("SELECT now FROM x")

    def test_postgres_a_derived_table_keeps_its_names(self) -> None:
        sql = "SELECT d.strpos FROM (SELECT strpos(email, '@') FROM orders) AS d"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert "POSITION('@' IN email) AS \"strpos\"" in out

    def test_postgres_a_scalar_subquery_keeps_its_name(self) -> None:
        # PostgreSQL names the outer column after the subquery's column.
        sql = "SELECT (SELECT max(substr(name, 1, 1)) FROM orders), (SELECT now() FROM orders)"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert 'CURRENT_TIMESTAMP AS "now"' in out

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH x(n) AS (SELECT now() FROM orders) SELECT n FROM x",
            "SELECT d.n FROM (SELECT now() FROM orders) AS d(n)",
            "SELECT id FROM t WHERE id IN (SELECT strpos(email, '@') FROM orders)",
            "SELECT id FROM t WHERE EXISTS (SELECT now() FROM orders)",
            "SELECT 1 AS a UNION ALL SELECT strpos(email, '@') FROM orders",
        ],
    )
    def test_postgres_every_select_keeps_its_names(self, sql: str) -> None:
        # Where no outer query reads the names the alias is harmless; it keeps a GROUP BY,
        # ORDER BY or HAVING on the engine's name working inside that SELECT.
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert ' AS "now"' in out or ' AS "strpos"' in out

    def test_postgres_a_group_by_inside_an_in_subquery_resolves(self) -> None:
        sql = (
            "SELECT id FROM t WHERE id IN (SELECT strpos(email, '@') FROM orders"
            " GROUP BY strpos HAVING strpos > 0 ORDER BY strpos)"
        )
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert "POSITION('@' IN email) AS \"strpos\"" in out

    def test_mysql_a_cte_body_keeps_its_names(self) -> None:
        sql = "WITH r AS (SELECT IFNULL(a,0), id FROM orders) SELECT * FROM r"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "COALESCE(a, 0) AS `IFNULL(a,0)`, id" in out

    def test_mysql_a_derived_table_behind_a_scalar_subquery_item(self) -> None:
        # The derived table's first item holds a SELECT of its own: the list is still found
        # from the derived table's own SELECT.
        sql = "SELECT d.* FROM (SELECT (SELECT MAX(id) FROM t), IFNULL(a,0) FROM orders) AS d"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "AS `IFNULL(a,0)`" in out
        assert "AS `(SELECT MAX(id) FROM t)`" not in out  # written back as written

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH r(x) AS (SELECT IFNULL(a,0) FROM orders) SELECT x FROM r",
            "SELECT id FROM t WHERE id IN (SELECT IFNULL(a,0) FROM orders)",
            "SELECT 1 AS a UNION ALL SELECT IFNULL(a,0) FROM orders",
        ],
    )
    def test_mysql_every_select_keeps_its_names(self, sql: str) -> None:
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "AS `IFNULL(a,0)`" in out

    def test_mysql_a_derived_alias_is_capped_at_the_column_name_limit(self) -> None:
        # A derived table or CTE column name may be 64 characters; a result column 256.
        item = "IFNULL(a," + "+".join(["1"] * 30) + ")"
        assert 64 < len(item) <= 256
        sql = f"WITH r AS (SELECT {item} FROM orders) SELECT {item} FROM r"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert out.count(f"AS `{item}`") == 1
        assert out.endswith(f"AS `{item}` FROM r")


class TestNiladicFunctions:
    """Functions written without parentheses must be written back as functions, not as
    quoted columns or with parentheses the engine refuses."""

    def test_mysql_utc_functions_stay_functions(self) -> None:
        sql = "SELECT UTC_DATE, utc_time, UTC_TIMESTAMP FROM orders WHERE d < UTC_DATE"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "`" not in out.split(" FROM ")[0]
        assert out.startswith("SELECT UTC_DATE, utc_time, UTC_TIMESTAMP FROM")
        assert out.endswith("WHERE d < UTC_DATE")

    def test_mysql_utc_functions_with_parentheses(self) -> None:
        sql = "SELECT utc_date(), UTC_TIME(3), utc_timestamp(6) FROM orders"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert "AT TIME ZONE" not in out
        assert out.startswith("SELECT UTC_DATE() AS `utc_date()`, UTC_TIME(3)")

    def test_mysql_a_quoted_or_qualified_column_of_that_name_stays_a_column(self) -> None:
        sql = "SELECT `utc_date`, o.utc_time FROM orders o"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert out.startswith("SELECT `utc_date`, o.`utc_time` FROM")

    @pytest.mark.parametrize(
        ("dialect", "keyword", "written"),
        [
            ("MySQL", "CURRENT_DATE", "CURRENT_DATE"),
            ("MySQL", "CURRENT_TIME", "CURRENT_TIME()"),
            ("MySQL", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"),
            ("MySQL", "CURRENT_USER", "CURRENT_USER()"),
            ("MySQL", "LOCALTIME", "LOCALTIME"),
            ("MySQL", "LOCALTIMESTAMP", "LOCALTIMESTAMP"),
            ("MySQL", "UTC_DATE", "UTC_DATE"),
            ("PostgreSQL", "current_date", "CURRENT_DATE"),
            ("PostgreSQL", "current_time", "CURRENT_TIME"),
            ("PostgreSQL", "current_time(2)", "CURRENT_TIME(2)"),
            ("PostgreSQL", "current_timestamp", "CURRENT_TIMESTAMP"),
            ("PostgreSQL", "current_user", "CURRENT_USER"),
            ("PostgreSQL", "localtime", "LOCALTIME"),
            ("PostgreSQL", "localtimestamp", "LOCALTIMESTAMP"),
        ],
    )
    def test_in_a_condition_inside_a_subquery(
        self, dialect: str, keyword: str, written: str
    ) -> None:
        sql = "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM orders)"
        out = apply_row_filters(sql, _filters(dialect, "orders", cond=f"k < {keyword}"), dialect)
        assert f"WHERE (orders.k < {written})" in out

    def test_postgres_current_time_has_no_empty_parentheses(self) -> None:
        sql = "SELECT current_time, x FROM orders WHERE t < current_time"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert "CURRENT_TIME()" not in out
        assert _names(out, "PostgreSQL") == ["", ""]


class TestDatePart:
    """sqlglot reads date_part(text, x) as EXTRACT(unit FROM x), which PostgreSQL 14 and
    later names extract (and types numeric, not double precision): it is written back as
    the date_part call it was."""

    def test_date_part_is_written_back_as_written(self) -> None:
        sql = "SELECT date_part('year', ordered_at), DATE_PART( 'Month' , (x + 1)) FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert out.startswith("SELECT date_part('year', ordered_at), DATE_PART('Month', (x + 1))")
        assert "EXTRACT" not in out

    def test_date_part_in_a_where_clause_too(self) -> None:
        sql = "SELECT id FROM orders WHERE date_part('dow', ordered_at) = 0"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert "WHERE date_part('dow', ordered_at) = 0" in out

    def test_a_filtered_table_inside_the_argument_is_still_filtered(self) -> None:
        sql = "SELECT date_part('year', COALESCE(d, (SELECT max(ordered_at) FROM orders)))"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert out.startswith("SELECT date_part('year', COALESCE(d, (SELECT MAX(ordered_at) FROM (")
        _assert_every_reference_filtered(out, "PostgreSQL", {"orders"})

    def test_extract_stays_extract(self) -> None:
        sql = "SELECT extract(year from ordered_at) FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert out.startswith("SELECT EXTRACT(YEAR FROM ordered_at) FROM")

    def test_date_part_over_an_argument_without_a_position_stays_extract(self) -> None:
        # Residual: sqlglot records no position for CURRENT_DATE, so the call cannot be
        # told from EXTRACT(DAY FROM CURRENT_DATE).
        sql = "SELECT date_part('day', current_date) FROM orders"
        out = apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        assert out.startswith("SELECT EXTRACT(DAY FROM CURRENT_DATE) FROM")


class TestMySQLHeadersNeverCarryTheCondition:
    """MySQL names an unaliased item after its text: an item holding a filtered table whose
    text is not known would be named after the rewritten text, condition included."""

    def test_an_item_starting_with_a_function_without_a_position(self) -> None:
        sql = "SELECT IF((SELECT COUNT(*) FROM orders) > 0, 1, 0) FROM t"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        assert _names(out, "MySQL") == ["IF((SELECT COUNT(*) FROM orders) > 0, 1, 0)"]

    def test_an_item_with_a_comment_gets_the_clean_text(self) -> None:
        sql = "SELECT IFNULL(/* c */ (SELECT MAX(id) FROM orders), 0) FROM t"
        out = apply_row_filters(sql, _filters("MySQL", "orders"), "MySQL")
        [name] = _names(out, "MySQL")
        assert name == "COALESCE((SELECT MAX(id) FROM orders), 0)"

    def test_a_long_item_gets_the_clean_text_cut_to_64_characters(self) -> None:
        item = "IFNULL((SELECT MAX(id) FROM orders)," + "+".join(["1"] * 120) + ")"
        out = apply_row_filters(f"SELECT {item} FROM t", _filters("MySQL", "orders"), "MySQL")
        [name] = _names(out, "MySQL")
        assert name == "COALESCE((SELECT MAX(id) FROM orders), 1 + 1 + 1 + 1 + 1 + 1 + 1 "[:64]
        assert "tenant_id" not in name

    def test_an_item_without_a_filtered_table_and_unknown_text_is_left_alone(self) -> None:
        out = apply_row_filters(
            "SELECT IFNULL(/* c */ a, 0) FROM orders", _filters("MySQL", "orders"), "MySQL"
        )
        assert _names(out, "MySQL") == [""]


class TestCatalogQualifiedColumns:
    def test_a_catalog_qualified_column_of_a_filtered_table_is_refused(self) -> None:
        # PostgreSQL refuses a cross-database reference; dropping the catalog would run it.
        sql = "SELECT db.public.orders.id FROM public.orders"
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
        _assert_plain(excinfo.value)
        assert "with a database name" in str(excinfo.value)
        _assert_advice(excinfo.value, "Qualify the columns of the table public.orders")

    def test_a_schema_qualified_column_still_runs(self) -> None:
        out = apply_row_filters(
            "SELECT public.orders.id FROM public.orders",
            _filters("PostgreSQL", "orders"),
            "PostgreSQL",
        )
        assert out.startswith("SELECT orders.id FROM")


def test_an_unaliased_table_function_counts_as_a_source() -> None:
    # PostgreSQL names an unaliased table function after the function: orders.id could
    # reach it.
    sql = "SELECT public.orders.id FROM public.orders CROSS JOIN orders(1)"
    with pytest.raises(RowFilterRejectedError) as excinfo:
        apply_row_filters(sql, _filters("PostgreSQL", "orders"), "PostgreSQL")
    assert "another table in the query has the same name" in str(excinfo.value)


class TestFunctionNamesAreKept:
    """sqlglot upper-cases a function it does not know and maps a known name to its own
    function whatever the quoting; PostgreSQL calls a quoted name with that exact case."""

    @pytest.mark.parametrize(
        ("written", "kept"),
        [
            ('"MyFunc"(x)', '"MyFunc"(x)'),
            ('"myfn"(x)', '"myfn"(x)'),
            ('"MyAgg"(x) OVER ()', '"MyAgg"(x) OVER ()'),
            ("myfn(x)", "myfn(x)"),
            ('"lower"(x)', "LOWER(x)"),
        ],
    )
    def test_postgres_a_quoted_function_name_keeps_its_case(self, written: str, kept: str) -> None:
        out = apply_row_filters(
            f"SELECT {written} AS v FROM orders", _filters("PostgreSQL", "orders"), "PostgreSQL"
        )
        assert out.startswith(f"SELECT {kept} AS v FROM")

    @pytest.mark.parametrize("written", ['"Lower"(x)', '"LOWER"(x)', '"STRPOS"(a, b)', '"Now"()'])
    def test_postgres_a_quoted_mixed_case_builtin_name_is_refused(self, written: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters(
                f"SELECT {written} FROM orders", _filters("PostgreSQL", "orders"), "PostgreSQL"
            )
        _assert_plain(excinfo.value)
        assert "quoted function name" in str(excinfo.value)
        assert (
            excinfo.value.llm_hint is not None and written.split("(")[0] in excinfo.value.llm_hint
        )

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_a_brace_struct_is_refused(self, dialect: str) -> None:
        with pytest.raises(RowFilterRejectedError) as excinfo:
            apply_row_filters("SELECT {x} FROM orders", _filters(dialect, "orders"), dialect)
        _assert_plain(excinfo.value)
        _assert_advice(excinfo.value, "without braces")

    def test_a_struct_function_call_runs(self) -> None:
        out = apply_row_filters(
            "SELECT struct(1) AS s FROM orders", _filters("PostgreSQL", "orders"), "PostgreSQL"
        )
        assert out.startswith("SELECT STRUCT(1) AS s FROM")


@pytest.mark.parametrize(
    ("dialect", "cond"), [("PostgreSQL", "\"Lower\"(name) = 'a'"), ("MySQL", "x = {y}")]
)
def test_a_condition_that_cannot_be_written_back_is_refused_without_hint(
    dialect: str, cond: str
) -> None:
    with pytest.raises(RowFilterRejectedError) as excinfo:
        apply_row_filters("SELECT id FROM orders", _filters(dialect, "orders", cond=cond), dialect)
    _assert_plain(excinfo.value)
    assert "cannot write back for this dialect" in str(excinfo.value)
    assert excinfo.value.llm_hint is None
