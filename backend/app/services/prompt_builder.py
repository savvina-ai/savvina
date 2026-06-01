# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Assembles the full system prompt sent to the LLM on every chat turn."""

from __future__ import annotations

import re

from ..cache.example_library import ExampleEntry
from ..cache.query_cache import has_temporal_reference
from ..datasources.base import BaseDataSource
from ..datasources.models import DataSourceSchema, PrivacySettings
from ..semantic.formatter import SemanticFormatter
from ..semantic.models import SemanticModel
from .entity_resolver import extract_entity_candidates, resolve_entities
from .intent_classifier import IntentClassifier

# Matches "top 5", "first 10", "show 20 rows", "limit 100", "50 results", etc.
_LIMIT_RE = re.compile(
    r"\b(?:top|first|show|display|list|limit|last)\s+(\d+)\b"
    r"|\b(\d+)\s+(?:results?|rows?|records?|entries?|items?)\b",
    re.IGNORECASE,
)

# Maps natural-language comparisons to SQL operators.
_CONDITION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(?:more than|greater than|above|over|exceeds?)\s+([\d,]+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
        ">",
    ),
    (
        re.compile(r"\b(?:less than|fewer than|below|under)\s+([\d,]+(?:\.\d+)?)\b", re.IGNORECASE),
        "<",
    ),
    (
        re.compile(r"\b(?:at least|minimum of|no less than)\s+([\d,]+(?:\.\d+)?)\b", re.IGNORECASE),
        ">=",
    ),
    (
        re.compile(r"\b(?:at most|maximum of|no more than)\s+([\d,]+(?:\.\d+)?)\b", re.IGNORECASE),
        "<=",
    ),
    (re.compile(r"\b(?:equal to|exactly)\s+([\d,]+(?:\.\d+)?)\b", re.IGNORECASE), "="),
]


