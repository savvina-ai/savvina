# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _validate_columns_against_schema() in chat_service."""

from __future__ import annotations

from app.datasources.models import ColumnInfo, DataSourceSchema, SchemaInfo, TableInfo
from app.services.schema_utils import _validate_columns_against_schema


def _make_schema() -> DataSourceSchema:
    """Minimal schema with public.customers and public.orders tables."""
    customers = TableInfo(
        catalog=None,
        schema_name="public",
        name="customers",
        table_type="table",
        columns=[
            ColumnInfo(name="id", data_type="integer", native_type="int4"),
            ColumnInfo(name="email", data_type="text", native_type="text"),
        ],
    )
    orders = TableInfo(
        catalog=None,
        schema_name="public",
        name="orders",
        table_type="table",
        columns=[
            ColumnInfo(name="id", data_type="integer", native_type="int4"),
            ColumnInfo(name="customer_id", data_type="integer", native_type="int4"),
            ColumnInfo(name="total_amount", data_type="numeric", native_type="numeric"),
            ColumnInfo(name="status", data_type="text", native_type="text"),
        ],
    )
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[customers, orders],
    )


# ── Valid queries — should all return None ────────────────────────────────────


class TestValidColumns:
    def test_simple_valid_query(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT c.id FROM public.customers c", _make_schema()
        )
        assert result is None

    def test_join_valid_columns(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT c.email, o.total_amount "
            "FROM public.customers c "
            "JOIN orders o ON c.id = o.customer_id",
            _make_schema(),
        )
        assert result is None

    def test_schema_qualified_columns(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT public.customers.id FROM public.customers",
            _make_schema(),
        )
        assert result is None

    def test_empty_schema_returns_none(self) -> None:
        empty = DataSourceSchema(source_type="postgresql")
        result = _validate_columns_against_schema("SELECT x.nonexistent FROM x", empty)
        assert result is None


# ── Missing table detection ───────────────────────────────────────────────────


class TestMissingTable:
    def test_nonexistent_table_detected(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT x.id FROM public.nonexistent x", _make_schema()
        )
        assert result is not None
        assert "nonexistent" in result

    def test_cte_not_flagged_as_missing(self) -> None:
        result = _validate_columns_against_schema(
            "WITH cte AS (SELECT id FROM customers) SELECT id FROM cte",
            _make_schema(),
        )
        assert result is None

    def test_two_ctes_not_flagged(self) -> None:
        result = _validate_columns_against_schema(
            "WITH a AS (SELECT c.id FROM customers c), "
            "b AS (SELECT o.id FROM orders o) "
            "SELECT a.id FROM a JOIN b ON a.id = b.id",
            _make_schema(),
        )
        assert result is None

    def test_hallucinated_join_table_detected(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT ca.acquisition_channel "
            "FROM customers c "
            "JOIN customer_acquisition ca ON c.id = ca.customer_id",
            _make_schema(),
        )
        assert result is not None
        assert "customer_acquisition" in result


# ── Missing column detection ──────────────────────────────────────────────────


class TestMissingColumn:
    def test_nonexistent_column_detected(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT c.acquisition_channel FROM customers c",
            _make_schema(),
        )
        assert result is not None
        assert "acquisition_channel" in result

    def test_valid_column_passes(self) -> None:
        result = _validate_columns_against_schema("SELECT o.status FROM orders o", _make_schema())
        assert result is None

    def test_unresolvable_alias_skipped(self) -> None:
        # alias 'x' is not registered in alias_map — must not produce a false positive
        result = _validate_columns_against_schema(
            "SELECT x.anything FROM customers c", _make_schema()
        )
        assert result is None

    def test_nonexistent_column_error_includes_available_columns(self) -> None:
        """Error message must list the columns that ARE available in the table."""
        result = _validate_columns_against_schema(
            "SELECT c.nonexistent FROM customers c",
            _make_schema(),
        )
        assert result is not None
        assert "nonexistent" in result
        assert "(available:" in result
        # The actual columns of customers are id and email
        assert "id" in result
        assert "email" in result


# ── String literal false-positive prevention ──────────────────────────────────


class TestStringLiterals:
    def test_dot_in_string_literal_no_false_positive(self) -> None:
        # 'orders.customer_id' is a string value — must not be treated as a column ref
        result = _validate_columns_against_schema(
            "SELECT c.id FROM customers c WHERE c.email = 'orders.customer_id'",
            _make_schema(),
        )
        assert result is None

    def test_nonexistent_pattern_in_string_no_false_positive(self) -> None:
        # 'fake.col' looks like a bad column ref but is just a string literal
        result = _validate_columns_against_schema(
            "SELECT c.id FROM customers c WHERE c.email = 'fake.col'",
            _make_schema(),
        )
        assert result is None


# ── EXTRACT / TRIM / SUBSTRING false-positive prevention ─────────────────────
# These SQL functions use FROM as a keyword inside their arguments.
# The validator must not treat those arguments as table names.


