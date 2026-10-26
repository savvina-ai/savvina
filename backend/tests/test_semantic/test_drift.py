# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``semantic/drift`` — the one schema-vs-model diff the check and the apply share,
and the before/after summary the apply reports."""

from __future__ import annotations

import pytest

from app.datasources.models import ColumnInfo, DataSourceSchema, PrivacySettings, TableInfo
from app.semantic.drift import (
    DriftApplySummary,
    SchemaDrift,
    compute_drift,
    policy_hides_column,
    resolve_real_key,
    strip_ident_quotes,
    summarize_model_change,
)
from app.semantic.models import ColumnSemantic, SemanticModel, TableSemantic


def _schema(
    *tables: tuple[str, str, list[str]], unreadable: list[str] | None = None
) -> DataSourceSchema:
    """``(schema_name, table_name, column_names)`` triples → a live schema."""
    return DataSourceSchema(
        source_type="postgresql",
        tables=[
            TableInfo(
                catalog=None,
                schema_name=schema_name,
                name=name,
                table_type="table",
                columns=[ColumnInfo(name=c, data_type="text", native_type="text") for c in cols],
            )
            for schema_name, name, cols in tables
        ],
        unreadable_tables=unreadable or [],
    )


def _model(**tables: list[str]) -> SemanticModel:
    """``public_orders=["id", "status"]`` → a model keyed ``public.orders``; the first
    underscore is the dot (kwargs cannot carry one)."""
    out: dict[str, TableSemantic] = {}
    for kwarg, cols in tables.items():
        key = kwarg.replace("_", ".", 1)
        out[key] = TableSemantic(
            display_name=key, columns={c: ColumnSemantic(display_name=c) for c in cols}
        )
    return SemanticModel(tables=out)


class TestStructuralDiff:
    def test_identical_schema_and_model_is_empty(self) -> None:
        drift = compute_drift(_schema(("public", "orders", ["id"])), _model(public_orders=["id"]))
        assert drift == SchemaDrift()
        assert drift.is_empty

    def test_new_removed_tables_and_removed_columns(self) -> None:
        schema = _schema(("public", "orders", ["id"]), ("public", "customers", ["id"]))
        model = _model(public_orders=["id", "legacy_code"], public_products=["id"])
        drift = compute_drift(schema, model)
        assert drift.new_table_keys == {"public.customers"}
        assert drift.removed_table_keys == {"public.products"}
        assert drift.removed_columns == {"public.orders": {"legacy_code"}}
        assert not drift.is_empty

    def test_a_non_default_schema_keys_as_schema_dot_table(self) -> None:
        """MySQL reports its database as the schema, so the key is not always ``public.``."""
        drift = compute_drift(_schema(("shop", "sales", ["id"])), _model(shop_sales=["id", "gone"]))
        assert drift.removed_columns == {"shop.sales": {"gone"}}

    def test_a_live_name_containing_a_quote_character_is_present(self) -> None:
        """``o'brien_flag`` is a real column name: an exact match is present, as the
        prune keeps it; only the stripped spelling would miss it."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "o'brien_flag"])),
            _model(public_orders=["id", "o'brien_flag"]),
        )
        assert drift.is_empty

    def test_a_quoted_spelling_of_a_live_column_is_removed_under_its_stored_spelling(
        self,
    ) -> None:
        """``'"status"'`` beside a live ``status`` is a blob-era key: the prune drops it
        (``_prune_table`` keeps an exact match only), so the check names it too, verbatim,
        and the apply has something to act on."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", '"status"']),
        )
        assert drift.removed_columns == {"public.orders": {'"status"'}}
        assert not drift.is_empty

    def test_a_quoted_stored_key_absent_live_is_removed_under_its_stored_spelling(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", "`gone`"]),
        )
        assert drift.removed_columns == {"public.orders": {"`gone`"}}

    def test_a_live_column_neither_stored_nor_known_is_added(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id", "loyalty_tier"])),
            _model(public_orders=["id"]),
            known_columns={"public.orders": {"id"}},
        )
        assert drift.added_columns == {"public.orders": {"loyalty_tier"}}
        assert drift.removed_columns == {}
        assert drift.new_table_keys == set()
        # An added column alone is something to apply.
        assert not drift.is_empty

    def test_a_live_column_the_baseline_knows_is_not_added_even_if_not_stored(self) -> None:
        """The LLM omitted it, or the user deleted its annotation: the model has seen it."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "notes"])),
            _model(public_orders=["id"]),
            known_columns={"public.orders": {"id", "notes"}},
        )
        assert drift.added_columns == {}
        assert drift.is_empty

    def test_a_table_with_no_baseline_entry_reports_no_added_columns(self) -> None:
        schema = _schema(("public", "orders", ["id", "loyalty_tier"]))
        model = _model(public_orders=["id"])
        assert compute_drift(schema, model).added_columns == {}
        assert compute_drift(schema, model, known_columns={}).added_columns == {}
        assert compute_drift(schema, model, known_columns={"public.other": {"x"}}).is_empty

    def test_a_baseline_naming_columns_that_left_the_table_is_harmless(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id"]),
            known_columns={"public.orders": {"id", "dropped_long_ago"}},
        )
        assert drift.added_columns == {}
        assert drift.is_empty

    def test_a_stored_column_the_baseline_lacks_is_not_added(self) -> None:
        """A column added by hand is stored but not yet in the baseline: it is held, not new."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "notes"])),
            _model(public_orders=["id", "notes"]),
            known_columns={"public.orders": {"id"}},
        )
        assert drift.added_columns == {}
        assert drift.is_empty

    def test_the_columns_of_a_new_table_are_not_added_columns(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"]), ("public", "events", ["id", "kind"])),
            _model(public_orders=["id"]),
            known_columns={"public.orders": {"id"}, "public.events": {"id"}},
        )
        assert drift.new_table_keys == {"public.events"}
        assert drift.added_columns == {}

    def test_a_renamed_column_is_one_removed_and_one_added(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id", "state"])),
            _model(public_orders=["id", "status"]),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"status"}}
        assert drift.added_columns == {"public.orders": {"state"}}

    def test_quoted_only_keys_are_removed_and_their_live_column_added_once(self) -> None:
        """The column is held only under two quoted spellings, which the apply drops: the
        baseline knows ``status`` (every writer records live names), yet it is new again
        so the apply re-annotates it rather than leave the table without it. Both
        spellings collapse to the one live name."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", '"status"', "`status`"]),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {'"status"', "`status`"}}
        assert drift.added_columns == {"public.orders": {"status"}}
        assert drift.respelled_columns == {"public.orders": {"status"}}

    def test_a_quoted_key_beside_its_stored_bare_twin_is_removed_and_nothing_added(
        self,
    ) -> None:
        """The bare ``status`` keeps its annotation, so dropping the duplicate loses nothing."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "status", '"status"']),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {'"status"'}}
        assert drift.added_columns == {}

    @pytest.mark.parametrize("stored_key", ["Status", '"Status"'])
    def test_a_case_only_key_is_removed_and_its_live_column_added(self, stored_key: str) -> None:
        """``Status`` is not the live ``status``, so the apply drops it: the column is new
        again rather than left without its annotation (quoted or not)."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", stored_key]),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {stored_key}}
        assert drift.added_columns == {"public.orders": {"status"}}
        assert drift.respelled_columns == {"public.orders": {"status"}}

    def test_a_case_only_key_beside_its_stored_target_is_removed_and_nothing_added(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "status", "Status"]),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"Status"}}
        assert drift.added_columns == {}

    def test_a_dropped_column_the_baseline_knew_is_not_a_respelling_of_its_case_twin(self) -> None:
        """The table had ``Status`` and ``status``; the model held only ``Status`` and the
        baseline saw both. The database dropped ``Status``: that is a drop, so ``status``,
        which the model saw and chose not to hold, is not new."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "Status"]),
            known_columns={"public.orders": {"id", "Status", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"Status"}}
        assert drift.added_columns == {}

    def test_a_removed_key_the_baseline_knows_is_a_dropped_column(self) -> None:
        """The table had ``Status`` and ``status``, the model held both and the baseline
        saw both. The database dropped ``Status``: a genuine drop, recorded as one, even
        though its case twin is still stored."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "Status", "status"]),
            known_columns={"public.orders": {"id", "Status", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"Status"}}
        assert drift.dropped_columns == {"public.orders": {"Status"}}
        assert drift.added_columns == {}

    def test_a_removed_key_the_baseline_does_not_know_is_not_a_dropped_column(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "Status", "status"]),
            known_columns={"public.orders": {"id", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"Status"}}
        assert drift.dropped_columns == {}

    def test_a_table_with_no_baseline_entry_has_no_dropped_columns(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])), _model(public_orders=["id", "gone"])
        )
        assert drift.removed_columns == {"public.orders": {"gone"}}
        assert drift.dropped_columns == {}

    def test_dropped_columns_do_not_change_is_empty(self) -> None:
        """Every dropped key is in ``removed_columns`` already: the diff is not empty
        because of the removal, and ``dropped_columns`` alone makes nothing to apply."""
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", "gone"]),
            known_columns={"public.orders": {"id", "gone"}},
        )
        assert drift.dropped_columns == {"public.orders": {"gone"}}
        assert not drift.is_empty
        assert SchemaDrift(dropped_columns={"public.orders": {"gone"}}).is_empty

    def test_a_case_only_rename_in_the_database_is_a_drop_and_a_new_column(self) -> None:
        """The baseline saw ``Status`` but never ``status``: the database renamed the
        column, so ``status`` is genuinely new and ``Status`` is just removed, not a
        respelling of it."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", "Status"]),
            known_columns={"public.orders": {"id", "Status"}},
        )
        assert drift.removed_columns == {"public.orders": {"Status"}}
        assert drift.added_columns == {"public.orders": {"status"}}
        assert drift.respelled_columns == {}

    def test_an_ambiguous_case_only_key_is_removed_and_adds_nothing(self) -> None:
        """``STATUS`` matches both live ``Status`` and ``status``: the resolver, like the
        prune's near-miss warning, names neither."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "Status", "status"])),
            _model(public_orders=["id", "STATUS"]),
            known_columns={"public.orders": {"id", "Status", "status"}},
        )
        assert drift.removed_columns == {"public.orders": {"STATUS"}}
        assert drift.added_columns == {}

    def test_a_quoted_only_key_with_no_baseline_entry_reports_no_added_column(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id", "status"])),
            _model(public_orders=["id", '"status"']),
        )
        assert drift.removed_columns == {"public.orders": {'"status"'}}
        assert drift.added_columns == {}

    def test_a_live_column_the_policy_hides_is_not_added_until_it_is_unhidden(self) -> None:
        """Even from a schema that still lists it, as the one hiding rule the removed
        columns use: the apply never sends a hidden column to the LLM. Un-hiding it makes
        it live and unknown to the baseline, so it is added."""
        schema = _schema(("public", "orders", ["id", "ssn"]))
        model = _model(public_orders=["id"])
        known = {"public.orders": {"id"}}
        hidden = compute_drift(
            schema, model, PrivacySettings(excluded_columns=["public.orders.ssn"]), known
        )
        assert hidden.added_columns == {}
        assert hidden.is_empty
        unhidden = compute_drift(schema, model, known_columns=known)
        assert unhidden.added_columns == {"public.orders": {"ssn"}}

    def test_a_near_miss_key_of_a_hidden_column_is_removed_and_respells_nothing(
        self,
    ) -> None:
        """The policy match is exact, so ``SSN`` is not hidden and the apply removes it;
        its target ``ssn`` is hidden, so it is neither added nor respelled."""
        drift = compute_drift(
            _schema(("public", "orders", ["id", "ssn"])),
            _model(public_orders=["id", "SSN"]),
            PrivacySettings(excluded_columns=["public.orders.ssn"]),
            {"public.orders": {"id", "ssn"}},
        )
        assert drift.removed_columns == {"public.orders": {"SSN"}}
        assert drift.added_columns == {}
        assert drift.respelled_columns == {}


