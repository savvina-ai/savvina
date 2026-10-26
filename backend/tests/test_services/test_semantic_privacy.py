# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Hidden identifiers are kept out of the semantic prompt's free-text sections."""

from __future__ import annotations

import re

from app.datasources.models import ColumnInfo, DataSourceSchema, PrivacySettings, TableInfo
from app.semantic.formatter import SemanticFormatter
from app.semantic.models import (
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    DerivedMetric,
    Segment,
    SemanticModel,
    SimpleMetric,
    TableSemantic,
)
from app.services import semantic_privacy
from app.services.schema_pruning import _filter_semantic_to_schema
from app.services.semantic_privacy import (
    _hidden_names,
    _HiddenMatcher,
    without_hidden_identifiers,
)


def _table(schema: str, name: str, cols: list[str]) -> TableInfo:
    return TableInfo(
        catalog=None,
        schema_name=schema,
        name=name,
        table_type="BASE TABLE",
        columns=[ColumnInfo(name=c, data_type="text", native_type="text") for c in cols],
    )


def _schema(*tables: TableInfo) -> DataSourceSchema:
    return DataSourceSchema(source_type="postgresql", tables=list(tables))


def _model(**update) -> SemanticModel:
    return SemanticModel(
        tables={
            "store.orders": TableSemantic(
                display_name="Orders",
                columns={
                    "id": ColumnSemantic(display_name="Order ID"),
                    "amount": ColumnSemantic(display_name="Amount"),
                    "secret_ts": ColumnSemantic(display_name="Hidden Stamp"),
                },
            ),
            "store.customers": TableSemantic(
                display_name="Customers", columns={"id": ColumnSemantic(display_name="Id")}
            ),
        },
        **update,
    )


def _schema_without_secret() -> DataSourceSchema:
    return _schema(
        _table("store", "orders", ["id", "amount"]), _table("store", "customers", ["id"])
    )


# The policy that hides ``store.orders.secret_ts``: the tests' usual hidden column.
_HIDE_SECRET = PrivacySettings(excluded_columns=["store.orders.secret_ts"])


def _metric(name: str, definition: str) -> DerivedMetric:
    return DerivedMetric(name=name, definition=definition, description="")


