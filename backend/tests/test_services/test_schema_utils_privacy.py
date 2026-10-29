# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``_apply_privacy_to_schema`` is the one narrowing every schema consumer passes
through, so it must drop everything the policy hides: schemas, tables, the
relationships between them, and ``excluded_columns``."""

from __future__ import annotations

from app.datasources.adapters.postgresql import PostgreSQLDataSource
from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from app.services.schema_utils import (
    _apply_privacy_to_schema,
    _check_query_complexity,
    _schema_from_dict,
    _schema_to_dict,
)


def _table(schema: str, name: str, cols: list[str]) -> TableInfo:
    return TableInfo(
        catalog=None,
        schema_name=schema,
        name=name,
        table_type="table",
        columns=[ColumnInfo(name=c, data_type="text", native_type="text") for c in cols],
    )


def _schema() -> DataSourceSchema:
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="store"), SchemaInfo(name="hr")],
        tables=[
            _table("store", "orders", ["id", "amount", "secret_ts"]),
            _table("store", "customers", ["id", "email"]),
            _table("hr", "people", ["id"]),
        ],
        relationships=[
            RelationshipInfo(
                from_schema="store",
                from_table="orders",
                from_column="id",
                to_schema="store",
                to_table="customers",
                to_column="id",
            ),
            RelationshipInfo(
                from_schema="store",
                from_table="orders",
                from_column="id",
                to_schema="hr",
                to_table="people",
                to_column="id",
            ),
        ],
    )


def _columns(schema: DataSourceSchema, key: str) -> list[str]:
    return [c.name for t in schema.tables if f"{t.schema_name}.{t.name}" == key for c in t.columns]


class TestApplyPrivacyToSchema:
    def test_excluded_columns_are_removed_by_exact_name(self) -> None:
        privacy = PrivacySettings(excluded_columns=["store.orders.secret_ts"])
        narrowed = _apply_privacy_to_schema(_schema(), privacy)
        assert _columns(narrowed, "store.orders") == ["id", "amount"]
        assert _columns(narrowed, "store.customers") == ["id", "email"]

    def test_sensitive_name_patterns_are_not_applied(self) -> None:
        """Same rule as the adapters: only exact ``schema.table.column`` entries. The
        sensitive patterns (``email``, ``secret``) mark columns, they do not hide them."""
        narrowed = _apply_privacy_to_schema(_schema(), PrivacySettings())
        assert _columns(narrowed, "store.orders") == ["id", "amount", "secret_ts"]
        assert _columns(narrowed, "store.customers") == ["id", "email"]

    def test_excluded_schemas_tables_and_their_relationships_go(self) -> None:
        privacy = PrivacySettings(excluded_schemas=["hr"])
        narrowed = _apply_privacy_to_schema(_schema(), privacy)
        assert [s.name for s in narrowed.schemas] == ["store"]
        assert {f"{t.schema_name}.{t.name}" for t in narrowed.tables} == {
            "store.orders",
            "store.customers",
        }
        assert [(r.to_schema, r.to_table) for r in narrowed.relationships] == [
            ("store", "customers")
        ]

    def test_input_schema_is_not_mutated(self) -> None:
        original = _schema()
        _apply_privacy_to_schema(
            original, PrivacySettings(excluded_columns=["store.orders.secret_ts"])
        )
        assert _columns(original, "store.orders") == ["id", "amount", "secret_ts"]

    def test_a_relationship_on_an_excluded_column_goes(self) -> None:
        """An FK naming an excluded column at either end would reveal its name to the
        LLM. ``format_schema_for_llm`` renders ``FK: col -> schema.table.col`` on the
        from-column's line, so the ``to`` end leaks there; and
        ``SemanticModelGenerator._build_relationship_graph`` puts both columns into the
        ``join_sql`` and description of edges that reach the generation prompts, so the
        ``from`` end leaks there."""
        privacy = PrivacySettings(excluded_columns=["store.customers.id"])
        narrowed = _apply_privacy_to_schema(_schema(), privacy)
        assert [(r.to_schema, r.to_table) for r in narrowed.relationships] == [("hr", "people")]

        privacy = PrivacySettings(excluded_columns=["store.orders.id"])
        assert _apply_privacy_to_schema(_schema(), privacy).relationships == []

    def test_a_stale_schema_cache_is_narrowed_after_deserialising(self) -> None:
        """A per-user cache written before the exclusion still holds the column; the
        narrowing applied after loading it removes the column."""
        stale = _schema_from_dict(_schema_to_dict(_schema()))
        assert "secret_ts" in _columns(stale, "store.orders")
        privacy = PrivacySettings(excluded_columns=["store.orders.secret_ts"])
        assert _columns(_apply_privacy_to_schema(stale, privacy), "store.orders") == [
            "id",
            "amount",
        ]


class TestUnreadableTables:
    """``unreadable_tables`` must survive the schema cache and the privacy narrowing,
    or drift and globals would read an unreadable table as dropped again."""

    def test_round_trips_through_the_schema_cache(self) -> None:
        schema = _schema()
        schema.unreadable_tables = ["store.payroll", "finance.ledger"]
        assert _schema_from_dict(_schema_to_dict(schema)).unreadable_tables == [
            "store.payroll",
            "finance.ledger",
        ]

    def test_a_cache_written_before_the_field_reads_as_empty(self) -> None:
        old = _schema_to_dict(_schema())
        del old["unreadable_tables"]
        assert _schema_from_dict(old).unreadable_tables == []

    def test_the_narrowing_keeps_them_minus_what_the_policy_hides(self) -> None:
        schema = _schema()
        schema.unreadable_tables = ["store.payroll", "finance.ledger", "store.audit"]
        privacy = PrivacySettings(excluded_schemas=["finance"], excluded_tables=["audit"])
        assert _apply_privacy_to_schema(schema, privacy).unreadable_tables == ["store.payroll"]


def _valued_schema() -> DataSourceSchema:
    """A MySQL schema whose cache row still carries values and ENUM members."""
    return DataSourceSchema(
        source_type="mysql",
        tables=[
            TableInfo(
                catalog=None,
                schema_name="shop",
                name="orders",
                table_type="table",
                columns=[
                    ColumnInfo(
                        name="status",
                        data_type="enum",
                        native_type="enum('pending','shipped')",
                        sample_values=["pending", "shipped"],
                    ),
                    ColumnInfo(
                        name="customer_email",
                        data_type="varchar",
                        native_type="varchar(255)",
                        sample_values=["a@example.com"],
                    ),
                ],
            )
        ],
    )


class TestApplyPrivacyWithholdsValues:
    """Columns stay (the sensitive patterns mark, they do not hide); their values go."""

    def test_values_withheld_when_sample_values_are_off(self) -> None:
        out = _apply_privacy_to_schema(_valued_schema(), PrivacySettings())
        status, email = out.tables[0].columns
        assert [status.name, email.name] == ["status", "customer_email"]
        assert status.sample_values is None
        assert status.native_type == "enum(…)"
        assert email.sample_values is None

    def test_sensitive_column_values_withheld_even_when_on(self) -> None:
        out = _apply_privacy_to_schema(
            _valued_schema(), PrivacySettings(include_sample_values=True)
        )
        status, email = out.tables[0].columns
        assert status.sample_values == ["pending", "shipped"]
        assert status.native_type == "enum('pending','shipped')"
        assert email.sample_values is None

    def test_valued_input_schema_is_not_mutated(self) -> None:
        schema = _valued_schema()
        _apply_privacy_to_schema(schema, PrivacySettings())
        assert schema.tables[0].columns[0].sample_values == ["pending", "shipped"]

    def test_withheld_values_leave_no_sample_source_behind(self) -> None:
        # sample_source says where sample_values came from; a column with no values has
        # no source, so a reader cannot be told a withheld column "had stats".
        out = _apply_privacy_to_schema(_hinted_schema(), PrivacySettings())
        for col in out.tables[0].columns:
            assert col.sample_values is None
            assert col.sample_source is None


class TestSampleSource:
    def test_round_trips_through_the_schema_cache(self) -> None:
        schema = _valued_schema()
        schema.tables[0].columns[0].sample_source = "type"
        out = _schema_from_dict(_schema_to_dict(schema))
        assert out.tables[0].columns[0].sample_source == "type"

    def test_a_cache_written_before_the_field_reads_as_none(self) -> None:
        d = _schema_to_dict(_valued_schema())
        for t in d["tables"]:
            for c in t["columns"]:
                c.pop("sample_source")
        out = _schema_from_dict(d)
        assert out.tables[0].columns[0].sample_source is None


def _hinted_schema() -> DataSourceSchema:
    """Two tables with every kind of hint: statistics values, type values, an unmarked
    cached column, and a row count."""

    def cols() -> list[ColumnInfo]:
        return [
            ColumnInfo("id", "integer", "integer", is_primary_key=True),
            ColumnInfo("status", "text", "text", sample_values=["a", "b"], sample_source="stats"),
            ColumnInfo(
                "kind", "enum", "enum('x','y')", sample_values=["x", "y"], sample_source="type"
            ),
            ColumnInfo("legacy", "text", "text", sample_values=["old"]),
        ]

    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[
            TableInfo(None, "public", "orders", "table", cols(), row_count_approx=5_000_000),
            TableInfo(None, "public", "items", "table", cols(), row_count_approx=5_000_000),
        ],
    )


_ON = PrivacySettings(include_sample_values=True, row_filters={"public.orders": "tenant_id = 7"})


class TestRowFilteredTablesLoseHints:
    def _table(self, name: str, privacy: PrivacySettings = _ON) -> TableInfo:
        out = _apply_privacy_to_schema(_hinted_schema(), privacy)
        return next(t for t in out.tables if t.name == name)

    def test_statistics_values_and_row_count_go(self) -> None:
        orders = self._table("orders")
        assert orders.row_count_approx is None
        assert {c.name: c.sample_values for c in orders.columns} == {
            "id": None,
            "status": None,
            "kind": ["x", "y"],
            "legacy": None,
        }

    def test_type_values_stay(self) -> None:
        kind = next(c for c in self._table("orders").columns if c.name == "kind")
        assert kind.sample_values == ["x", "y"]

    def test_an_unmarked_cached_column_is_withheld(self) -> None:
        legacy = next(c for c in self._table("orders").columns if c.name == "legacy")
        assert legacy.sample_values is None

    def test_an_unfiltered_table_is_untouched(self) -> None:
        items = self._table("items")
        assert items.row_count_approx == 5_000_000
        assert [c.sample_values for c in items.columns] == [None, ["a", "b"], ["x", "y"], ["old"]]

    def test_a_key_matching_no_table_withholds_nothing(self) -> None:
        privacy = PrivacySettings(
            include_sample_values=True, row_filters={"public.ordres": "tenant_id = 7"}
        )
        assert self._table("orders", privacy).row_count_approx == 5_000_000

    def test_the_comparison_ignores_case(self) -> None:
        # A stored key that differs from the cached table only in case (an older key saved
        # before unquoted parts were folded, or a quoted spelling) still names that table
        # as far as the hints are concerned: withholding is the safe direction.
        privacy = PrivacySettings(
            include_sample_values=True, row_filters={"public.Orders": "tenant_id = 7"}
        )
        orders = self._table("orders", privacy)
        assert orders.row_count_approx is None
        assert next(c for c in orders.columns if c.name == "status").sample_values is None
        assert self._table("items", privacy).row_count_approx == 5_000_000

    def test_the_migration_state_withholds_from_every_table(self) -> None:
        privacy = PrivacySettings(include_sample_values=True, row_filter_sql="tenant_id = 7")
        assert privacy.row_filter_needs_migration
        for name in ("orders", "items"):
            table = self._table(name, privacy)
            assert table.row_count_approx is None
            assert {c.name: c.sample_values for c in table.columns} == {
                "id": None,
                "status": None,
                "kind": ["x", "y"],
                "legacy": None,
            }

    def test_input_schema_is_not_mutated(self) -> None:
        schema = _hinted_schema()
        _apply_privacy_to_schema(schema, _ON)
        assert schema.tables[0].row_count_approx == 5_000_000
        assert schema.tables[0].columns[1].sample_values == ["a", "b"]

    def test_prompt_header_has_no_row_count_for_the_filtered_table(self) -> None:
        out = _apply_privacy_to_schema(_hinted_schema(), _ON)
        text = PostgreSQLDataSource().format_schema_for_llm(out, _ON)
        headers = [line for line in text.splitlines() if line.startswith("-- Table:")]
        orders_header = next(line for line in headers if "orders" in line)
        items_header = next(line for line in headers if "items" in line)
        assert "≈" not in orders_header
        assert "≈5,000,000 rows" in items_header

    def test_the_full_scan_guard_does_not_fire_for_the_filtered_table(self) -> None:
        # Intended side effect: with no row count the guard has nothing to report, and its
        # message would otherwise print the whole-table count.
        narrowed = _apply_privacy_to_schema(_hinted_schema(), _ON)
        assert _check_query_complexity("SELECT * FROM public.orders", narrowed) is None
        warning = _check_query_complexity("SELECT * FROM public.items", narrowed)
        assert warning is not None
        assert "5,000,000" in warning
