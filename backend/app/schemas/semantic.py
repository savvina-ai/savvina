# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""API-only Pydantic schemas for the semantic model router.

All "shape of the model" classes live in :mod:`app.semantic.models` and are
Pydantic-first. This module intentionally keeps only:

- update / patch payload shapes (subsets of the canonical model with optional
  fields), and
- API-only response envelopes (``DriftReport``, ``GenerateInitResponse``, …)
  that don't correspond to a persisted model section.

The old "mirror" classes (``SemanticModelResponse``, ``TableSemanticSchema``,
``BusinessMetricSchema``, etc.) are gone — routes now declare
``response_model=SemanticModel`` directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..semantic.drift import DriftApplySummary
from ..semantic.models import (
    GENERATION_BATCH_SIZE,
    AggregationType,
    BusinessMetric,
    CardinalityClass,
    CommonJoin,
    DerivedColumn,
    FormatHint,
    RelationshipEdge,
    Segment,
    SemanticModel,
    SemanticType,
    TimeGranularity,
    ValueMapping,
)

# ── Drift / generation-init responses ─────────────────────────────────────────


class DriftReport(BaseModel):
    """Returned by ``GET /connections/{id}/semantic/drift``."""

    connection_id: str
    warnings: list[str]
    warning_count: int
    checked_at: str
    # "live": compared against a fresh introspection, which also refreshed the schema
    # cache. "cache": the data source could not be reached, so the comparison used the
    # schema cached at ``schema_cached_at``; ``refresh_error`` says why.
    schema_source: Literal["live", "cache"] = "live"
    schema_cached_at: str | None = None
    refresh_error: str | None = None
    # True when a table was added or removed, or a column added, removed or renamed —
    # what ``POST .../semantic/apply-drift`` acts on.
    # An unreadable table or a metric naming a missing table is reported in ``warnings``
    # but is not actionable: apply-drift returns the model unchanged for those alone.
    has_actionable_changes: bool = False


class LastDriftResponse(BaseModel):
    """Returned by ``GET /connections/{id}/semantic/drift/last``.

    ``report`` is the background monitor's latest report when it still describes the
    stored model: ``None`` when no monitor report exists, when a later Check Drift
    superseded it (the page has that one), or when the model changed since (an apply, a
    regenerate, a save). ``monitor_interval_minutes`` is ``DRIFT_CHECK_INTERVAL_MINUTES``;
    the page polls only while it is positive.
    """

    report: DriftReport | None = None
    monitor_interval_minutes: int = 0


class ApplyDriftResponse(BaseModel):
    """Returned by ``POST /connections/{id}/semantic/apply-drift``.

    ``semantic_model`` is the model as stored; ``summary`` names what this apply
    added, removed and renamed (on a ``dry_run``, what it would). The page shows it until
    dismissed; a real apply's summary is also logged and served by
    ``GET .../semantic/drift/history``.
    """

    semantic_model: SemanticModelResponse
    summary: DriftApplySummary


class DriftApplyHistoryEntry(BaseModel):
    """One logged Apply Drift, from ``GET .../semantic/drift/history``."""

    applied_at: str
    applied_by: str | None
    summary: DriftApplySummary


class GenerateInitResponse(BaseModel):
    """Returned by ``POST /connections/{id}/semantic/generate/init``.

    ``pending_table_keys`` is the work list: a fresh run lists every table, a
    ``resume=true`` run lists only the ones the interrupted run had not reached.
    The frontend slices it into batches itself — nothing positional survives on
    the server.
    """

    connection_id: str
    tables_total: int
    batch_count: int
    batch_size: int
    pending_table_keys: list[str] = []
    resumed: bool = False


class GenerateBatchRequest(BaseModel):
    """Body of ``POST /connections/{id}/semantic/generate/batch`` — one batch of
    tables to annotate, named by key rather than by position."""

    table_keys: list[str] = Field(min_length=1, max_length=GENERATION_BATCH_SIZE)


class GenerateBatchResponse(BaseModel):
    """Returned by ``POST /connections/{id}/semantic/generate/batch``.

    Progress only, never the model: the client drives its own progress bar from
    the keys it dispatched and reads the finished model from ``/generate/globals``.
    ``tables_done`` counts the tables this run has reconciled so far.
    """

    connection_id: str
    table_keys: list[str]
    tables_done: int
    tables_total: int
    batch_size: int
    # Keys of this batch the LLM returned no annotation for. They are not stamped,
    # so they stay pending; the client re-queues them once, and ``/generate/globals``
    # warns about any table still without one.
    omitted_table_keys: list[str] = []


