# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for entity_resolver — NER extraction and schema sample-value matching."""

from __future__ import annotations

from app.datasources.models import ColumnInfo, DataSourceSchema, TableInfo
from app.services.entity_resolver import extract_entity_candidates, resolve_entities

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_schema(
    table_name: str,
    col_name: str,
    sample_values: list[str] | None,
    schema_name: str = "public",
) -> DataSourceSchema:
    col = ColumnInfo(
        name=col_name,
        data_type="text",
        native_type="text",
        sample_values=sample_values,
    )
    table = TableInfo(
        catalog=None,
        schema_name=schema_name,
        name=table_name,
        table_type="BASE TABLE",
        columns=[col],
    )
    return DataSourceSchema(source_type="postgresql", tables=[table])


# ── extract_entity_candidates ──────────────────────────────────────────────────


class TestExtractEntityCandidates:
    def test_extracts_double_quoted_string(self):
        result = extract_entity_candidates('show orders for "Acme Corp"')
        assert "Acme Corp" in result

    def test_extracts_single_quoted_string(self):
        result = extract_entity_candidates("filter by 'Q1 2024'")
        assert "Q1 2024" in result

    def test_extracts_capitalized_multi_word(self):
        result = extract_entity_candidates("orders from North America last month")
        assert "North America" in result

    def test_ignores_single_stop_word(self):
        result = extract_entity_candidates("Show me the orders")
        assert "Show" not in result

    def test_ignores_multi_stop_word_phrase(self):
        # "First All" — both are stop words individually; the phrase starts with a stop word
        result = extract_entity_candidates("How Many orders were placed")
        assert "How Many" not in result

    def test_deduplicates_case_insensitively(self):
        # Quoted then also appears as capitalized — should appear once
        result = extract_entity_candidates('"Acme Corp" orders for Acme Corp')
        assert result.count("Acme Corp") == 1

    def test_quoted_takes_priority_over_capitalized(self):
        # The quoted version is extracted first; capitalized version is deduplicated away
        result = extract_entity_candidates('"Acme Corp" and Acme Corp both appear')
        assert result[0] == "Acme Corp"
        assert len([c for c in result if c.lower() == "acme corp"]) == 1

    def test_empty_question_returns_empty(self):
        assert extract_entity_candidates("") == []

    def test_plain_lowercase_returns_empty(self):
        assert extract_entity_candidates("how many orders were placed last month") == []

    def test_single_capitalized_word_not_extracted(self):
        # Single words are not multi-word; regex requires at least two tokens
        result = extract_entity_candidates("orders from Europe")
        assert "Europe" not in result

    def test_min_length_two_chars_for_quoted(self):
        # Single-char quoted strings are ignored
        result = extract_entity_candidates("filter by 'a'")
        assert result == []


# ── resolve_entities ───────────────────────────────────────────────────────────


class TestResolveEntities:
    def test_exact_sample_value_match(self):
        schema = _make_schema("customers", "company_name", ["Acme Corp", "Beta LLC"])
        notes = resolve_entities(["Acme Corp"], schema)
        assert len(notes) == 1
        assert "Acme Corp" in notes[0]
        assert "company_name" in notes[0]

    def test_case_insensitive_match(self):
        schema = _make_schema("orders", "status", ["ACTIVE", "INACTIVE"])
        notes = resolve_entities(["active"], schema)
        assert len(notes) == 1

    def test_partial_substring_match(self):
        # candidate is substring of sample value
        schema = _make_schema("customers", "name", ["Acme Corporation"])
        notes = resolve_entities(["Acme Corp"], schema)
        # "acme corp" in "acme corporation" → match
        assert len(notes) == 1

    def test_no_match_returns_empty(self):
        schema = _make_schema("orders", "status", ["active", "inactive"])
        notes = resolve_entities(["xyz_nonexistent_entity"], schema)
        assert notes == []

    def test_no_sample_values_column_skipped(self):
        schema = _make_schema("orders", "id", None)
        notes = resolve_entities(["Acme"], schema)
        assert notes == []

    def test_empty_sample_values_column_skipped(self):
        schema = _make_schema("orders", "id", [])
        notes = resolve_entities(["Acme"], schema)
        assert notes == []

    def test_caps_match_display_at_three(self):
        # 5 columns all matching "Acme" → note must mention at most 3
        cols = [
            ColumnInfo(
                name=f"col{i}",
                data_type="text",
                native_type="text",
                sample_values=["Acme Corp"],
            )
            for i in range(5)
        ]
        table = TableInfo(
            catalog=None,
            schema_name="public",
            name="t",
            table_type="BASE TABLE",
            columns=cols,
        )
        schema = DataSourceSchema(source_type="postgresql", tables=[table])
        notes = resolve_entities(["Acme"], schema)
        assert len(notes) == 1
        # count semicolons to determine how many matches are mentioned
        match_count = notes[0].count(";") + 1
        assert match_count <= 3

    def test_qualified_table_name_when_non_public_schema(self):
        schema = _make_schema("orders", "status", ["active"], schema_name="sales")
        notes = resolve_entities(["active"], schema)
        assert len(notes) == 1
        assert "sales.orders" in notes[0]

    def test_public_schema_omitted_from_table_label(self):
        schema = _make_schema("orders", "status", ["active"], schema_name="public")
        notes = resolve_entities(["active"], schema)
        assert len(notes) == 1
        assert "public.orders" not in notes[0]
        assert "orders.status" in notes[0]

    def test_empty_candidates_returns_empty(self):
        schema = _make_schema("customers", "name", ["Acme Corp"])
        assert resolve_entities([], schema) == []

    def test_multiple_candidates_produce_separate_notes(self):
        col1 = ColumnInfo(
            name="company", data_type="text", native_type="text", sample_values=["Acme Corp"]
        )
        col2 = ColumnInfo(
            name="region", data_type="text", native_type="text", sample_values=["North America"]
        )
        table = TableInfo(
            catalog=None,
            schema_name="public",
            name="customers",
            table_type="BASE TABLE",
            columns=[col1, col2],
        )
        schema = DataSourceSchema(source_type="postgresql", tables=[table])
        notes = resolve_entities(["Acme Corp", "North America"], schema)
        assert len(notes) == 2
