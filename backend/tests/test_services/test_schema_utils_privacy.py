# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``_apply_privacy_to_schema`` is the one narrowing every schema consumer passes
through, so it must drop everything the policy hides: schemas, tables, the
relationships between them, and ``excluded_columns``."""

from __future__ import annotations

from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from app.services.schema_utils import _apply_privacy_to_schema, _schema_from_dict, _schema_to_dict


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
