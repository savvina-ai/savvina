# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _compress_prompt() and _compact_semantic_model() in chat_service."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.datasources.models import DataSourceSchema, TableInfo
from app.semantic.models import (
    AggregationType,
    SemanticModel,
    SimpleMetric,
    TableSemantic,
)
from app.services.pipeline import (
    _PROTECTED_TABLE_COUNT,
    _compact_semantic_model,
    _compress_prompt,
    _split_schema_by_priority,
)
from app.services.prompt_builder import PromptBuilder


def _make_builder(*prompt_sizes: int) -> MagicMock:
    """Return a mock PromptBuilder whose build_system_prompt returns strings of given lengths."""
    builder = MagicMock(spec=PromptBuilder)
    builder.build_system_prompt.side_effect = ["x" * s for s in prompt_sizes]
    return builder


class TestCompactSemanticModel:
    def test_clears_tables(self) -> None:
        model = SemanticModel(tables={"public.orders": TableSemantic(display_name="Orders")})
        result = _compact_semantic_model(model)
        assert result.tables == {}

    def test_preserves_business_metrics(self) -> None:
        metric = SimpleMetric(
            name="Revenue",
            definition="SUM(total_amount)",
            aggregation=AggregationType.SUM,
            description="Total revenue",
        )
        model = SemanticModel(
            tables={"public.orders": TableSemantic(display_name="Orders")},
            business_metrics=[metric],
        )
        result = _compact_semantic_model(model)
        assert len(result.business_metrics) == 1
        assert result.business_metrics[0].name == "Revenue"

    def test_preserves_time_expressions(self) -> None:
        model = SemanticModel(time_expressions={"today": "CURRENT_DATE"})
        result = _compact_semantic_model(model)
        assert result.time_expressions == {"today": "CURRENT_DATE"}

    def test_clears_relationships_and_joins(self) -> None:
        model = SemanticModel(tables={})
        result = _compact_semantic_model(model)
        assert result.relationships == []
        assert result.common_joins == []
        assert result.derived_columns == []

    def test_original_is_not_mutated(self) -> None:
        model = SemanticModel(tables={"public.orders": TableSemantic(display_name="Orders")})
        _compact_semantic_model(model)
        assert "public.orders" in model.tables


