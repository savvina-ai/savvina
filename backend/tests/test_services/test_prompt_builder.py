# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for PromptBuilder — the system-prompt assembly service."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.cache.example_library import ExampleEntry
from app.datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    SchemaInfo,
    TableInfo,
)
from app.semantic.models import AggregationType, SemanticModel, SimpleMetric, TableSemantic
from app.services.prompt_builder import PromptBuilder

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_adapter(display_name: str = "PostgreSQL", dialect: str = "postgresql") -> MagicMock:
    """Return a minimal mock datasource adapter."""
    adapter = MagicMock()
    adapter.display_name = display_name
    adapter.query_dialect = dialect
    adapter.get_system_prompt_additions = MagicMock(return_value="-- dialect hint --")
    adapter.format_schema_for_llm = MagicMock(return_value="-- schema DDL --")
    return adapter


def _minimal_schema() -> DataSourceSchema:
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
    )


def _make_example(
    question: str = "How many rows?",
    query: str = "SELECT COUNT(*) FROM t",
) -> ExampleEntry:
    return ExampleEntry(id="ex1", question=question, query=query, query_dialect="postgresql")


def _make_semantic_model_with_tables() -> SemanticModel:
    return SemanticModel(
        tables={"public.orders": TableSemantic(display_name="Orders", description="Sales orders")}
    )


def _build_prompt(
    *,
    adapter=None,
    schema=None,
    privacy=None,
    semantic_model=None,
    few_shot_examples=None,
) -> str:
    adapter = adapter or _make_adapter()
    schema = schema or _minimal_schema()
    return PromptBuilder().build_system_prompt(
        datasource=adapter,
        schema=schema,
        privacy=privacy,
        semantic_model=semantic_model,
        few_shot_examples=few_shot_examples,
    )


# ── Base instructions ──────────────────────────────────────────────────────────


class TestPromptBuilderBaseInstructions:
    def test_contains_savvina_ai(self):
        prompt = _build_prompt()
        assert "Savvina AI" in prompt

    def test_contains_datasource_display_name(self):
        prompt = _build_prompt(adapter=_make_adapter(display_name="MySQL"))
        assert "MySQL" in prompt

    def test_contains_query_dialect(self):
        prompt = _build_prompt(adapter=_make_adapter(dialect="mysql"))
        assert "mysql" in prompt

    def test_includes_dialect_specific_additions(self):
        adapter = _make_adapter()
        adapter.get_system_prompt_additions.return_value = "LIMIT is mandatory"
        prompt = _build_prompt(adapter=adapter)
        assert "LIMIT is mandatory" in prompt

    def test_includes_database_schema_header(self):
        prompt = _build_prompt()
        assert "## Database Schema" in prompt

    def test_includes_formatted_schema(self):
        adapter = _make_adapter()
        adapter.format_schema_for_llm.return_value = "CREATE TABLE orders (...)"
        prompt = _build_prompt(adapter=adapter)
        assert "CREATE TABLE orders" in prompt

    def test_format_schema_called_with_privacy(self):
        adapter = _make_adapter()
        privacy = PrivacySettings(include_sample_values=False)
        _build_prompt(adapter=adapter, privacy=privacy)
        adapter.format_schema_for_llm.assert_called_once_with(_minimal_schema(), privacy)

    def test_schema_override_replaces_format_schema_for_llm(self):
        """When schema_override is supplied, format_schema_for_llm is skipped entirely."""
        adapter = _make_adapter()
        prompt = PromptBuilder().build_system_prompt(
            datasource=adapter,
            schema=_minimal_schema(),
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            schema_override="-- tiered schema override --",
        )
        adapter.format_schema_for_llm.assert_not_called()
        assert "-- tiered schema override --" in prompt

    def test_schema_override_none_falls_through_to_format_schema_for_llm(self):
        """When schema_override is None (default), format_schema_for_llm is called normally."""
        adapter = _make_adapter()
        PromptBuilder().build_system_prompt(
            datasource=adapter,
            schema=_minimal_schema(),
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            schema_override=None,
        )
        adapter.format_schema_for_llm.assert_called_once()


# ── Output format instructions ─────────────────────────────────────────────────