class TestHiddenNames:
    def test_a_hidden_column_contributes_its_three_forms(self) -> None:
        names = _hidden_names(_model(), _schema_without_secret(), _HIDE_SECRET)
        assert names == {"store.orders.secret_ts", "orders.secret_ts", "secret_ts"}

    def test_a_name_visible_elsewhere_is_not_secret(self) -> None:
        """``id`` is hidden on orders but customers has one: only the qualified forms
        are hidden."""
        schema = _schema(
            _table("store", "orders", ["amount", "secret_ts"]),
            _table("store", "customers", ["id"]),
        )
        names = _hidden_names(
            _model(), schema, PrivacySettings(excluded_columns=["store.orders.id"])
        )
        assert "id" not in names and "orders.id" in names

    def test_a_table_the_schema_lacks_hides_its_key_bare_name_and_columns(self) -> None:
        privacy = PrivacySettings(excluded_tables=["store.orders"])
        names = _hidden_names(_model(), _schema(_table("store", "customers", ["id"])), privacy)
        assert {"store.orders", "orders", "orders.secret_ts", "secret_ts", "amount"} <= names
        assert "id" not in names  # visible on customers

    def test_a_table_in_an_excluded_schema_hides_its_key_and_columns(self) -> None:
        model = SemanticModel(
            tables={
                "hr.payroll": TableSemantic(
                    display_name="P", columns={"ssn": ColumnSemantic(display_name="s")}
                )
            }
        )
        privacy = PrivacySettings(excluded_schemas=["hr"])
        names = _hidden_names(model, _schema_without_secret(), privacy)
        assert {"hr.payroll", "payroll", "payroll.ssn", "ssn"} <= names

    def test_a_bare_stored_key_is_hidden_when_its_table_is_excluded_anywhere(self) -> None:
        """A stored key is normally ``schema.table``; a bare one is an anomaly, and
        for a privacy filter the safe reading is the lenient one."""
        model = SemanticModel(
            tables={
                "salaries": TableSemantic(
                    display_name="S", columns={"pay": ColumnSemantic(display_name="p")}
                )
            }
        )
        privacy = PrivacySettings(excluded_tables=["hr.salaries"])
        assert {"salaries", "pay"} <= _hidden_names(model, _schema_without_secret(), privacy)

    def test_a_gone_table_or_column_is_not_hidden(self) -> None:
        """Dropped or renamed is stale, not secret: only the policy hides."""
        schema = _schema(_table("store", "orders", ["id", "amount", "stamp_ts"]))
        assert _hidden_names(_model(), schema, None) == set()
        assert _hidden_names(_model(), schema, PrivacySettings()) == set()
        unrelated = PrivacySettings(excluded_columns=["store.orders.other"])
        assert "secret_ts" not in _hidden_names(_model(), schema, unrelated)
        assert "store.customers" not in _hidden_names(_model(), schema, unrelated)

    def test_the_policy_is_matched_casefolded(self) -> None:
        """The policy compares exact strings; here a case-only difference between the
        stored spelling and the policy's still hides (the safe side for a filter)."""
        model = SemanticModel(
            tables={
                "public.Orders": TableSemantic(
                    display_name="O", columns={"Email": ColumnSemantic(display_name="E")}
                ),
                "Pay.Roll": TableSemantic(
                    display_name="P", columns={"Ssn": ColumnSemantic(display_name="S")}
                ),
            }
        )
        schema = _schema(_table("public", "orders", ["id"]))
        privacy = PrivacySettings(
            excluded_columns=["PUBLIC.orders.EMAIL"], excluded_tables=["pay.ROLL"]
        )
        names = _hidden_names(model, schema, privacy)
        assert {"public.orders.email", "email", "pay.roll", "roll", "ssn"} <= names

    def test_visibility_is_casefolded(self) -> None:
        model = SemanticModel(
            tables={
                "public.Orders": TableSemantic(
                    display_name="O", columns={"Email": ColumnSemantic(display_name="E")}
                )
            }
        )
        privacy = PrivacySettings(excluded_columns=["public.Orders.Email"])
        visible = _schema(_table("public", "orders", ["email"]))
        assert _hidden_names(model, visible, privacy) == set()
        hidden = _hidden_names(model, _schema(_table("public", "orders", ["id"])), privacy)
        assert hidden == {"public.orders.email", "orders.email", "email"}

    def test_privacy_settings_seed_names_the_model_never_annotated(self) -> None:
        privacy = PrivacySettings(
            excluded_columns=["store.orders.ssn"], excluded_tables=["hr.salaries"]
        )
        names = _hidden_names(_model(), _schema_without_secret(), privacy)
        assert {"store.orders.ssn", "orders.ssn", "ssn", "hr.salaries", "salaries"} <= names


class TestHiddenMatcher:
    def test_matches_whole_identifiers_in_dotted_and_quoted_chains(self) -> None:
        m = _HiddenMatcher({"orders.secret_ts", "ssn"})
        assert m.first_mention('"orders"."secret_ts" IS NULL') == "orders.secret_ts"
        assert m.first_mention("[store].[orders].[secret_ts]") == "orders.secret_ts"
        assert m.first_mention("`orders` . `secret_ts`") == "orders.secret_ts"
        assert m.first_mention("SUM(SSN)") == "ssn"
        assert m.first_mention("secret_ts IS NULL") is None  # bare form not hidden
        assert m.first_mention("ssn_hash, xssn") is None
        assert m.first_mention("customers.secret_ts") is None
        # Brackets are separators, not glue: neither reads as ``arrayssn`` / ``xssn``.
        assert m.first_mention("ARRAY[ssn]") == "ssn"
        assert m.first_mention("x[ssn]") == "ssn"

    def test_a_non_ascii_name_is_a_whole_identifier(self) -> None:
        m = _HiddenMatcher({"τιμή"})
        assert m.first_mention("SUM(τιμή_μονάδας)") is None
        assert m.first_mention("SUM(Τιμή) * 2") == "τιμή"

    def test_an_excluded_schema_prefixes_a_mention(self) -> None:
        m = _HiddenMatcher((), schemas={"HR"})
        assert m
        assert m.first_mention("SUM(hr.payroll.amount)") == "hr"
        assert m.first_mention('"HR"."payroll"') == "hr"
        assert m.first_mention("[hr].[payroll]") == "hr"
        assert m.first_mention("`db`.`hr`.`payroll`") == "hr"
        assert m.first_mention("db.hr.payroll") == "hr"
        assert m.first_mention("hr_flag = 1 AND t.hr = 2") is None
        # Only a tight chain: whitespace at the dot is prose, not a reference.
        assert m.first_mention("Headcount by hr. Includes contractors") is None
        assert m.first_mention("hr . payroll") is None

    def test_a_name_outside_the_plain_alphabet_is_a_substring_check(self) -> None:
        m = _HiddenMatcher({"order date"})
        assert m.first_mention('MAX("Order Date")') == "order date"
        assert m.first_mention("MAX(order_date)") is None