class TestCompressPrompt:
    def test_full_prompt_fits_returned_without_modification(self) -> None:
        """When the full prompt fits within budget, it is returned on the first attempt."""
        builder = _make_builder(100)
        schema = DataSourceSchema(source_type="postgresql")

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=MagicMock(),
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=200,  # 100 (prompt) + 1 (user_message "q") well under 200
        )

        assert len(result) == 100
        assert builder.build_system_prompt.call_count == 1

    def test_drops_few_shot_when_over_budget(self) -> None:
        """Over budget with few-shot → second attempt strips few-shot and fits."""
        # First call (with few-shot): 300 chars; second call (no few-shot): 90 chars
        builder = _make_builder(300, 90)
        schema = DataSourceSchema(source_type="postgresql")

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=MagicMock(),
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        assert len(result) == 90
        assert builder.build_system_prompt.call_count == 2
        second_kwargs = builder.build_system_prompt.call_args_list[1].kwargs
        assert second_kwargs["few_shot_examples"] is None

    def test_uses_compact_semantic_at_level_3(self) -> None:
        """Level 1+2 over budget → level 3 uses compact semantic (not None) and fits."""
        # First (full): over; second (no few-shot): over; third (compact semantic): fits
        builder = _make_builder(300, 300, 80)
        schema = DataSourceSchema(source_type="postgresql")
        semantic = SemanticModel(
            tables={"public.orders": TableSemantic(display_name="Orders")},
        )

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=semantic,
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        assert len(result) == 80
        assert builder.build_system_prompt.call_count == 3
        third_kwargs = builder.build_system_prompt.call_args_list[2].kwargs
        assert third_kwargs["few_shot_examples"] is None
        # Compact semantic passed — not None, but tables dict is empty
        assert third_kwargs["semantic_model"] is not None
        assert third_kwargs["semantic_model"].tables == {}

    def test_drops_semantic_completely_at_level_4(self) -> None:
        """Levels 1-3 over budget → level 4 passes semantic_model=None."""
        # First/second/third: over; fourth (no semantic): fits
        builder = _make_builder(300, 300, 300, 80)
        schema = DataSourceSchema(source_type="postgresql")

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=MagicMock(),
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        assert len(result) == 80
        assert builder.build_system_prompt.call_count == 4
        fourth_kwargs = builder.build_system_prompt.call_args_list[3].kwargs
        assert fourth_kwargs["few_shot_examples"] is None
        assert fourth_kwargs["semantic_model"] is None

    def test_returns_smallest_when_nothing_fits(self) -> None:
        """Even fully stripped prompt exceeds budget → returns the smallest result anyway."""
        builder = _make_builder(300, 250, 200, 150, 120)  # all over budget of 100
        schema = DataSourceSchema(source_type="postgresql")

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=MagicMock(),
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        # Returns the last (smallest) prompt regardless
        assert len(result) == 120
        assert builder.build_system_prompt.call_count == 5

    def test_level_5_passes_schema_override(self) -> None:
        """All 4 loops over budget → level-5 calls build_system_prompt with schema_override."""
        # 4 loop calls all over budget, then the level-5 explicit call fits
        builder = _make_builder(300, 300, 300, 300, 80)
        schema = DataSourceSchema(source_type="postgresql")
        datasource = MagicMock()
        datasource.format_schema_for_llm.return_value = "-- schema part --"

        result = _compress_prompt(
            builder=builder,
            datasource=datasource,
            schema=schema,
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        assert len(result) == 80
        assert builder.build_system_prompt.call_count == 5
        level5_kwargs = builder.build_system_prompt.call_args_list[4].kwargs
        assert level5_kwargs["schema_override"] is not None
        assert level5_kwargs["few_shot_examples"] is None
        assert level5_kwargs["semantic_model"] is None

    def test_level_5_calls_format_schema_for_llm_twice(self) -> None:
        """Level-5 block renders schema in two tiers: once for protected tables, once for rest."""
        builder = _make_builder(300, 300, 300, 300, 50)
        schema = DataSourceSchema(source_type="postgresql")
        datasource = MagicMock()
        datasource.format_schema_for_llm.return_value = ""

        _compress_prompt(
            builder=builder,
            datasource=datasource,
            schema=schema,
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        # Two calls: one for protected tables (original privacy), one for remainder (_min_pvt)
        assert datasource.format_schema_for_llm.call_count == 2

    def test_no_semantic_model_skips_compact_level(self) -> None:
        """When semantic_model is None, compact_semantic is also None — no crash."""
        # First (full, no semantic): over; second: over; third (compact=None): over; fourth: fits
        builder = _make_builder(300, 300, 300, 80)
        schema = DataSourceSchema(source_type="postgresql")

        result = _compress_prompt(
            builder=builder,
            datasource=MagicMock(),
            schema=schema,
            privacy=None,
            semantic_model=None,  # no semantic at all
            few_shot_examples=[MagicMock()],
            user_question="q",
            user_message="q",
            budget_chars=100,
        )

        assert len(result) == 80
        assert builder.build_system_prompt.call_count == 4  # 4 loop levels only, no level-5


def _make_table(name: str) -> TableInfo:
    return TableInfo(catalog=None, schema_name="public", name=name, table_type="BASE TABLE")


class TestSplitSchemaByPriority:
    def test_protected_contains_first_n_tables(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(5)]
        schema = DataSourceSchema(source_type="postgresql", tables=tables)
        protected, _ = _split_schema_by_priority(schema, max_protected=3)
        assert [t.name for t in protected.tables] == ["t0", "t1", "t2"]

    def test_remainder_contains_rest(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(5)]
        schema = DataSourceSchema(source_type="postgresql", tables=tables)
        _, rest = _split_schema_by_priority(schema, max_protected=3)
        assert [t.name for t in rest.tables] == ["t3", "t4"]

    def test_fewer_tables_than_max_protected(self) -> None:
        """When schema has fewer tables than max_protected, all go to protected, rest is empty."""
        tables = [_make_table("only")]
        schema = DataSourceSchema(source_type="postgresql", tables=tables)
        protected, rest = _split_schema_by_priority(schema, max_protected=3)
        assert len(protected.tables) == 1
        assert len(rest.tables) == 0

    def test_empty_schema(self) -> None:
        schema = DataSourceSchema(source_type="postgresql")
        protected, rest = _split_schema_by_priority(schema)
        assert protected.tables == []
        assert rest.tables == []

    def test_metadata_preserved_in_both_halves(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(4)]
        schema = DataSourceSchema(
            source_type="bigquery",
            tables=tables,
            metadata={"db": "prod"},
        )
        protected, rest = _split_schema_by_priority(schema, max_protected=2)
        assert protected.source_type == "bigquery"
        assert protected.metadata == {"db": "prod"}
        assert rest.source_type == "bigquery"
        assert rest.metadata == {"db": "prod"}

    def test_default_max_protected_matches_constant(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(10)]
        schema = DataSourceSchema(source_type="postgresql", tables=tables)
        protected, _ = _split_schema_by_priority(schema)
        assert len(protected.tables) == _PROTECTED_TABLE_COUNT