class TestPromptBuilderOutputFormat:
    def test_contains_response_format_header(self):
        prompt = _build_prompt()
        assert "## Response Format" in prompt

    def test_contains_query_marker(self):
        prompt = _build_prompt()
        assert "QUERY:" in prompt

    def test_contains_explanation_marker(self):
        prompt = _build_prompt()
        assert "EXPLANATION:" in prompt

    def test_contains_sql_code_fence(self):
        prompt = _build_prompt()
        assert "```sql" in prompt


# ── Safety rules ───────────────────────────────────────────────────────────────


class TestPromptBuilderSafetyRules:
    def setup_method(self):
        self.prompt = _build_prompt()

    def test_contains_rules_header(self):
        assert "## Rules" in self.prompt

    def test_contains_read_only_restriction(self):
        # Must mention the dangerous statements that are forbidden
        assert "INSERT" in self.prompt
        assert "UPDATE" in self.prompt
        assert "DELETE" in self.prompt

    def test_contains_limit_default(self):
        assert "LIMIT" in self.prompt

    def test_contains_null_handling(self):
        assert "NULL" in self.prompt

    def test_eight_rules_are_numbered(self):
        # Rules 1-8 should all be present
        for i in range(1, 9):
            assert f"{i}." in self.prompt


# ── Semantic model section ─────────────────────────────────────────────────────


class TestPromptBuilderSemanticModel:
    def test_includes_business_context_header_when_model_present(self):
        prompt = _build_prompt(semantic_model=_make_semantic_model_with_tables())
        assert "## Business Context" in prompt

    def test_includes_formatted_semantic_content(self):
        prompt = _build_prompt(semantic_model=_make_semantic_model_with_tables())
        # SemanticFormatter renders "SEMANTIC CONTEXT:"
        assert "SEMANTIC CONTEXT:" in prompt

    def test_omits_business_context_when_semantic_model_is_none(self):
        prompt = _build_prompt(semantic_model=None)
        assert "## Business Context" not in prompt

    def test_omits_business_context_when_tables_dict_is_empty_and_no_metrics(self):
        empty_model = SemanticModel(tables={})
        prompt = _build_prompt(semantic_model=empty_model)
        assert "## Business Context" not in prompt

    def test_includes_business_context_for_compact_model_with_metrics(self):
        """Compact semantic (empty tables, non-empty business_metrics) must render Business
        Context."""
        metric = SimpleMetric(
            name="Revenue",
            definition="SUM(total_amount)",
            aggregation=AggregationType.SUM,
            description="Total revenue",
        )
        compact = SemanticModel(tables={}, business_metrics=[metric])
        prompt = _build_prompt(semantic_model=compact)
        assert "## Business Context" in prompt
        assert "Revenue" in prompt

    def test_includes_business_context_when_only_time_expressions_present(self):
        model = SemanticModel(tables={}, time_expressions={"today": "CURRENT_DATE"})
        prompt = _build_prompt(semantic_model=model, adapter=_make_adapter(dialect="postgresql"))
        # Time expressions only render for temporal questions; use a temporal user_question
        prompt = PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=_minimal_schema(),
            privacy=None,
            semantic_model=model,
            few_shot_examples=None,
            user_question="what happened last week",
        )
        assert "## Business Context" in prompt


# ── Few-shot examples section ──────────────────────────────────────────────────


class TestPromptBuilderFewShotExamples:
    def test_includes_example_queries_header_when_examples_present(self):
        prompt = _build_prompt(few_shot_examples=[_make_example()])
        assert "## Example Queries" in prompt

    def test_includes_question_prefix(self):
        ex = _make_example(question="Top customers?")
        prompt = _build_prompt(few_shot_examples=[ex])
        assert "Q: Top customers?" in prompt

    def test_includes_query_prefix(self):
        ex = _make_example(query="SELECT name FROM customers LIMIT 5")
        prompt = _build_prompt(few_shot_examples=[ex])
        assert "Query: SELECT name FROM customers LIMIT 5" in prompt

    def test_includes_multiple_examples(self):
        examples = [
            _make_example(question="Q1", query="SELECT 1"),
            _make_example(question="Q2", query="SELECT 2"),
        ]
        prompt = _build_prompt(few_shot_examples=examples)
        assert "Q: Q1" in prompt
        assert "Q: Q2" in prompt

    def test_omits_examples_section_when_none(self):
        prompt = _build_prompt(few_shot_examples=None)
        assert "## Example Queries" not in prompt

    def test_omits_examples_section_when_empty_list(self):
        prompt = _build_prompt(few_shot_examples=[])
        assert "## Example Queries" not in prompt