class _CountingSet(frozenset):
    """A frozenset that counts membership lookups."""

    lookups = 0

    def __contains__(self, item: object) -> bool:
        type(self).lookups += 1
        return super().__contains__(item)


class TestMatcherCost:
    def _lookups(self, n_names: int, text: str) -> int:
        """Membership lookups ``first_mention(text)`` makes against ``n_names``
        plain names of one shape (``s.tI.cI``, so the chain span is the same)."""
        matcher = _HiddenMatcher({f"s.t{i}.c{i}" for i in range(n_names)})
        matcher._plain = _CountingSet(matcher._plain)
        _CountingSet.lookups = 0
        assert matcher.first_mention(text) is None
        return _CountingSet.lookups

    def test_lookups_depend_on_the_text_not_on_the_name_count(self) -> None:
        """Guards against a regression to a names-by-text matcher (the regex
        alternation the set lookup replaced took seconds with ~5,000 hidden
        names): the matcher's work must grow with the text, not the name set."""
        text = " ".join(
            f'SUM(s.v.k) / NULLIF(COUNT(DISTINCT "v"."k"), 0) - AVG(k) + {i}' for i in range(50)
        )
        small, large = self._lookups(10, text), self._lookups(5000, text)
        assert small == large > 0
        # At most ``span`` (3) candidate runs start at each identifier part.
        parts = len(re.findall(r"\w+", text))
        assert large <= 3 * parts


