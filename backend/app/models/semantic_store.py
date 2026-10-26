# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Normalised persistence tables for the semantic layer.

The Pydantic classes in ``app/semantic/models.py`` remain the in-memory and
prompt-facing shape; these rows are the persistence shape, assembled into a
``SemanticModel`` by ``app/semantic/store.py``. Nothing here runs a query —
the repository owns all of that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base

# PostgreSQL gets real JSONB; SQLite, which the store tests run on, gets plain
# JSON. No query in this package uses a JSONB path operator — the lookups are on
# real columns.
_JSON = JSON().with_variant(JSONB, "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class _Provenanced:
    """Columns every semantic row carries. Mixed in, never instantiated.

    ``user_edited_fields`` is per-field provenance and the source of truth: a
    row-level bool cannot express "the user wrote ``description``, so a
    regenerate may still refresh ``grain``". ``is_user_edited`` is derived.

    ``version`` is the optimistic lock: a single-row edit issues
    ``UPDATE … WHERE id = :id AND version = :version`` and treats a zero
    rowcount as a conflict.
    """

    user_edited_fields: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    @property
    def is_user_edited(self) -> bool:
        return bool(self.user_edited_fields)


class SemanticModelRow(_Provenanced, Base):
    """One header row per connection; every other semantic row hangs off it."""

    __tablename__ = "semantic_models"
    __table_args__ = (Index("uq_semantic_models_connection", "connection_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_dialect: Mapped[str] = mapped_column(String(50), nullable=False, default="sql")
    db_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    generation_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    generation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    is_user_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generation_progress: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    generation_warnings: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    notes: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    time_expressions: Mapped[dict[str, str]] = mapped_column(_JSON, nullable=False, default=dict)
    # The run in flight while ``generation_status`` is ``tables_partial``: minted
    # by ``store.begin_generation`` on a fresh run, kept on a resume, cleared by
    # the full save that ends the run. Compared against
    # ``SemanticTableRow.generation_run_id`` to decide which tables this run has
    # (re)annotated.
    generation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SemanticKnownColumnsRow(Base):
    """The live column names the model has seen for one table (``store.update_known_columns``).

    Not ``_Provenanced`` and not part of ``SemanticModel``: bookkeeping that never bumps
    a version, never writes history and is never rebuilt by ``save_model`` (which
    rebuilds table and column rows). A table with no row has no baseline yet. Rows of a
    table the model no longer holds are removed by ``update_known_columns(prune=True)``.
    """

    __tablename__ = "semantic_known_columns"
    __table_args__ = (
        PrimaryKeyConstraint("connection_id", "table_key"),
        Index("ix_semantic_known_columns_model", "model_id"),
    )

    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_models.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    table_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Sorted column names, replaced whole per table (bounded by the table's width).
    columns: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)


class SemanticTableRow(_Provenanced, Base):
    """One row per table key. ``(connection_id, table_key)`` is the lookup key."""

    __tablename__ = "semantic_tables"
    __table_args__ = (
        Index("uq_semantic_tables_key", "connection_id", "table_key", unique=True),
        Index("ix_semantic_tables_model", "model_id"),
        Index("ix_semantic_tables_run", "connection_id", "generation_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_models.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from the header row so reads can filter without a join.
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    table_key: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    grain: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_timestamp_column: Mapped[str | None] = mapped_column(String(512), nullable=True)
    primary_date_column: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_filters: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    hierarchies: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, nullable=False, default=list)
    partition_columns: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    cluster_columns: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    # The run that last reconciled this row. Not a content field: stamping it
    # bumps neither ``version`` nor ``updated_at``.
    generation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class SemanticColumnRow(_Provenanced, Base):
    """One row per column of a semantic table."""

    __tablename__ = "semantic_columns"
    __table_args__ = (
        Index("uq_semantic_columns_name", "table_id", "column_name", unique=True),
        # No separate table_id index: uq_semantic_columns_name(table_id, column_name) leads with it.
        Index(
            "ix_semantic_columns_sensitive",
            "table_id",
            postgresql_where=text("is_sensitive"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    table_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_tables.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cardinality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_aggregation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_non_additive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    time_granularity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_mappings: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSON, nullable=False, default=list
    )


class SemanticMetricRow(_Provenanced, Base):
    """One row per BusinessMetric. The variant fields live in ``payload``."""

    __tablename__ = "semantic_metrics"
    __table_args__ = (
        Index("uq_semantic_metrics_name", "connection_id", "name", unique=True),
        Index("ix_semantic_metrics_model", "model_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_models.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    # Position in ``SemanticModel.business_metrics`` when written. ``id`` is a
    # random UUID, and order is content (the formatter renders as stored), so
    # every read must ``ORDER BY ordinal``.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SemanticRelationshipRow(_Provenanced, Base):
    """One row per relationship edge; read by model only."""

    __tablename__ = "semantic_relationships"
    __table_args__ = (Index("ix_semantic_rel_model", "model_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_models.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    from_table: Mapped[str] = mapped_column(String(512), nullable=False)
    from_column: Mapped[str] = mapped_column(String(512), nullable=False)
    to_table: Mapped[str] = mapped_column(String(512), nullable=False)
    to_column: Mapped[str] = mapped_column(String(512), nullable=False)
    join_sql: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relationship_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="many_to_one"
    )
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SemanticGlobalItemRow(_Provenanced, Base):
    """A segment, derived column or common join — one table, ``kind`` differs.

    ``name`` is the join pattern for a common join, which has no name of its
    own, hence ``Text`` rather than a capped VARCHAR: truncating a SQL fragment
    would corrupt what gets spliced into the prompt.
    """

    __tablename__ = "semantic_global_items"
    __table_args__ = (
        Index("uq_semantic_global_kind_name", "connection_id", "kind", "name", unique=True),
        Index("ix_semantic_global_model", "model_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_models.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 'segment' | 'derived_column' | 'common_join'
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    # Position within this row's own ``kind``.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SemanticHistoryRow(Base):
    """Append-only audit trail, written by the store on every change.

    Application-level rather than a DB trigger, so there is no invisible writer.

    ``connection_id`` deliberately has no foreign key, so the audit trail
    survives deletion of the connection and its semantic rows.
    """

    __tablename__ = "semantic_model_history"
    __table_args__ = (
        Index("ix_semantic_history_connection", "connection_id", "created_at"),
        Index("ix_semantic_history_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 'model' | 'table' | 'column' | 'metric' | 'relationship' | 'global_item' | 'drift_apply'
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # 'insert' | 'update' | 'delete'
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'user' | 'generation' | 'migration' | 'drift'
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    old_value: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    changed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