class PromptBuilder:
    """Builds a complete system prompt from schema, semantic model, and examples."""

    def __init__(self) -> None:
        self._classifier = IntentClassifier()

    def build_system_prompt(
        self,
        datasource: BaseDataSource,
        schema: DataSourceSchema,
        privacy: PrivacySettings | None,
        semantic_model: SemanticModel | None,
        few_shot_examples: list[ExampleEntry] | None,
        user_question: str | None = None,
        schema_override: str | None = None,
    ) -> str:
        """Construct the full system prompt with all available context."""

        parts = []

        # 1. Base instructions
        parts.append(self._base_instructions(datasource))

        # 2. Intent hint — placed early so the LLM frames its schema reading around
        #    the query type (higher weight than instructions buried after the schema)
        if user_question:
            intent = self._classifier.classify(user_question)
            hint = self._classifier.get_intent_prompt_hint(intent)
            if hint:
                parts.append(f"## Query Pattern Guidance\n{hint}")

        # 2b. Entity context — focused tables + extracted LIMIT/conditions from the
        #     question, placed before the full schema so the LLM anchors on them.
        if user_question:
            entity_ctx = self._entity_context(user_question, schema, privacy)
            if entity_ctx:
                parts.append(entity_ctx)

        # 3. Dialect-specific additions
        parts.append(datasource.get_system_prompt_additions())

        # 4. Schema context (respects privacy settings)
        parts.append("## Database Schema")
        if schema_override is not None:
            parts.append(schema_override)
        else:
            parts.append(datasource.format_schema_for_llm(schema, privacy))

        # 5. Semantic model (if available)
        #    Time expressions are only injected for temporal questions to save tokens.
        if semantic_model and (
            semantic_model.tables
            or semantic_model.business_metrics
            or semantic_model.segments
            or semantic_model.time_expressions
        ):
            is_temporal = has_temporal_reference(user_question) if user_question else True
            parts.append("## Business Context")
            parts.append(
                SemanticFormatter().format_for_prompt(
                    semantic_model,
                    include_time_exprs=is_temporal,
                )
            )

        # 6. Few-shot examples (if available)
        if few_shot_examples:
            parts.append("## Example Queries")
            parts.append(
                "Here are verified examples of questions and their correct"
                " queries for this database:"
            )
            for ex in few_shot_examples:
                parts.append(f"Q: {ex.question}")
                parts.append(f"Query: {ex.query}")
                parts.append("")

        # 7. Output format instructions
        parts.append(self._output_format_instructions())

        # 8. Safety rules
        parts.append(self._safety_rules())

        return "\n\n".join(parts)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _entity_context(
        self,
        user_question: str,
        schema: DataSourceSchema,
        privacy: PrivacySettings | None = None,
    ) -> str | None:
        """Build a brief focus block from the pruned schema and extracted question entities.

        Surfaces table names, a user-stated LIMIT, numeric comparison conditions,
        and (when sample values are permitted) named entity resolution hints.
        Placed near the top of the prompt so the LLM anchors on them before the DDL.
        """
        bullets: list[str] = []

        # Tables already pruned by _select_relevant_tables() — surface them explicitly.
        if schema.tables:
            names = [
                t.name if t.schema_name in ("public", "", None) else f"{t.schema_name}.{t.name}"
                for t in schema.tables
            ]
            bullets.append(f"Tables in scope: {', '.join(names)}")

        # Explicit LIMIT from the question ("top 5", "show 20 results", etc.)
        limit_match = _LIMIT_RE.search(user_question)
        if limit_match:
            limit_val = limit_match.group(1) or limit_match.group(2)
            bullets.append(f"User-specified result size: {limit_val} rows — use LIMIT {limit_val}")

        # Numeric comparison conditions ("more than 1000", "at least 50", etc.)
        for pattern, operator in _CONDITION_PATTERNS:
            m = pattern.search(user_question)
            if m:
                raw = m.group(1).replace(",", "")
                bullets.append(f"Detected numeric filter: {operator} {raw}")

        # Named entity resolution — only when sample values are already permitted to
        # reach the LLM (respects the app's no-data-to-LLM privacy guarantee).
        if privacy is None or privacy.include_sample_values is not False:
            candidates = extract_entity_candidates(user_question)
            if candidates:
                for note in resolve_entities(candidates, schema):
                    bullets.append(note)

        if not bullets:
            return None

        lines = "\n".join(f"- {b}" for b in bullets)
        return f"## Query Focus\n{lines}"

    def _base_instructions(self, datasource: BaseDataSource) -> str:
        return (
            "You are Savvina AI, an expert data analyst assistant. "
            "You help users query their data by generating precise, read-only queries.\n\n"
            f"You are connected to a {datasource.display_name} database "
            f"using {datasource.query_dialect} dialect."
        )

    def _output_format_instructions(self) -> str:
        return (
            "## Response Format\n"
            "ALWAYS respond in this exact format:\n\n"
            "REASONING:\n"
            "1. Tables needed: <which tables are required and why>\n"
            "2. Joins required: <how tables connect, or 'none' if single table>\n"
            "3. Filtering/grouping: <WHERE conditions, GROUP BY, HAVING — or 'none'>\n"
            "4. Output columns: <what to SELECT, any aggregates, aliases>\n"
            "5. CTE plan (if using CTEs): <list each CTE name and every column it must"
            " expose to downstream CTEs or the outer SELECT — verify this list before"
            " writing any CTE>\n\n"
            "QUERY:\n"
            "```sql\n"
            "YOUR QUERY HERE\n"
            "```\n\n"
            "EXPLANATION:\n"
            "Brief explanation of what the query does and why you chose this approach."
        )

    def _safety_rules(self) -> str:
        return (
            "## Rules\n"
            "1. Generate ONLY read-only queries. NEVER generate INSERT, UPDATE, DELETE, "
            "DROP, CREATE, ALTER, TRUNCATE, or any data modification statements.\n"
            "2. ONLY reference tables and columns that are explicitly listed in the "
            "Database Schema above. NEVER invent or assume a column exists. If the schema "
            "does not contain a column needed for the query, say so in the EXPLANATION "
            "and do not generate a QUERY block.\n"
            "3. Use fully qualified table names where applicable.\n"
            "4. Add a LIMIT clause if the user doesn't specify. Default to 20 rows.\n"
            "5. Use descriptive column aliases.\n"
            "6. Handle NULLs appropriately.\n"
            "7. For date-based questions, use dialect-appropriate date functions.\n"
            "8. If the semantic context defines default filters for a table, apply them "
            "unless the user explicitly asks to include filtered-out records.\n"
            "9. If the semantic context defines a business metric, use its exact definition.\n"
            "10. Never SELECT a column marked [SENSITIVE] in the schema. If your query would "
            "naturally include a sensitive column (e.g. email, SSN), simply omit it and use "
            "non-sensitive identifiers instead (e.g. customer_id, display_name). Only refuse "
            "with an EXPLANATION and no QUERY block if the sensitive column IS the core data "
            "the user is explicitly asking for (e.g. 'show me all customer emails').\n"
            "11. Only add WHERE clause filters that are explicitly stated in the user's question. "
            "Never infer or assume filters (e.g. cuisine type, city, status, category) that the "
            "user did not mention. If the user asks for 'top 10 restaurants by rating', do not "
            "silently add a cuisine or city filter.\n"
            "12. When the Business Context defines value mappings for a column "
            "(shown as Label='raw_value' with [SQL values: ...]), those are the ONLY valid values "
            "for that column. ALWAYS use the exact raw_value in WHERE clauses — never invent or "
            "assume other values from general knowledge (e.g. if mappings show "
            "Delivered='delivered', do NOT use 'completed', 'done', or any other value).\n"
            "13. Multi-CTE queries: every column referenced in a downstream CTE or the outer "
            "SELECT MUST appear in the upstream CTE's own SELECT list. After writing each CTE, "
            "verify its SELECT list contains all columns that will be used downstream before "
            "writing the next CTE. A column used in GROUP BY but not in SELECT is invisible to "
            "callers — add it to SELECT explicitly if downstream steps need it."
        )