class TestPrivacyExclusionsAreNotDrops:
    """The schema every drift route holds is privacy-filtered, so an excluded column or
    table is absent from it exactly like a dropped one. The diff subtracts what the
    policy explains — over-explaining is the safe side, since here it means not deleting."""

    def test_an_excluded_column_is_not_a_removed_column(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", "status"]),
            PrivacySettings(excluded_columns=["public.orders.status"]),
        )
        assert drift.is_empty

    def test_an_exclusion_on_another_table_explains_nothing(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", "status"]),
            PrivacySettings(excluded_columns=["public.customers.status"]),
        )
        assert drift.removed_columns == {"public.orders": {"status"}}

    def test_a_quoted_stored_key_for_an_excluded_column_is_not_removed(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", '"status"']),
            PrivacySettings(excluded_columns=["public.orders.status"]),
        )
        assert drift.is_empty

    @pytest.mark.parametrize("entry", ["public.customers", "customers"])
    def test_an_excluded_table_is_not_a_removed_table(self, entry: str) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id"], public_customers=["id"]),
            PrivacySettings(excluded_tables=[entry]),
        )
        assert drift.is_empty

    def test_an_excluded_schema_explains_every_table_in_it(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id"], hr_salaries=["id"]),
            PrivacySettings(excluded_schemas=["hr"]),
        )
        assert drift.is_empty

    def test_a_genuinely_dropped_table_beside_an_excluded_one_is_still_removed(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id"], public_customers=["id"], public_products=["id"]),
            PrivacySettings(excluded_tables=["public.customers"]),
        )
        assert drift.removed_table_keys == {"public.products"}

    def test_no_privacy_means_nothing_is_explained(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"])),
            _model(public_orders=["id", "status"], public_customers=["id"]),
            None,
        )
        assert drift.removed_table_keys == {"public.customers"}
        assert drift.removed_columns == {"public.orders": {"status"}}