# ── Section ordering ───────────────────────────────────────────────────────────


class TestPromptBuilderSectionOrder:
    """Verify the prompt sections appear in the correct order."""

    def test_base_instructions_before_schema(self):
        prompt = _build_prompt()
        assert prompt.index("Savvina AI") < prompt.index("## Database Schema")

    def test_schema_before_format_instructions(self):
        prompt = _build_prompt()
        assert prompt.index("## Database Schema") < prompt.index("## Response Format")

    def test_format_instructions_before_safety_rules(self):
        prompt = _build_prompt()
        assert prompt.index("## Response Format") < prompt.index("## Rules")

    def test_business_context_before_format_instructions(self):
        prompt = _build_prompt(semantic_model=_make_semantic_model_with_tables())
        assert prompt.index("## Business Context") < prompt.index("## Response Format")

    def test_examples_before_format_instructions(self):
        prompt = _build_prompt(few_shot_examples=[_make_example()])
        assert prompt.index("## Example Queries") < prompt.index("## Response Format")

    def test_intent_hint_before_schema(self):
        """Intent hint (Query Pattern Guidance) must appear before the schema section."""
        prompt = PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=_minimal_schema(),
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            user_question="show me the top 10 customers by revenue",
        )
        assert "## Query Pattern Guidance" in prompt
        assert prompt.index("## Query Pattern Guidance") < prompt.index("## Database Schema")


# ── Conditional time expressions ───────────────────────────────────────────────


class TestPromptBuilderTimeExpressions:
    """Time expressions should only be injected for temporal questions."""

    def _build_with_time_exprs(self, question: str) -> str:
        model = SemanticModel(
            tables={"public.t": TableSemantic(display_name="T")},
            time_expressions={"today": "CURRENT_DATE", "this_month": "DATE_TRUNC('month', NOW())"},
        )
        return PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=_minimal_schema(),
            privacy=None,
            semantic_model=model,
            few_shot_examples=None,
            user_question=question,
        )

    def test_time_expressions_included_for_temporal_question(self):
        prompt = self._build_with_time_exprs("show revenue for last month")
        assert "TIME EXPRESSIONS" in prompt

    def test_time_expressions_omitted_for_non_temporal_question(self):
        prompt = self._build_with_time_exprs("show me the top 10 customers by revenue")
        assert "TIME EXPRESSIONS" not in prompt


# ── Named entity resolution ────────────────────────────────────────────────────


def _make_schema_with_samples(
    table_name: str,
    col_name: str,
    sample_values: list[str],
) -> DataSourceSchema:
    col = ColumnInfo(
        name=col_name, data_type="text", native_type="text", sample_values=sample_values
    )
    table = TableInfo(
        catalog=None, schema_name="public", name=table_name, table_type="BASE TABLE", columns=[col]
    )
    return DataSourceSchema(source_type="postgresql", tables=[table])


class TestPromptBuilderEntityResolution:
    def test_resolution_note_injected_when_sample_value_matches(self):
        """Quoted entity matching a sample value produces a resolution note in the prompt."""
        schema = _make_schema_with_samples("customers", "company_name", ["Acme Corp", "Beta LLC"])
        prompt = PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=schema,
            privacy=None,  # include_sample_values defaults to True
            semantic_model=None,
            few_shot_examples=None,
            user_question='show orders for "Acme Corp"',
        )
        assert "Acme Corp" in prompt
        assert "may match" in prompt

    def test_no_note_when_entity_has_no_match(self):
        """Entity with no matching sample value produces no resolution note (silent)."""
        schema = _make_schema_with_samples("orders", "status", ["active", "inactive"])
        prompt = PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=schema,
            privacy=None,
            semantic_model=None,
            few_shot_examples=None,
            user_question='show me "XYZ Corp" orders',
        )
        # No crash; no spurious "may match" note for a non-existent entity
        assert "may match" not in prompt

    def test_ner_skipped_when_sample_values_disabled(self):
        """NER is gated behind include_sample_values; with it False, no entity notes appear."""
        schema = _make_schema_with_samples("customers", "company_name", ["Acme Corp"])
        privacy = PrivacySettings(include_sample_values=False)
        prompt = PromptBuilder().build_system_prompt(
            datasource=_make_adapter(),
            schema=schema,
            privacy=privacy,
            semantic_model=None,
            few_shot_examples=None,
            user_question='show orders for "Acme Corp"',
        )
        assert "may match" not in prompt
