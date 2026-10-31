# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The chat warning for a prompt the compression stripped of semantic context.

- ``_compress_prompt`` returns, with the prompt, what it kept of the semantic model
  (``"full"``, ``"compact"``, ``"dropped"``).
- ``_semantic_context_warning`` turns that into the warning: dropped when the prompt
  would have rendered the model at all, compacted when the compact copy had tables,
  relationships or derived columns to strip.
- ``_generate_query`` sets ``semantic_warning`` from the last compression, whichever
  budget (context window or TPM) ran it, and ``user_warning`` joins it with the TPM
  note for ``DoneEvent.warning``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from app.datasources.models import DataSourceSchema
from app.semantic.models import (
    ColumnSemantic,
    DerivedColumn,
    RelationshipEdge,
    SemanticModel,
    SimpleMetric,
    TableSemantic,
)
from app.services.pipeline import (
    _SEMANTIC_COMPACTED_WARNING,
    _SEMANTIC_DROPPED_WARNING,
    _compact_semantic_model,
    _compress_prompt,
    _generate_query,
    _GenerationResult,
    _semantic_context_warning,
)
from app.services.prompt_builder import PromptBuilder

from .test_tpm_warning import (
    _PIPELINE,
    _base_patches,
    _make_adapter,
    _make_db,
    _make_llm_response,
    _make_provider,
    _make_settings,
)


def _model_with_table() -> SemanticModel:
    return SemanticModel(
        tables={
            "public.orders": TableSemantic(
                display_name="Orders", columns={"id": ColumnSemantic(display_name="ID")}
            )
        },
        business_metrics=[
            SimpleMetric(name="Revenue", definition="orders.total", aggregation="sum")
        ],
    )


def _edge() -> RelationshipEdge:
    return RelationshipEdge(
        from_table="public.orders",
        from_column="customer_id",
        to_table="public.customers",
        to_column="id",
        join_sql="public.orders.customer_id = public.customers.id",
    )


class TestSemanticContextWarning:
    def test_none_without_a_model(self) -> None:
        assert _semantic_context_warning("dropped", None) is None

    def test_none_for_an_empty_model(self) -> None:
        assert _semantic_context_warning("dropped", SemanticModel()) is None

    def test_none_when_the_model_was_kept(self) -> None:
        assert _semantic_context_warning("full", _model_with_table()) is None

    def test_dropped(self) -> None:
        assert _semantic_context_warning("dropped", _model_with_table()) == (
            _SEMANTIC_DROPPED_WARNING
        )

    def test_compacted_when_the_tables_are_gone(self) -> None:
        assert _semantic_context_warning("compact", _model_with_table()) == (
            _SEMANTIC_COMPACTED_WARNING
        )

    def test_compacted_when_relationships_or_derived_columns_are_gone(self) -> None:
        """A model whose tables were filtered away before the prompt still loses its
        join guidance and derived expressions to the compact copy."""
        metrics = _model_with_table().business_metrics
        with_edge = SemanticModel(business_metrics=metrics, relationships=[_edge()])
        assert _semantic_context_warning("compact", with_edge) == _SEMANTIC_COMPACTED_WARNING
        derived = DerivedColumn(name="Margin", sql_expression="price - cost")
        with_derived = SemanticModel(business_metrics=metrics, derived_columns=[derived])
        assert _semantic_context_warning("compact", with_derived) == _SEMANTIC_COMPACTED_WARNING

    def test_compacting_a_model_with_nothing_to_strip_is_silent(self) -> None:
        metrics_only = SemanticModel(business_metrics=_model_with_table().business_metrics)
        assert _semantic_context_warning("compact", metrics_only) is None

    def test_a_model_the_prompt_never_renders_loses_nothing(self) -> None:
        """Relationships alone make no Business Context section (``has_business_context``),
        so there was nothing in the prompt to drop."""
        assert _semantic_context_warning("dropped", SemanticModel(relationships=[_edge()])) is None


class TestUserWarning:
    @staticmethod
    def _result(**kw) -> _GenerationResult:
        base = dict(
            generated_query="SELECT 1",
            explanation="",
            error=None,
            status="executed",
            cache_hit=False,
            provider=None,
            configured_model="",
            configured_max_tokens=0,
            system_prompt="",
            history=[],
        )
        return _GenerationResult(**{**base, **kw})

    def test_none_when_nothing_to_say(self) -> None:
        assert self._result().user_warning is None

    def test_each_alone(self) -> None:
        assert self._result(tpm_warning="tpm").user_warning == "tpm"
        assert self._result(semantic_warning="sem").user_warning == "sem"

    def test_both_joined_tpm_first(self) -> None:
        assert self._result(tpm_warning="tpm", semantic_warning="sem").user_warning == "tpm sem"


async def _call_generate(provider: MagicMock, db: MagicMock, semantic_model) -> _GenerationResult:
    cache = MagicMock()
    cache.lookup = AsyncMock(return_value=None)
    cache.compute_embedding_async = AsyncMock(return_value=[0.1])
    examples = MagicMock()
    examples.find_similar_examples = AsyncMock(return_value=[])
    with contextlib.ExitStack() as stack:
        for p in _base_patches(provider, db):
            stack.enter_context(p)
        return await _generate_query(
            cache=cache,
            examples=examples,
            connection_id="conn-1",
            message="how many rows?",
            session_id=None,
            adapter=_make_adapter(),
            schema=MagicMock(),
            privacy=None,
            semantic_model=semantic_model,
            provider_name="openai",
            options={},
            settings=_make_settings(),
            bypass_cache=True,
        )