class TestPolicyHidesColumn:
    """One rule for the check and the apply: a stored name is hidden when it or its
    quote-stripped spelling is what the policy excludes."""

    @pytest.mark.parametrize(
        ("excluded", "stored"),
        [("ssn", '"ssn"'), ("ssn", "ssn"), ("o'brien_flag", "o'brien_flag")],
        ids=["quoted-stored-key", "plain", "quote-in-real-name"],
    )
    def test_hidden(self, excluded: str, stored: str) -> None:
        privacy = PrivacySettings(excluded_columns=[f"public.orders.{excluded}"])
        assert policy_hides_column("public.orders", stored, privacy)

    def test_not_hidden(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.orders.ssn"])
        assert not policy_hides_column("public.orders", "email", privacy)
        assert not policy_hides_column("public.orders", "ssn", None)


class TestUnreadableTablesAreNotDrops:
    """A table the connection's role cannot read exists; the adapter names it in
    ``unreadable_tables`` and the store keeps its annotations (``prune_to_schema``'s
    ``keep_unreadable`` rule). It is reported separately, never removed."""

    def test_an_unreadable_stored_table_is_listed_not_removed(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"]), unreadable=["public.payroll"]),
            _model(public_orders=["id"], public_payroll=["id"]),
        )
        assert drift.removed_table_keys == set()
        assert drift.unreadable_table_keys == {"public.payroll"}
        # Nothing to apply: the grant is the fix, not a model edit.
        assert drift.is_empty

    def test_an_unreadable_table_the_model_never_had_is_not_new(self) -> None:
        drift = compute_drift(
            _schema(("public", "orders", ["id"]), unreadable=["public.payroll"]),
            _model(public_orders=["id"]),
        )
        assert drift.new_table_keys == set()
        assert drift.unreadable_table_keys == set()


class TestSummarizeModelChange:
    """The apply's report: what left and what arrived, by name, sorted."""

    @staticmethod
    def _full(**overrides) -> SemanticModel:
        base = {
            "tables": {
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, "legacy": {"display_name": "L"}},
                },
                "public.coupons": {"display_name": "Coupons", "columns": {}},
            },
            "business_metrics": [
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                },
                {
                    "name": "Orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                },
            ],
            "common_joins": [
                {
                    "tables": ["public.orders", "public.coupons"],
                    "join_pattern": "orders.coupon_id = coupons.id",
                    "description": "x",
                }
            ],
            "derived_columns": [
                {"name": "coupon_rate", "sql_expression": "1", "description": "x"},
            ],
            "segments": [{"name": "discounted", "description": "x", "sql_expression": "1=1"}],
            "relationships": [
                {
                    "from_table": "public.orders",
                    "from_column": "coupon_id",
                    "to_table": "public.coupons",
                    "to_column": "id",
                    "join_sql": "public.orders.coupon_id = public.coupons.id",
                }
            ],
            "is_user_reviewed": False,
        }
        return SemanticModel.model_validate({**base, **overrides})

    def test_identical_models_change_nothing(self) -> None:
        summary = summarize_model_change(self._full(), self._full())
        assert summary.changed is False
        assert summary.tables_removed == []
        assert summary.columns_removed == {}

    def test_every_removal_is_named(self) -> None:
        after = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                }
            },
            business_metrics=[
                {
                    "name": "Orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                }
            ],
            common_joins=[],
            derived_columns=[],
            segments=[],
            relationships=[],
        )
        summary = summarize_model_change(self._full(), after)
        assert summary.changed is True
        assert summary.tables_removed == ["public.coupons"]
        assert summary.columns_removed == {"public.orders": ["legacy"]}
        assert summary.metrics_removed == ["Coupon uses"]
        assert summary.joins_removed == ["orders.coupon_id = coupons.id"]
        assert summary.derived_columns_removed == ["coupon_rate"]
        assert summary.segments_removed == ["discounted"]
        assert summary.relationships_removed == ["public.orders.coupon_id → public.coupons.id"]
        assert summary.tables_added == []
        assert summary.metrics_added == []

    def test_additions_are_named_and_a_removed_tables_columns_are_not_listed(self) -> None:
        """A removed table's columns are implied by the table; only surviving tables
        list removed columns."""
        before = self._full()
        after = self._full(
            tables={
                **{k: v.model_dump() for k, v in before.tables.items() if k != "public.coupons"},
                "public.events": {"display_name": "Events", "columns": {}},
            },
            business_metrics=[
                *[m.model_dump() for m in before.business_metrics],
                {
                    "name": "Events",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                },
            ],
        )
        summary = summarize_model_change(before, after)
        assert summary.tables_added == ["public.events"]
        assert summary.tables_removed == ["public.coupons"]
        assert summary.metrics_added == ["Events"]
        assert summary.columns_removed == {}

    def test_quoted_column_keys_are_reported_unquoted(self) -> None:
        before = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, '"Gone"': {"display_name": "G"}},
                }
            }
        )
        after = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                }
            }
        )
        assert summarize_model_change(before, after).columns_removed == {"public.orders": ["Gone"]}

    def test_a_stripped_name_that_is_still_a_live_column_is_reported_verbatim(self) -> None:
        """Dropping a blob-era ``"id"`` key while ``id`` survives must not read as
        ``id`` having been removed."""
        before = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}, '"id"': {"display_name": "Id"}},
                }
            }
        )
        after = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                }
            }
        )
        assert summarize_model_change(before, after).columns_removed == {"public.orders": ['"id"']}

    def test_two_spellings_of_one_removed_column_are_listed_once(self) -> None:
        """A blob-era ``"status"`` key and ``status`` both dropped read as one column."""
        before = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "Id"},
                        "status": {"display_name": "S"},
                        '"status"': {"display_name": "S"},
                    },
                }
            }
        )
        after = self._full(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"id": {"display_name": "Id"}},
                }
            }
        )
        assert summarize_model_change(before, after).columns_removed == {
            "public.orders": ["status"]
        }

    def test_a_removed_item_replaced_under_its_name_is_both_removed_and_added(self) -> None:
        """A diff by name cannot see a replacement; the apply names the items it took
        out, so a same-named arrival is reported as what it is."""
        before = self._full()
        after = self._full(
            business_metrics=[
                {
                    "name": "Coupon uses",
                    "metric_type": "simple",
                    "definition": "COUNT(DISTINCT coupon_id)",
                    "description": "llm",
                    "aggregation": "count",
                },
                {
                    "name": "Orders",
                    "metric_type": "simple",
                    "definition": "COUNT(*)",
                    "description": "x",
                    "aggregation": "count",
                },
            ],
            segments=[],
        )
        summary = summarize_model_change(
            before,
            after,
            removed_items={"metric": ["Coupon uses"], "segment": ["discounted"]},
        )
        assert summary.metrics_removed == ["Coupon uses"]
        assert summary.metrics_added == ["Coupon uses"]
        # Removed and not replaced: removed only.
        assert summary.segments_removed == ["discounted"]
        assert summary.segments_added == []
        assert summary.changed is True

    def test_a_removed_item_replaced_under_its_name_counts_for_every_kind(self) -> None:
        before = self._full()
        summary = summarize_model_change(
            before,
            before,
            removed_items={
                "common_join": ["orders.coupon_id = coupons.id"],
                "derived_column": ["coupon_rate"],
                "segment": ["discounted"],
            },
        )
        assert summary.joins_removed == summary.joins_added == ["orders.coupon_id = coupons.id"]
        assert summary.derived_columns_removed == summary.derived_columns_added == ["coupon_rate"]
        assert summary.segments_removed == summary.segments_added == ["discounted"]

    def test_a_column_added_to_a_surviving_table_is_named(self) -> None:
        before = _model(public_orders=["id"])
        after = _model(public_orders=["id", "loyalty_tier", "region"])
        summary = summarize_model_change(before, after)
        assert summary.columns_added == {"public.orders": ["loyalty_tier", "region"]}
        assert summary.changed is True

    def test_the_columns_of_an_added_table_are_not_listed_as_added_columns(self) -> None:
        summary = summarize_model_change(
            _model(public_orders=["id"]), _model(public_orders=["id"], public_events=["id", "kind"])
        )
        assert summary.tables_added == ["public.events"]
        assert summary.columns_added == {}

    def test_a_near_miss_key_swapped_for_its_bare_column_is_both_removed_and_added(self) -> None:
        """The prune drops a blob-era ``"status"`` key and the apply adds ``status``: the
        removed list keeps the stored spelling (``status`` is still a column, so the
        stripped name would read as the column leaving) and the added list names the
        bare column, so together they read as a spelling swap, not a loss."""
        summary = summarize_model_change(
            _model(public_orders=["id", '"status"']), _model(public_orders=["id", "status"])
        )
        assert summary.columns_removed == {"public.orders": ['"status"']}
        assert summary.columns_added == {"public.orders": ["status"]}

    def test_tables_whose_column_batch_failed_are_a_note_not_a_change(self) -> None:
        model = _model(public_orders=["id"])
        summary = summarize_model_change(
            model, model, column_tables_failed=["public.orders", "public.events"]
        )
        assert summary.column_tables_failed == ["public.events", "public.orders"]
        assert summary.changed is False

    def test_a_logged_summary_from_before_added_columns_loads_with_empty_defaults(self) -> None:
        """The history route validates logged summaries into today's model: an entry
        written before ``columns_added`` / ``column_tables_failed`` existed has neither
        key and must load unchanged."""
        summary = DriftApplySummary.model_validate(
            {"tables_removed": ["public.a"], "tables_not_annotated": ["public.b"]}
        )
        assert summary.tables_removed == ["public.a"]
        assert summary.tables_not_annotated == ["public.b"]
        assert summary.columns_added == {}
        assert summary.column_tables_failed == []
        assert summary.changed is True

    def test_every_list_field_is_structural_or_a_note(self) -> None:
        """A new list on the summary must be counted by ``changed`` or named a note here,
        so it cannot be left out of ``changed`` by omission."""
        from app.semantic import drift

        lists = {
            name
            for name, field in DriftApplySummary.model_fields.items()
            if getattr(field.annotation, "__origin__", None) in (list, dict)
        }
        assert lists == set(drift._STRUCTURAL_FIELDS) | {
            "warnings",
            "tables_not_annotated",
            "column_tables_failed",
        }

    def test_changed_is_the_structural_diff_not_the_notes(self) -> None:
        """An apply that annotated nothing changed nothing: ``changed`` is false even with
        skipped tables and warnings to report, and a logged ``changed`` is recomputed
        from the lists rather than believed."""
        summary = summarize_model_change(
            self._full(), self._full(), tables_not_annotated=["public.audit"], warnings=["w"]
        )
        assert summary.changed is False
        assert summary.tables_not_annotated == ["public.audit"]
        assert summary.warnings == ["w"]
        assert summary.model_dump()["changed"] is False
        assert DriftApplySummary.model_validate({"changed": True}).changed is False
        logged = DriftApplySummary.model_validate(
            {"changed": False, "tables_removed": ["public.a"]}
        )
        assert logged.changed is True