class TestWithoutHiddenIdentifiers:
    def test_items_naming_a_hidden_column_are_dropped_from_the_prompt(self) -> None:
        model = _model(
            business_metrics=[
                _metric("Net", "SUM(amount) - SUM(secret_ts)"),
                _metric("Gross", "SUM(amount)"),
            ],
            derived_columns=[
                DerivedColumn(name="Lag", sql_expression="now() - store.orders.secret_ts"),
                DerivedColumn(name="Half", sql_expression="amount / 2"),
            ],
            segments=[
                Segment(name="Stamped", sql_expression="SECRET_TS IS NOT NULL"),
                Segment(name="Paid", sql_expression="amount > 0"),
            ],
            common_joins=[
                CommonJoin(
                    description="By stamp",
                    tables=["store.orders", "store.customers"],
                    join_pattern="orders.secret_ts = customers.id",
                ),
            ],
        )
        schema = _schema_without_secret()
        cleaned = without_hidden_identifiers(model, schema, _HIDE_SECRET)
        assert [m.name for m in cleaned.business_metrics] == ["Gross"]
        assert [d.name for d in cleaned.derived_columns] == ["Half"]
        assert [s.name for s in cleaned.segments] == ["Paid"]
        assert cleaned.common_joins == []
        prompt = SemanticFormatter().format_for_prompt(_filter_semantic_to_schema(cleaned, schema))
        assert "secret_ts" not in prompt.lower()
        # The input is untouched.
        assert len(model.business_metrics) == 2

    def test_a_bare_name_visible_elsewhere_keeps_its_metric(self) -> None:
        model = _model(
            business_metrics=[_metric("Rows", "COUNT(id)"), _metric("Hid", "COUNT(orders.id)")]
        )
        schema = _schema(
            _table("store", "orders", ["amount", "secret_ts"]),
            _table("store", "customers", ["id"]),
        )
        privacy = PrivacySettings(excluded_columns=["store.orders.id"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Rows"]

    def test_a_quoted_qualified_reference_is_dropped(self) -> None:
        """``secret_ts`` is visible on customers, so only the qualified forms are
        hidden; quoting does not hide them from the matcher."""
        model = _model(
            segments=[
                Segment(name="A", sql_expression='"orders"."secret_ts" IS NULL'),
                Segment(name="B", sql_expression="`store`.`orders`.`secret_ts` > 0"),
                Segment(name="C", sql_expression="[orders].[secret_ts] = 1"),
                Segment(name="D", sql_expression='"customers"."secret_ts" IS NULL'),
            ]
        )
        schema = _schema(
            _table("store", "orders", ["id", "amount"]),
            _table("store", "customers", ["id", "secret_ts"]),
        )
        cleaned = without_hidden_identifiers(model, schema, _HIDE_SECRET)
        assert [s.name for s in cleaned.segments] == ["D"]

    def test_a_hidden_tables_column_is_dropped(self) -> None:
        model = SemanticModel(
            tables={
                "hr.salaries": TableSemantic(
                    display_name="S", columns={"ssn": ColumnSemantic(display_name="x")}
                )
            },
            business_metrics=[_metric("Pay", "SUM(ssn)"), _metric("Ok", "SUM(amount)")],
        )
        schema = _schema(_table("store", "orders", ["amount"]))
        assert without_hidden_identifiers(model, schema, None) is model  # gone, not hidden
        for privacy in (
            PrivacySettings(excluded_tables=["hr.salaries"]),
            PrivacySettings(excluded_schemas=["hr"]),
        ):
            cleaned = without_hidden_identifiers(model, schema, privacy)
            assert [m.name for m in cleaned.business_metrics] == ["Ok"]

    def test_a_fixed_vocabulary_field_does_not_match_a_hidden_column(self) -> None:
        """A hidden column named ``count`` must not drop a metric whose only
        ``count`` is its aggregation."""
        model = SemanticModel(
            tables={
                "s.orders": TableSemantic(
                    display_name="O",
                    columns={
                        "id": ColumnSemantic(display_name="i"),
                        "count": ColumnSemantic(display_name="c"),
                    },
                )
            },
            business_metrics=[
                SimpleMetric(name="Orders", definition="orders.id", aggregation="count"),
                _metric("Tally", "SUM(orders.count)"),
            ],
        )
        privacy = PrivacySettings(excluded_columns=["s.orders.count"])
        schema = _schema(_table("s", "orders", ["id"]))
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Orders"]

    def test_an_excluded_column_the_model_never_annotated_is_dropped(self) -> None:
        model = _model(business_metrics=[_metric("Pay", "SUM(ssn)"), _metric("Ok", "SUM(id)")])
        schema = _schema_without_secret()
        assert len(without_hidden_identifiers(model, schema, None).business_metrics) == 2
        privacy = PrivacySettings(excluded_columns=["store.orders.ssn"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Ok"]

    def test_notes_default_filters_and_base_sql_are_scrubbed(self) -> None:
        model = SemanticModel(
            tables={
                "s.orders": TableSemantic(
                    display_name="O",
                    default_filters=["deleted_ssn IS NULL", "status <> 'void'"],
                    base_sql="SELECT id, deleted_ssn FROM raw.orders",
                    columns={
                        "id": ColumnSemantic(display_name="i"),
                        "deleted_ssn": ColumnSemantic(display_name="d"),
                    },
                ),
                "s.items": TableSemantic(
                    display_name="I", base_sql="SELECT id FROM raw.items", columns={}
                ),
            },
            notes=["Never expose deleted_ssn", "Amounts are in euros"],
        )
        schema = _schema(_table("s", "orders", ["id", "status"]), _table("s", "items", ["id"]))
        privacy = PrivacySettings(excluded_columns=["s.orders.deleted_ssn"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert cleaned.notes == ["Amounts are in euros"]
        assert cleaned.tables["s.orders"].default_filters == ["status <> 'void'"]
        assert cleaned.tables["s.orders"].base_sql is None
        assert cleaned.tables["s.items"] is model.tables["s.items"]
        prompt = SemanticFormatter().format_for_prompt(_filter_semantic_to_schema(cleaned, schema))
        assert "deleted_ssn" not in prompt
        # The shared input is untouched.
        assert model.tables["s.orders"].base_sql is not None
        assert len(model.notes) == 2

    def test_a_dropped_item_is_logged_at_debug_with_its_trigger(self, caplog) -> None:
        model = _model(business_metrics=[_metric("Net", "SUM(secret_ts)")])
        with caplog.at_level("DEBUG", logger="app.services.semantic_privacy"):
            without_hidden_identifiers(model, _schema_without_secret(), _HIDE_SECRET)
        messages = [r.getMessage() for r in caplog.records]
        assert any("'Net'" in m and "'secret_ts'" in m for m in messages)

    def test_a_hidden_greek_column_is_dropped(self) -> None:
        model = SemanticModel(
            tables={
                "s.προϊόντα": TableSemantic(
                    display_name="P",
                    columns={
                        "τιμή": ColumnSemantic(display_name="t"),
                        "τιμή_μονάδας": ColumnSemantic(display_name="u"),
                    },
                )
            },
            business_metrics=[_metric("Hid", "SUM(Τιμή)"), _metric("Ok", "SUM(τιμή_μονάδας)")],
        )
        schema = _schema(_table("s", "προϊόντα", ["τιμή_μονάδας"]))
        privacy = PrivacySettings(excluded_columns=["s.προϊόντα.τιμή"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Ok"]

    def test_an_unannotated_table_in_an_excluded_schema_is_dropped(self) -> None:
        model = _model(
            business_metrics=[
                _metric("Pay", "SUM(hr.payroll.amount)"),
                _metric("Ok", "SUM(amount)"),
            ]
        )
        schema = _schema_without_secret()
        assert len(without_hidden_identifiers(model, schema, None).business_metrics) == 2
        privacy = PrivacySettings(excluded_schemas=["hr"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Ok"]

    def test_an_excluded_schema_name_in_prose_keeps_its_item(self) -> None:
        model = _model(
            business_metrics=[
                DerivedMetric(
                    name="Revenue",
                    definition="SUM(amount)",
                    description="Total sales. Includes refunds",
                ),
                _metric("Leak", "SUM(sales.ledger.amount)"),
            ]
        )
        privacy = PrivacySettings(excluded_schemas=["sales"])
        cleaned = without_hidden_identifiers(model, _schema_without_secret(), privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Revenue"]

    def test_an_excluded_schema_named_like_a_visible_table_is_not_secret(self) -> None:
        """With ``public.hr`` visible, ``hr.id`` names that table, not schema ``hr``."""
        model = _model(business_metrics=[_metric("Staff", "COUNT(hr.id)")])
        schema = _schema(_table("store", "orders", ["id"]), _table("public", "hr", ["id"]))
        privacy = PrivacySettings(excluded_schemas=["HR"])
        cleaned = without_hidden_identifiers(model, schema, privacy)
        assert [m.name for m in cleaned.business_metrics] == ["Staff"]

    def test_nothing_hidden_returns_the_same_instance(self) -> None:
        model = _model(business_metrics=[_metric("Net", "SUM(secret_ts)")])
        schema = _schema(
            _table("store", "orders", ["id", "amount", "secret_ts"]),
            _table("store", "customers", ["id"]),
        )
        assert _hidden_names(model, schema, _HIDE_SECRET) == set()
        assert without_hidden_identifiers(model, schema, _HIDE_SECRET) is model
        assert without_hidden_identifiers(model, schema, None) is model

    def test_a_renamed_or_dropped_column_without_a_policy_keeps_its_items(self) -> None:
        """A metric or note naming a column that left the database is stale, not
        secret: it stays until the next globals run. The same column hidden by the
        policy takes them out."""
        model = _model(
            business_metrics=[_metric("Net", "SUM(orders.secret_ts)"), _metric("Ok", "SUM(id)")],
            notes=["secret_ts is the legacy stamp", "Amounts are in euros"],
        )
        renamed = _schema(
            _table("store", "orders", ["id", "amount", "stamp_ts"]),
            _table("store", "customers", ["id"]),
        )
        for schema in (renamed, _schema_without_secret()):
            assert without_hidden_identifiers(model, schema, None) is model
            assert without_hidden_identifiers(model, schema, PrivacySettings()) is model
            cleaned = without_hidden_identifiers(model, schema, _HIDE_SECRET)
            assert [m.name for m in cleaned.business_metrics] == ["Ok"]
            assert cleaned.notes == ["Amounts are in euros"]

    def test_memoised_on_the_model_instance_schema_and_privacy(self) -> None:
        model = _model(business_metrics=[_metric("Net", "SUM(secret_ts)")])
        first = without_hidden_identifiers(model, _schema_without_secret(), _HIDE_SECRET)
        assert first is not model
        assert without_hidden_identifiers(model, _schema_without_secret(), _HIDE_SECRET) is first
        privacy = PrivacySettings(excluded_columns=["store.orders.secret_ts", "store.orders.ssn"])
        assert without_hidden_identifiers(model, _schema_without_secret(), privacy) is not first
        twin = _model(business_metrics=[_metric("Net", "SUM(secret_ts)")])
        assert without_hidden_identifiers(twin, _schema_without_secret(), _HIDE_SECRET) is not first
        assert len(semantic_privacy._cleaned_cache) == 3

    def test_the_memo_evicts_the_least_recently_used_entry(self, monkeypatch) -> None:
        monkeypatch.setattr(semantic_privacy, "_CLEANED_CACHE_SIZE", 1)
        model = _model(business_metrics=[_metric("Net", "SUM(secret_ts)")])
        first = without_hidden_identifiers(model, _schema_without_secret(), _HIDE_SECRET)
        without_hidden_identifiers(model, _schema(_table("store", "orders", ["id"])), _HIDE_SECRET)
        assert len(semantic_privacy._cleaned_cache) == 1
        assert (
            without_hidden_identifiers(model, _schema_without_secret(), _HIDE_SECRET) is not first
        )