class TestExtractFunctionFalsePositives:
    def test_extract_current_date_not_flagged(self) -> None:
        # EXTRACT(QUARTER FROM CURRENT_DATE) — 'current_date' is not a table
        result = _validate_columns_against_schema(
            "SELECT o.id FROM orders o WHERE EXTRACT(QUARTER FROM CURRENT_DATE) = 1",
            _make_schema(),
        )
        assert result is None

    def test_extract_current_year_not_flagged(self) -> None:
        # EXTRACT(YEAR FROM CURRENT_DATE) — second built-in variant
        result = _validate_columns_against_schema(
            "SELECT o.id FROM orders o WHERE EXTRACT(YEAR FROM CURRENT_DATE) = 2026",
            _make_schema(),
        )
        assert result is None

    def test_extract_column_not_flagged_as_table(self) -> None:
        # EXTRACT(YEAR FROM ordered_at) — column name must not be treated as a table
        result = _validate_columns_against_schema(
            "SELECT SUM(o.total_amount) FROM orders o WHERE EXTRACT(YEAR FROM ordered_at) = 2025",
            _make_schema(),
        )
        assert result is None

    def test_extract_multiple_calls_not_flagged(self) -> None:
        # Multiple EXTRACT calls with both built-ins and column args
        result = _validate_columns_against_schema(
            "SELECT o.id FROM orders o "
            "WHERE EXTRACT(QUARTER FROM CURRENT_DATE) = EXTRACT(QUARTER FROM ordered_at) "
            "AND EXTRACT(YEAR FROM CURRENT_DATE) = EXTRACT(YEAR FROM ordered_at)",
            _make_schema(),
        )
        assert result is None

    def test_trim_from_column_not_flagged(self) -> None:
        # TRIM(LEADING chars FROM column) — column must not be treated as a table
        result = _validate_columns_against_schema(
            "SELECT TRIM(LEADING ' ' FROM c.email) FROM customers c",
            _make_schema(),
        )
        assert result is None

    def test_substring_from_column_not_flagged(self) -> None:
        # SUBSTRING(col FROM n FOR len) — col must not be treated as a table
        result = _validate_columns_against_schema(
            "SELECT SUBSTRING(c.email FROM 1 FOR 5) FROM customers c",
            _make_schema(),
        )
        assert result is None

    def test_cte_with_extract_and_cross_join(self) -> None:
        # Reproduces the exact failure: two CTEs with EXTRACT in WHERE + CROSS JOIN
        result = _validate_columns_against_schema(
            "WITH last_month AS ("
            "  SELECT SUM(o.total_amount) AS revenue"
            "  FROM orders o"
            "  WHERE EXTRACT(YEAR FROM CURRENT_DATE) = EXTRACT(YEAR FROM ordered_at)"
            "    AND EXTRACT(MONTH FROM CURRENT_DATE) - 1 = EXTRACT(MONTH FROM ordered_at)"
            "), month_before AS ("
            "  SELECT SUM(o.total_amount) AS revenue"
            "  FROM orders o"
            "  WHERE EXTRACT(YEAR FROM CURRENT_DATE) = EXTRACT(YEAR FROM ordered_at)"
            "    AND EXTRACT(MONTH FROM CURRENT_DATE) - 2 = EXTRACT(MONTH FROM ordered_at)"
            ") "
            "SELECT l.revenue, m.revenue "
            "FROM last_month l CROSS JOIN month_before m",
            _make_schema(),
        )
        assert result is None


# ── Schemaless tables (schema_name is None) ───────────────────────────────────
# Tables without a schema namespace must not crash the validator and must still
# resolve unqualified table references.


class TestSchemalessTables:
    @staticmethod
    def _make_schemaless_schema() -> DataSourceSchema:
        diamonds = TableInfo(
            catalog=None,
            schema_name=None,
            name="diamonds",
            table_type="table",
            columns=[
                ColumnInfo(name="cut", data_type="text", native_type="VARCHAR"),
                ColumnInfo(name="price", data_type="integer", native_type="INTEGER"),
                ColumnInfo(name="carat", data_type="double", native_type="DOUBLE"),
            ],
        )
        return DataSourceSchema(source_type="postgresql", tables=[diamonds])

    def test_unqualified_select_does_not_crash(self) -> None:
        # Reproduces the AttributeError: 'NoneType' object has no attribute 'lower'
        result = _validate_columns_against_schema(
            "SELECT cut, AVG(price::DOUBLE / carat) FROM diamonds GROUP BY cut",
            self._make_schemaless_schema(),
        )
        assert result is None

    def test_cte_over_schemaless_table(self) -> None:
        result = _validate_columns_against_schema(
            "WITH cut_stats AS ("
            "  SELECT cut, AVG(price::DOUBLE / carat) AS avg_price_per_carat"
            "  FROM diamonds GROUP BY cut"
            ") "
            "SELECT RANK() OVER (ORDER BY avg_price_per_carat DESC), cut, avg_price_per_carat "
            "FROM cut_stats ORDER BY 1",
            self._make_schemaless_schema(),
        )
        assert result is None

    def test_missing_table_still_detected(self) -> None:
        result = _validate_columns_against_schema(
            "SELECT id FROM nonexistent_table",
            self._make_schemaless_schema(),
        )
        assert result is not None
        assert "nonexistent_table" in result