class TestStripIdentQuotes:
    """One normaliser for every identifier comparison; the generator's ``_strip_quotes``
    delegates to it, so the check, the prune and the near-miss remap read a name alike."""

    @pytest.mark.parametrize(
        ("raw", "stripped"),
        [
            ('"public"."Orders"', "public.Orders"),
            ("`sales`.`orders`", "sales.orders"),
            ("'status'", "status"),
            ('"status"', "status"),
            ("  public.orders ", "public.orders"),
            ("o'brien_flag", "o'brien_flag"),
            ("status", "status"),
        ],
        ids=[
            "double-quoted-parts",
            "backticked-parts",
            "single-quoted",
            "double-quoted",
            "whitespace",
            "quote-inside-a-name",
            "plain",
        ],
    )
    def test_strips_surrounding_quotes_per_dotted_part(self, raw: str, stripped: str) -> None:
        assert strip_ident_quotes(raw) == stripped

    def test_the_generator_delegates_to_it(self) -> None:
        from app.semantic.generator import SemanticModelGenerator

        assert (
            SemanticModelGenerator._strip_quotes("`a`.'b'")
            == strip_ident_quotes("`a`.'b'")
            == "a.b"
        )


class TestResolveRealKey:
    """The one near-miss resolver; the generator's ``_resolve_real_key`` delegates to it,
    so the remap, the prune's warning, ``names_any_table`` and the drift check match a
    name alike."""

    _TABLES = ("public.orders", "public.events", "sales.events")

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("public.orders", "public.orders"),
            ('"public"."orders"', "public.orders"),
            ("PUBLIC.Orders", "public.orders"),
            ("public.missing", None),
        ],
        ids=["exact", "quote-stripped", "case-only", "no-match"],
    )
    def test_matches_a_qualified_name_by_quotes_then_case(
        self, name: str, expected: str | None
    ) -> None:
        assert resolve_real_key(name, list(self._TABLES), bare_ok=False) == expected

    def test_a_case_only_match_that_is_not_unique_names_nothing(self) -> None:
        assert resolve_real_key("STATUS", ["Status", "status"], bare_ok=False) is None

    def test_an_exact_match_wins_over_other_case_variants(self) -> None:
        assert resolve_real_key("status", ["Status", "status"], bare_ok=False) == "status"

    def test_a_unique_bare_table_name_resolves_only_when_bare_ok(self) -> None:
        assert resolve_real_key("Orders", list(self._TABLES), bare_ok=True) == "public.orders"
        assert resolve_real_key("Orders", list(self._TABLES), bare_ok=False) is None

    def test_an_ambiguous_bare_table_name_names_nothing(self) -> None:
        assert resolve_real_key("events", list(self._TABLES), bare_ok=True) is None

    def test_the_generator_delegates_to_it(self) -> None:
        from app.semantic.generator import SemanticModelGenerator

        keys = list(self._TABLES)
        for name, bare_ok in [('"Public"."Orders"', False), ("orders", True), ("events", True)]:
            assert SemanticModelGenerator._resolve_real_key(
                name, keys, bare_ok=bare_ok
            ) == resolve_real_key(name, keys, bare_ok=bare_ok)