class TestPipelineSetsSemanticWarning:
    """``_make_provider`` gives the provider no context window, so nothing is compressed
    unless a test sets one; the compression itself is patched to report a fit."""

    @staticmethod
    def _compressing(fit: str):
        provider = _make_provider(side_effects=[_make_llm_response()])
        provider.context_window = 1  # any prompt is over budget
        compress = patch(f"{_PIPELINE}._compress_prompt", return_value=("sys-prompt", fit))
        return provider, compress

    async def test_set_from_what_the_compression_kept(self) -> None:
        for fit, expected in (
            ("compact", _SEMANTIC_COMPACTED_WARNING),
            ("dropped", _SEMANTIC_DROPPED_WARNING),
        ):
            provider, compress = self._compressing(fit)
            with compress as mock_compress:
                result = await _call_generate(provider, _make_db(), _model_with_table())
            mock_compress.assert_called_once()
            assert result.semantic_warning == expected
            assert result.user_warning == expected
            assert result.error is None

    async def test_none_when_nothing_was_compressed(self) -> None:
        provider = _make_provider(side_effects=[_make_llm_response()])
        result = await _call_generate(provider, _make_db(), _model_with_table())
        assert result.semantic_warning is None
        assert result.user_warning is None

    async def test_none_without_a_model(self) -> None:
        provider, compress = self._compressing("dropped")
        with compress:
            result = await _call_generate(provider, _make_db(), None)
        assert result.semantic_warning is None
        assert result.user_warning is None

    async def test_none_when_generation_failed(self) -> None:
        provider, compress = self._compressing("dropped")
        provider.generate_response = AsyncMock(side_effect=RuntimeError("boom"))
        with compress:
            result = await _call_generate(provider, _make_db(), _model_with_table())
        assert result.error is not None
        assert result.semantic_warning is None


class TestCompressPromptReportsItsFit:
    """Run the real ``PromptBuilder`` through ``_compress_prompt`` at each budget and
    check the fit it reports with the prompt."""

    _MESSAGE = "how many orders?"
    _SCHEMA = DataSourceSchema(source_type="postgresql", schemas=["public"], tables=[])

    @staticmethod
    def _datasource() -> MagicMock:
        ds = MagicMock()
        ds.display_name = "PostgreSQL"
        ds.query_dialect = "postgresql"
        ds.get_system_prompt_additions.return_value = "dialect notes"
        ds.format_schema_for_llm.return_value = "CREATE TABLE public.orders (id int);\n" * 20
        return ds

    def _compress(self, ds: MagicMock, model: SemanticModel | None, budget: int):
        return _compress_prompt(
            builder=PromptBuilder(),
            datasource=ds,
            schema=self._SCHEMA,
            privacy=None,
            semantic_model=model,
            few_shot_examples=None,
            user_question=self._MESSAGE,
            user_message=self._MESSAGE,
            budget_chars=budget,
        )

    def test_the_fit_follows_the_compression_level(self) -> None:
        ds = self._datasource()
        model = _model_with_table()

        def built(semantic: SemanticModel | None) -> str:
            return PromptBuilder().build_system_prompt(
                datasource=ds,
                schema=self._SCHEMA,
                privacy=None,
                semantic_model=semantic,
                few_shot_examples=None,
                user_question=self._MESSAGE,
            )

        full, compact, dropped = built(model), built(_compact_semantic_model(model)), built(None)
        assert len(dropped) < len(compact) < len(full)
        extra = len(self._MESSAGE)

        # Level 2 (the example list is empty) fits the whole budget: the prompt is complete.
        assert self._compress(ds, model, len(full) + extra) == (full, "full")
        # Too small for the full model, big enough for the compacted one (level 3).
        assert self._compress(ds, model, len(compact) + extra) == (compact, "compact")
        # Too small for the compacted model (level 4): the model is gone.
        assert self._compress(ds, model, len(dropped) + extra) == (dropped, "dropped")
        # Nothing fits (level 5, tiered schema): the model is still gone.
        prompt, fit = self._compress(ds, model, 0)
        assert fit == "dropped" and "## Business Context" not in prompt

    def test_without_a_model_a_fitting_prompt_is_full(self) -> None:
        assert self._compress(self._datasource(), None, 10**6)[1] == "full"

    def test_a_compact_copy_that_renders_nothing_counts_as_dropped(self) -> None:
        """Notes alone make no Business Context section, so a model with tables and notes
        but no metrics, segments or time expressions is gone at level 3 as at level 4."""
        ds = self._datasource()
        model = SemanticModel(
            tables=_model_with_table().tables, notes=["Amounts are stored in cents"]
        )

        def built(semantic: SemanticModel | None) -> str:
            return PromptBuilder().build_system_prompt(
                datasource=ds,
                schema=self._SCHEMA,
                privacy=None,
                semantic_model=semantic,
                few_shot_examples=None,
                user_question=self._MESSAGE,
            )

        compact = built(_compact_semantic_model(model))
        prompt, fit = self._compress(ds, model, len(compact) + len(self._MESSAGE))
        assert fit == "dropped"
        assert "## Business Context" not in prompt
        assert _semantic_context_warning(fit, model) == _SEMANTIC_DROPPED_WARNING