# ── Semantic suggestion (DB row → API) ────────────────────────────────────────


class SemanticSuggestionResponse(BaseModel):
    """Wire shape for a row from ``semantic_suggestions``."""

    id: str
    connection_id: str
    table_key: str
    field: str
    correction_type: str
    value: dict
    is_applied: bool
    source_message_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── PUT /connections/{id}/semantic — update payloads ──────────────────────────


class ValueMappingUpdate(BaseModel):
    """Same shape as :class:`ValueMapping` — kept in this module because the
    router accepts it as input via :class:`ColumnSemanticUpdate`."""

    raw_value: str
    display_value: str
    description: str | None = None


class ColumnSemanticUpdate(BaseModel):
    """Partial column update.

    Every field is optional so the client can patch a single attribute (e.g.
    just ``description``) without resending the full column shape. The router
    deep-merges this into the stored column dict using
    ``model_dump(exclude_unset=True)``.
    """

    display_name: str | None = None
    description: str | None = None
    value_mappings: list[ValueMapping] | None = None
    is_sensitive: bool | None = None
    is_non_additive: bool | None = None
    semantic_type: SemanticType | None = None
    cardinality: CardinalityClass | None = None
    currency: str | None = None
    unit: str | None = None
    default_aggregation: AggregationType | None = None
    time_granularity: TimeGranularity | None = None


class TableSemanticUpdate(BaseModel):
    """Partial table update — keyed by ``schema.table`` in
    :class:`SemanticModelUpdate.tables`."""

    display_name: str | None = None
    description: str | None = None
    default_filters: list[str] | None = None
    primary_timestamp_column: str | None = None
    primary_date_column: str | None = None
    grain: str | None = None
    domain: str | None = None
    # Keyed by column name; only supplied columns are deep-merged.
    columns: dict[str, ColumnSemanticUpdate] | None = None


class SemanticModelUpdate(BaseModel):
    """Partial update payload for ``PUT /connections/{id}/semantic``.

    Every field is optional. Sections not present are left untouched. ``tables``
    is deep-merged per-column; the rest are wholesale replacements.
    """

    tables: dict[str, TableSemanticUpdate] | None = None
    business_metrics: list[BusinessMetric] | None = None
    common_joins: list[CommonJoin] | None = None
    relationships: list[RelationshipEdge] | None = None
    derived_columns: list[DerivedColumn] | None = None
    segments: list[Segment] | None = None
    notes: list[str] | None = None
    is_user_reviewed: bool | None = None
    # The ``version`` the draft was loaded from (``SemanticModelResponse.version``). When
    # given, the PUT is refused (409) if the stored model has moved since; when omitted the
    # PUT is unconditional (API clients, "save anyway"). Read by the route, never a section:
    # ``apply_user_patch`` excludes it.
    base_version: int | None = None


TombstoneKind = Literal["metric", "segment", "derived_column", "common_join", "relationship"]


class TombstoneEntry(BaseModel):
    """One deleted item (``GET /connections/{id}/semantic/tombstones``): its kind, its key
    (the metric, segment or derived-column name, the join pattern, or the relationship's
    ``from_table.from_column->to_table.to_column``) and when it was deleted."""

    kind: TombstoneKind
    key: str
    created_at: datetime


class TombstoneRestore(BaseModel):
    """Body of ``POST /connections/{id}/semantic/tombstones/restore``: the tombstone to lift.
    A body, not a path segment: ``key`` is free text (a join pattern, a dotted key)."""

    kind: TombstoneKind
    key: str = Field(min_length=1)


class SemanticModelResponse(SemanticModel):
    """The stored model as every model-serving route returns it, plus the header's
    content ``version`` (bumped by every content write). The page sends it back as
    ``SemanticModelUpdate.base_version`` on its next PUT. Not a ``SemanticModel`` field:
    ``store.save_model`` fits the model's dump onto ``SemanticModelRow``, which has a
    ``version`` column of its own."""

    version: int


# ── POST /connections/{id}/semantic/business-metrics — create payload ─────────


class BusinessMetricCreate(BaseModel):
    """Convenience body for adding a single ``SimpleMetric`` via REST.

    The full discriminated :class:`BusinessMetric` union is also accepted via
    :class:`SemanticModelUpdate.business_metrics`; this shape exists for the
    common UI flow that adds one simple metric at a time.
    """

    name: str
    definition: str
    description: str
    aggregation: AggregationType
    filters: list[str] = []
    related_tables: list[str] = []
    format_hint: FormatHint | None = None


# ``ApplyDriftResponse`` names ``SemanticModelResponse`` before it is defined.
ApplyDriftResponse.model_rebuild()
