# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Repository for the normalised semantic layer.

The only module that knows both the ORM rows in ``app/models/semantic_store.py``
and the Pydantic shape in ``app/semantic/models.py``. Routers and services call
this; they never hand-merge semantic dicts and never touch
``Connection.semantic_model`` — the legacy blob is unread everywhere and is
cleared only here, by ``delete_model``.

Nothing here commits. The caller owns the transaction, so a write and the cache
invalidation beside it share one ``await db.commit()``.

Order is content for the list sections: metrics, relationships, segments,
derived columns and common joins carry an ``ordinal`` and are read back in that
order. Tables and columns are read in key order (alphabetical), which differs
from the blob era's generator order.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from typing import TYPE_CHECKING, Any, Literal
import uuid

from pydantic import TypeAdapter
from sqlalchemy import String, delete, func, null, select, update

from ..models.connection import Connection
from ..models.semantic_store import (
    SemanticColumnRow,
    SemanticGlobalItemRow,
    SemanticHistoryRow,
    SemanticMetricRow,
    SemanticModelRow,
    SemanticRelationshipRow,
    SemanticTableRow,
)
from .models import (
    BusinessMetric,
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    GenerationProgress,
    GenerationStatus,
    RelationshipEdge,
    Segment,
    SemanticModel,
    TableSemantic,
)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..schemas.semantic import SemanticModelUpdate

logger = logging.getLogger(__name__)

# Scalar/JSON fields carried straight across between the header row and SemanticModel.
_MODEL_FIELDS: tuple[str, ...] = (
    "schema_hash",
    "source_dialect",
    "db_timezone",
    "generated_at",
    "generation_model",
    "is_user_reviewed",
    "generation_warnings",
    "notes",
    "time_expressions",
)
_TABLE_FIELDS: tuple[str, ...] = (
    "display_name",
    "description",
    "grain",
    "primary_timestamp_column",
    "primary_date_column",
    "base_sql",
    "domain",
    "default_filters",
    "hierarchies",
    "partition_columns",
    "cluster_columns",
)
_COLUMN_FIELDS: tuple[str, ...] = (
    "display_name",
    "description",
    "is_sensitive",
    "semantic_type",
    "cardinality",
    "currency",
    "unit",
    "default_aggregation",
    "is_non_additive",
    "time_granularity",
    "value_mappings",
)
_CHUNK = 500

# Provenance keyed by entity key: {"model": {"": [fields]}, "table": {table_key:
# [fields]}, "column": {"table_key.column": [fields]}, "metric": {name: [...]},
# "global_item": {"kind:name": [...]}, "relationship": {"a.x->b.y": [...]}}.
# What a full save has to carry across so a user's edits survive a globals
# finalisation that rebuilds every row.
# Column keys are ``"table_key.column_name"``: only ever looked up, never parsed,
# but a quoted identifier containing a dot can in principle make two columns
# share a key (the same encoding as prod).
ProvenanceMap = dict[str, dict[str, list[str]]]
_HEADER_KEY = ""
# Review state rather than content: ``/generate/globals`` resets it on purpose.
_HEADER_UNPROTECTED = frozenset({"is_user_reviewed"})


def _now() -> datetime:
    return datetime.now(UTC)


def _chunks(items: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


# Migration 0003 deliberately keeps the *first* duplicate (legacy blob order);
# runtime saves keep the *last* (the caller's latest intent).
def _dedupe_last_wins(items: Sequence[Any], key: Callable[[Any], Any]) -> list[Any]:
    """Collapse items sharing a key to the last one, keeping the first position.

    The metric and global-item tables are unique on name; a legacy blob or a
    hand-edited draft can carry two entries with the same name, and the second
    insert would raise ``IntegrityError`` (not a ``ValueError``, so the routers'
    ``except ValueError -> 400`` would not catch it).
    """
    by_key: dict[Any, Any] = {}
    for item in items:
        by_key[key(item)] = item
    return list(by_key.values())


# Columns that name an entity. Truncating one would change what it means, so an
# over-long value is rejected rather than cut.
_IDENTITY_COLUMNS = frozenset(
    {
        "table_key",
        "column_name",
        "name",
        "from_table",
        "from_column",
        "to_table",
        "to_column",
    }
)

# ``semantic_global_items.name`` is Text (a join pattern is SQL and must never be
# truncated) but sits in a unique btree index, and PostgreSQL refuses an index
# entry over about a third of a page (2704 bytes, tuple header and the other
# indexed columns included). A Text identity column is therefore bounded by bytes,
# with room to spare for ``connection_id`` and ``kind``.
_MAX_INDEXED_TEXT_BYTES = 2000

# The kinds of named item ``name_rejection`` checks.
NameKind = Literal["metric", "segment", "derived_column", "common_join"]

# The row class whose ``name`` column a generated item of each kind is stored in.
_NAME_ROW_BY_KIND: dict[NameKind, type] = {
    "metric": SemanticMetricRow,
    "segment": SemanticGlobalItemRow,
    "derived_column": SemanticGlobalItemRow,
    "common_join": SemanticGlobalItemRow,
}


def _strip_nul(value: Any) -> Any:
    """Remove NUL characters from a string, or from every string (and object key)
    inside a JSON-shaped value. PostgreSQL rejects them in TEXT and JSONB."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


def _fit_values(row_cls: type, values: dict[str, Any]) -> dict[str, Any]:
    """Make ``values`` acceptable to PostgreSQL for ``row_cls``'s columns.

    The old JSON blob took any string; real PostgreSQL rejects a NUL character in
    TEXT/JSONB and a value longer than a bounded VARCHAR, either of which would
    fail a whole save. NULs are stripped (recursively inside JSON values) and a
    bounded string is truncated to its column length. Identity columns
    (``_IDENTITY_COLUMNS``) are never truncated: one that does not fit raises
    ``ValueError``, which the routes map to HTTP 400. An identity column that is
    unbounded Text is bounded by the unique index it sits in instead
    (``_MAX_INDEXED_TEXT_BYTES``, measured in UTF-8 bytes, which is what the
    index measures). Keys that are not columns of ``row_cls`` pass through
    unchanged. Migration 0003 does the same for the backfill (``_clean_row`` /
    ``_fit_identity``).
    """
    columns = row_cls.__table__.c  # type: ignore[attr-defined]
    out: dict[str, Any] = {}
    for key, value in values.items():
        column = columns.get(key)
        if column is None:
            out[key] = value
            continue
        value = _strip_nul(value)
        if isinstance(value, str) and isinstance(column.type, String):
            length = column.type.length
            if length is None:
                if key in _IDENTITY_COLUMNS:
                    size = len(value.encode("utf-8"))
                    if size > _MAX_INDEXED_TEXT_BYTES:
                        raise ValueError(
                            f"{key} {value[:60]!r}... is {size} bytes; the limit is "
                            f"{_MAX_INDEXED_TEXT_BYTES} bytes"
                        )
            elif len(value) > length:
                if key in _IDENTITY_COLUMNS:
                    raise ValueError(
                        f"{key} {value[:60]!r}... is {len(value)} characters; the limit is {length}"
                    )
                logger.warning(
                    "semantic store: truncated %s.%s from %d to %d characters",
                    row_cls.__tablename__,  # type: ignore[attr-defined]
                    key,
                    len(value),
                    length,
                )
                value = value[:length]
        out[key] = value
    return out


def name_rejection(kind: NameKind, name: str) -> str | None:
    """Why ``save_model`` would reject an item of ``kind`` named ``name``, or ``None``.

    ``kind`` is ``"metric"``, ``"segment"``, ``"derived_column"`` or
    ``"common_join"`` (whose name is its ``join_pattern``). The same rule
    ``_fit_values`` applies at the save — NULs stripped first, then the column's
    length or, for an indexed Text column, the index's byte limit — so a caller
    can drop one bad generated item instead of failing the whole save.
    """
    try:
        _fit_values(_NAME_ROW_BY_KIND[kind], {"name": name})
    except ValueError as exc:
        return str(exc)
    return None


def _table_row_values(table_dump: dict[str, Any]) -> dict[str, Any]:
    return {field: table_dump.get(field) for field in _TABLE_FIELDS}


def _column_row_values(col_dump: dict[str, Any]) -> dict[str, Any]:
    return {field: col_dump.get(field) for field in _COLUMN_FIELDS}


def _global_item_name(kind: str, payload: dict[str, Any]) -> str:
    """A common join has no name; its ``join_pattern`` identifies it."""
    if kind == "common_join":
        return str(payload.get("join_pattern", ""))
    return str(payload.get("name", ""))


def _relationship_identity(
    edge: Mapping[str, Any] | RelationshipEdge,
) -> tuple[str, str, str, str]:
    if isinstance(edge, Mapping):
        return (edge["from_table"], edge["from_column"], edge["to_table"], edge["to_column"])
    return (edge.from_table, edge.from_column, edge.to_table, edge.to_column)


def relationship_key(edge: Mapping[str, Any] | RelationshipEdge) -> str:
    """The provenance-map key for a relationship: its four endpoints, joined."""
    from_table, from_column, to_table, to_column = _relationship_identity(edge)
    return f"{from_table}.{from_column}->{to_table}.{to_column}"


def _relationship_row_to_dict(row: SemanticRelationshipRow) -> dict[str, Any]:
    return {
        "from_table": row.from_table,
        "from_column": row.from_column,
        "to_table": row.to_table,
        "to_column": row.to_column,
        "join_sql": row.join_sql,
        "relationship_type": row.relationship_type,
        "is_required": row.is_required,
        "description": row.description,
        "entity_type": row.entity_type,
    }


def _relationship_patch_to_dict(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise an incoming relationship with the insert's defaults so an echo
    compares equal to ``_relationship_row_to_dict``."""
    return {
        "from_table": payload.get("from_table"),
        "from_column": payload.get("from_column"),
        "to_table": payload.get("to_table"),
        "to_column": payload.get("to_column"),
        "join_sql": payload.get("join_sql", ""),
        "relationship_type": payload.get("relationship_type", "many_to_one"),
        "is_required": payload.get("is_required", False),
        "description": payload.get("description"),
        "entity_type": payload.get("entity_type"),
    }


def _canonical_payload_list(items: Iterable[Mapping[str, Any]]) -> list[str]:
    # Not sorted: the formatter renders as stored, so a reorder is a real edit.
    return [json.dumps(item, sort_keys=True, default=str) for item in items]


def _payload_lists_equal(
    old: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]
) -> bool:
    return _canonical_payload_list(old) == _canonical_payload_list(new)


def _canonical_payload(item: Mapping[str, Any]) -> str:
    return _canonical_payload_list([item])[0]


def _stored_marks(
    rows: Iterable[tuple[Any, Mapping[str, Any], list[str]]],
) -> dict[Any, tuple[str, list[str]]]:
    return {
        identity: (_canonical_payload(payload), list(marks)) for identity, payload, marks in rows
    }


def _item_marks(
    stored: Mapping[Any, tuple[str, list[str]]], identity: Any, payload: Mapping[str, Any]
) -> list[str]:
    """Provenance for a re-inserted section item: an echo keeps its marker, a new
    or changed item is marked. Marking every re-inserted row would freeze the
    whole section against regeneration on the first edit to any one item."""
    prior = stored.get(identity)
    if prior is None or prior[0] != _canonical_payload(payload):
        return ["payload"]
    return prior[1]


def _write_history(
    db: AsyncSession,
    *,
    connection_id: str,
    entity_type: str,
    entity_id: str,
    entity_key: str,
    action: str,
    source: str,
    old_value: dict[str, Any] | None,
    new_value: dict[str, Any] | None,
    changed_by: str | None,
) -> None:
    """Append one history row. Synchronous — it only stages an ORM object.

    ``entity_key`` is fitted like any other value: a column key is
    ``"{table_key}.{column_name}"``, and two identity values that each fit their
    own column can together exceed the key's.
    The key is best-effort audit context (a truncated key can collide); ``entity_id``
    is the stable reference.
    """
    fitted = _fit_values(SemanticHistoryRow, {"entity_key": entity_key})
    db.add(
        SemanticHistoryRow(
            connection_id=connection_id,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_key=fitted["entity_key"],
            action=action,
            source=source,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
        )
    )


def _header_to_dict(header: SemanticModelRow) -> dict[str, Any]:
    data: dict[str, Any] = {field: getattr(header, field) for field in _MODEL_FIELDS}
    data["generation_status"] = header.generation_status
    data["generation_progress"] = header.generation_progress
    data["generation_run_id"] = header.generation_run_id
    return data


# ── Write ─────────────────────────────────────────────────────────────────────


async def save_model(
    db: AsyncSession,
    connection_id: str,
    model: SemanticModel,
    *,
    updated_by: str | None,
    source: str = "user",
    provenance: ProvenanceMap | None = None,
) -> None:
    """Replace every semantic row for ``connection_id`` with ``model``.

    A full replace, used where the caller holds the whole model (``/generate/
    globals``). The non-destructive regenerate path uses ``reconcile_tables``.

    ``provenance`` is a ``provenance_map`` snapshot taken *before* this call. For
    the header, tables and columns, every field it names takes the *stored*
    value over the incoming model's (``_protected_values``) and gets its
    ``user_edited_fields`` back. Metrics, relationships and global items get
    their marker back but their payload comes from ``model`` — the globals route
    swaps each marked item back in itself before calling this.

    Raises ``ValueError`` (before touching the session) if an identity value — a
    table key, column name, metric name or relationship endpoint — is longer than
    its column, or a segment, derived-column or join name is over the unique
    index's byte limit; every other string is made to fit (``_fit_values``).
    """
    prov = provenance or {}
    dumped = model.model_dump(mode="json")

    # Fit every value first: a ValueError must leave the session untouched.
    header_values = _fit_values(
        SemanticModelRow,
        {
            **{field: dumped.get(field) for field in _MODEL_FIELDS},
            "generation_status": str(model.generation_status),
            "generation_progress": dumped.get("generation_progress"),
        },
    )
    table_specs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for table_key, table in model.tables.items():
        table_values = _fit_values(
            SemanticTableRow,
            {"table_key": table_key, **_table_row_values(table.model_dump(mode="json"))},
        )
        column_values = [
            _fit_values(
                SemanticColumnRow,
                {
                    "column_name": column_name,
                    **_column_row_values(column.model_dump(mode="json")),
                },
            )
            for column_name, column in table.columns.items()
        ]
        table_specs.append((table_values, column_values))
    metric_values = _dedupe_last_wins(
        [
            _fit_values(
                SemanticMetricRow,
                {
                    "name": metric.name,
                    "metric_type": str(metric.metric_type),
                    "payload": metric.model_dump(mode="json"),
                },
            )
            for metric in model.business_metrics
        ],
        key=lambda values: values["name"],
    )
    rel_values = [
        _fit_values(
            SemanticRelationshipRow,
            {
                "from_table": edge.from_table,
                "from_column": edge.from_column,
                "to_table": edge.to_table,
                "to_column": edge.to_column,
                "join_sql": edge.join_sql,
                "relationship_type": str(edge.relationship_type),
                "is_required": edge.is_required,
                "description": edge.description,
                "entity_type": str(edge.entity_type) if edge.entity_type else None,
            },
        )
        for edge in model.relationships
    ]
    global_values: list[tuple[str, dict[str, Any]]] = []
    for kind, items in (
        ("segment", model.segments),
        ("derived_column", model.derived_columns),
        ("common_join", model.common_joins),
    ):
        fitted_items = [
            _fit_values(
                SemanticGlobalItemRow,
                {
                    "name": _global_item_name(kind, payload),
                    "payload": payload,
                },
            )
            for payload in (item.model_dump(mode="json") for item in items)
        ]
        for values in _dedupe_last_wins(fitted_items, key=lambda v: v["name"]):
            global_values.append((kind, values))

    # populate_existing: a re-read under a row lock must see other transactions'
    # committed state, not an identity-mapped instance.
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )

    if header is None:
        header = SemanticModelRow(connection_id=connection_id)
        db.add(header)
        await db.flush()  # header.id is a Python-side default applied at flush
        action = "insert"
        old_value: dict[str, Any] | None = None
        protected: dict[str, dict[str, dict[str, Any]]] = {}
    else:
        action = "update"
        old_value = _header_to_dict(header)
        protected = await _protected_values(db, header.id, provenance) if provenance else {}
        # Cascades clear tables → columns.
        await db.execute(delete(SemanticTableRow).where(SemanticTableRow.model_id == header.id))
        await db.execute(delete(SemanticMetricRow).where(SemanticMetricRow.model_id == header.id))
        await db.execute(
            delete(SemanticRelationshipRow).where(SemanticRelationshipRow.model_id == header.id)
        )
        await db.execute(
            delete(SemanticGlobalItemRow).where(SemanticGlobalItemRow.model_id == header.id)
        )

    header_edited = list(prov.get("model", {}).get(_HEADER_KEY, []))
    protected_header = (
        set(header_edited) - _HEADER_UNPROTECTED if action == "update" else set[str]()
    )
    for field, value in header_values.items():
        if field in protected_header:
            continue
        setattr(header, field, value)
    header.user_edited_fields = header_edited
    if header.generation_status != str(GenerationStatus.TABLES_PARTIAL):
        # The run is over; the rows rebuilt below carry no run id either.
        header.generation_run_id = None
    # Saving with status ``tables_partial`` keeps the header's run id but rebuilds
    # every table row without a stamp, which resets resume state: callers must
    # not full-save mid-run (use ``reconcile_tables``).
    header.updated_at = _now()
    header.updated_by = updated_by
    header.version = header.version + 1 if action == "update" else 1

    _write_history(
        db,
        connection_id=connection_id,
        entity_type="model",
        entity_id=header.id,
        entity_key="",
        action=action,
        source=source,
        old_value=old_value,
        new_value=_header_to_dict(header),
        changed_by=updated_by,
    )

    table_rows: list[SemanticTableRow] = []
    column_rows: list[SemanticColumnRow] = []
    for table_values, column_values in table_specs:
        table_key = table_values["table_key"]
        row = SemanticTableRow(
            # Explicit id: the column rows below need it before any flush.
            id=str(uuid.uuid4()),
            model_id=header.id,
            connection_id=connection_id,
            updated_at=_now(),
            updated_by=updated_by,
            **table_values,
        )
        for field, value in protected.get("table", {}).get(table_key, {}).items():
            setattr(row, field, value)
        row.user_edited_fields = prov.get("table", {}).get(table_key, [])
        table_rows.append(row)
        for col_values in column_values:
            col_row = SemanticColumnRow(
                table_id=row.id,
                updated_at=_now(),
                updated_by=updated_by,
                **col_values,
            )
            col_key = f"{table_key}.{col_values['column_name']}"
            for field, value in protected.get("column", {}).get(col_key, {}).items():
                setattr(col_row, field, value)
            col_row.user_edited_fields = prov.get("column", {}).get(col_key, [])
            column_rows.append(col_row)

    for chunk in _chunks(table_rows):
        db.add_all(chunk)
    await db.flush()
    for chunk in _chunks(column_rows):
        db.add_all(chunk)

    metric_rows = [
        SemanticMetricRow(
            model_id=header.id,
            connection_id=connection_id,
            ordinal=ordinal,
            updated_at=_now(),
            updated_by=updated_by,
            user_edited_fields=prov.get("metric", {}).get(values["name"], []),
            **values,
        )
        for ordinal, values in enumerate(metric_values)
    ]
    for chunk in _chunks(metric_rows):
        db.add_all(chunk)

    rel_rows = [
        SemanticRelationshipRow(
            model_id=header.id,
            connection_id=connection_id,
            ordinal=ordinal,
            updated_at=_now(),
            updated_by=updated_by,
            user_edited_fields=prov.get("relationship", {}).get(relationship_key(values), []),
            **values,
        )
        for ordinal, values in enumerate(rel_values)
    ]
    for chunk in _chunks(rel_rows):
        db.add_all(chunk)

    global_rows: list[SemanticGlobalItemRow] = []
    ordinals: dict[str, int] = {}
    for kind, values in global_values:
        ordinal = ordinals.get(kind, 0)
        ordinals[kind] = ordinal + 1
        global_rows.append(
            SemanticGlobalItemRow(
                model_id=header.id,
                connection_id=connection_id,
                kind=kind,
                ordinal=ordinal,
                updated_at=_now(),
                updated_by=updated_by,
                user_edited_fields=prov.get("global_item", {}).get(f"{kind}:{values['name']}", []),
                **values,
            )
        )
    for chunk in _chunks(global_rows):
        db.add_all(chunk)

    await db.flush()


async def delete_model(db: AsyncSession, connection_id: str, *, changed_by: str | None) -> None:
    """Delete every semantic row for a connection, recording one history entry.

    Also clears the legacy ``connections.semantic_model`` blob, rows or no rows:
    the migration that drops that column must find no connection with a blob but
    no ``semantic_models`` row, and this is the one place that knows the rows and
    the blob go together. ``null()``, not ``None``: ``None`` on a JSON column
    stores the JSON text ``'null'``.
    """
    await db.execute(
        update(Connection).where(Connection.id == connection_id).values(semantic_model=null())
    )
    header = await db.scalar(
        select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
    )
    if header is None:
        return
    _write_history(
        db,
        connection_id=connection_id,
        entity_type="model",
        entity_id=header.id,
        entity_key="",
        action="delete",
        source="user",
        old_value=_header_to_dict(header),
        new_value=None,
        changed_by=changed_by,
    )
    await db.delete(header)
    await db.flush()


# ── Read ──────────────────────────────────────────────────────────────────────


def _column_row_to_dict(col: SemanticColumnRow) -> dict[str, Any]:
    return {field: getattr(col, field) for field in _COLUMN_FIELDS}


async def load_model(db: AsyncSession, connection_id: str) -> SemanticModel | None:
    """Assemble the whole semantic model for a connection, or None if unset."""
    # populate_existing: a re-read under a row lock must see other transactions'
    # committed state, not an identity-mapped instance.
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None:
        return None

    table_rows = list(
        (
            await db.scalars(
                select(SemanticTableRow)
                .where(SemanticTableRow.model_id == header.id)
                .order_by(SemanticTableRow.table_key)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    columns_by_table: dict[str, list[SemanticColumnRow]] = {}
    for chunk in _chunks([row.id for row in table_rows]):
        rows = await db.scalars(
            select(SemanticColumnRow)
            .where(SemanticColumnRow.table_id.in_(list(chunk)))
            .order_by(SemanticColumnRow.column_name)
            .execution_options(populate_existing=True)
        )
        for col in rows:
            columns_by_table.setdefault(col.table_id, []).append(col)

    tables: dict[str, Any] = {}
    for row in table_rows:
        table_data: dict[str, Any] = {field: getattr(row, field) for field in _TABLE_FIELDS}
        table_data["columns"] = {
            col.column_name: _column_row_to_dict(col) for col in columns_by_table.get(row.id, [])
        }
        tables[row.table_key] = table_data

    metrics = (
        await db.scalars(
            select(SemanticMetricRow)
            .where(SemanticMetricRow.model_id == header.id)
            .order_by(SemanticMetricRow.ordinal)
        )
    ).all()
    relationships = (
        await db.scalars(
            select(SemanticRelationshipRow)
            .where(SemanticRelationshipRow.model_id == header.id)
            .order_by(SemanticRelationshipRow.ordinal)
        )
    ).all()
    globals_ = (
        await db.scalars(
            select(SemanticGlobalItemRow)
            .where(SemanticGlobalItemRow.model_id == header.id)
            .order_by(SemanticGlobalItemRow.kind, SemanticGlobalItemRow.ordinal)
        )
    ).all()

    data: dict[str, Any] = {field: getattr(header, field) for field in _MODEL_FIELDS}
    data["generation_status"] = header.generation_status
    data["generation_progress"] = header.generation_progress
    data["tables"] = tables
    data["business_metrics"] = [row.payload for row in metrics]
    data["relationships"] = [_relationship_row_to_dict(row) for row in relationships]
    data["segments"] = [row.payload for row in globals_ if row.kind == "segment"]
    data["derived_columns"] = [row.payload for row in globals_ if row.kind == "derived_column"]
    data["common_joins"] = [row.payload for row in globals_ if row.kind == "common_join"]
    # The model's own ``mode="before"`` validators coerce most stored shapes, but
    # a malformed value can still raise pydantic's ValidationError (a
    # ValueError), exactly as reading the blob did.
    return SemanticModel.model_validate(data)


# ── Generation run state ──────────────────────────────────────────────────────


async def begin_generation(
    db: AsyncSession,
    connection_id: str,
    *,
    schema_hash: str,
    tables_total: int,
    tables_done: int,
    batch_size: int,
    source_dialect: str,
    resume: bool = False,
) -> None:
    """Mark a generation run as started. Touches the header only — never the rows.

    The old init wrote ``"tables": {}``. Nothing is blanked any more: a
    regenerate is a reconcile, and the rows that exist are the resume state.

    A fresh run mints a new ``generation_run_id``; ``resume=True`` keeps the
    stored one so the tables the interrupted run already stamped stay done.
    That marker — not row existence — is what ``pending_table_keys`` and
    ``count_tables_in_run`` compare against: every table has a row before the
    first batch of a regenerate lands, so "has a row" would report the whole
    model done from the start. ``tables_done`` is supplied by the caller for the
    same reason: 0 for a fresh run, the stamped count for a resume.
    """
    # Fit first: the strings come from the datasource, and PostgreSQL rejects NULs
    # and over-long values the blob accepted.
    fitted = _fit_values(
        SemanticModelRow, {"schema_hash": schema_hash, "source_dialect": source_dialect}
    )
    header = await db.scalar(
        select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
    )
    if header is None:
        header = SemanticModelRow(connection_id=connection_id)
        db.add(header)
        await db.flush()
    old_value = _header_to_dict(header)
    if not resume or header.generation_run_id is None:
        header.generation_run_id = str(uuid.uuid4())
    header.schema_hash = fitted["schema_hash"]
    header.source_dialect = fitted["source_dialect"]
    header.generation_status = str(GenerationStatus.TABLES_PARTIAL)
    header.generation_progress = {
        "tables_done": tables_done,
        "tables_total": tables_total,
        "batch_size": batch_size,
    }
    header.version += 1
    header.updated_at = _now()
    _write_history(
        db,
        connection_id=connection_id,
        entity_type="model",
        entity_id=header.id,
        entity_key="",
        action="update",
        source="generation",
        old_value=old_value,
        new_value=_header_to_dict(header),
        changed_by=None,
    )
    await db.flush()


@dataclass(frozen=True)
class SemanticHeader:
    """The header row's run-state fields, without assembling the model."""

    generation_status: str
    schema_hash: str | None
    generation_progress: GenerationProgress | None
    generation_run_id: str | None


async def load_header(db: AsyncSession, connection_id: str) -> SemanticHeader | None:
    """Read the header row fresh, or ``None`` when the connection has no model.

    ``populate_existing`` bypasses the identity map: the batch route's post-lock
    re-read exists to see a status another request changed while this one
    awaited the LLM.
    """
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None:
        return None
    progress = header.generation_progress
    return SemanticHeader(
        generation_status=header.generation_status,
        schema_hash=header.schema_hash,
        generation_progress=GenerationProgress.model_validate(progress) if progress else None,
        generation_run_id=header.generation_run_id,
    )


async def count_tables_in_run(db: AsyncSession, connection_id: str) -> int:
    """How many table rows the run in flight has stamped — the progress figure."""
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None or header.generation_run_id is None:
        return 0
    return int(
        await db.scalar(
            select(func.count())
            .select_from(SemanticTableRow)
            .where(
                SemanticTableRow.connection_id == connection_id,
                SemanticTableRow.generation_run_id == header.generation_run_id,
            )
        )
        or 0
    )


async def advance_progress(db: AsyncSession, connection_id: str) -> GenerationProgress | None:
    """Recompute ``tables_done`` from this run's stamped rows; return the new progress.

    The recount is only correct when the caller serialises batches: the batch
    route holds the connection row lock, so no other batch stamps rows between
    the count and the write.
    """
    header = await db.scalar(
        select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
    )
    if header is None or not header.generation_progress:
        return None
    # Count first: the count re-reads the header (populate_existing), so the
    # progress dict below is built from the freshly loaded value, not a stale one.
    done = await count_tables_in_run(db, connection_id)
    header.generation_progress = {**header.generation_progress, "tables_done": done}
    await db.flush()
    return GenerationProgress.model_validate(header.generation_progress)


async def pending_table_keys(
    db: AsyncSession, connection_id: str, all_keys: Sequence[str]
) -> list[str]:
    """The keys in ``all_keys`` the run in flight has not stamped, in order.

    With no run in flight (no header, or no run id) every key is pending.
    Chunked so a wide schema cannot exceed the per-statement bind limit.
    """
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None or header.generation_run_id is None:
        return list(all_keys)
    done: set[str] = set()
    for chunk in _chunks(list(all_keys)):
        rows = await db.execute(
            select(SemanticTableRow.table_key).where(
                SemanticTableRow.connection_id == connection_id,
                SemanticTableRow.generation_run_id == header.generation_run_id,
                SemanticTableRow.table_key.in_(list(chunk)),
            )
        )
        done |= {key for (key,) in rows.all()}
    return [key for key in all_keys if key not in done]


async def reconcile_tables(
    db: AsyncSession,
    connection_id: str,
    generated: Mapping[str, TableSemantic],
    *,
    updated_by: str | None,
    expected_run_id: str | None = None,
) -> None:
    """Upsert the generator's output, skipping every field a human wrote.

    For each generated table: an existing row takes the new value on every
    field **not** in its ``user_edited_fields``; a missing row is inserted with
    empty provenance. Columns likewise. Tables the batch does not name are not
    touched. ``is_sensitive`` is additionally never cleared by generation.

    Nothing is ever deleted here: a table or column that left the schema keeps
    its row through every batch. Removing those is ``/generate/globals``'s job,
    via ``generator.prune_to_schema`` before its full save (the CE has no
    apply-drift).

    A regenerate identical to what is stored writes no history row and bumps
    no version, but every table the batch names is stamped with the header's
    ``generation_run_id`` — that stamp is what tells a resume and the progress
    figure that this run has reached the table.

    Every generated key and value is fitted (``_fit_values``) *before* it is
    compared with the stored row, because the stored row holds the fitted value:
    comparing the raw one would see an over-long or NUL-bearing value as changed
    on every regenerate. Raises ``ValueError`` (before touching the session) for
    an over-long table key or column name.

    ``expected_run_id`` is the run the caller generated for. When given and the
    header's ``generation_run_id`` differs (a fresh ``/generate/init`` restarted
    the run), raises ``SemanticStoreConflict`` before writing anything: stamping
    this output with the new run id would make a resume of that run skip tables
    it never generated.
    """
    # Fit everything first: a ValueError must leave the session untouched. A
    # later key that fits to the same string as an earlier one replaces it.
    fitted_tables: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for raw_key, table in generated.items():
        table_values = _fit_values(
            SemanticTableRow,
            {"table_key": raw_key, **_table_row_values(table.model_dump(mode="json"))},
        )
        columns: dict[str, dict[str, Any]] = {}
        for raw_name, column in table.columns.items():
            col_values = _fit_values(
                SemanticColumnRow,
                {"column_name": raw_name, **_column_row_values(column.model_dump(mode="json"))},
            )
            columns[col_values["column_name"]] = col_values
        fitted_tables[table_values["table_key"]] = (table_values, columns)

    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if expected_run_id is not None and (
        header is None or header.generation_run_id != expected_run_id
    ):
        raise SemanticStoreConflict(
            f"generation run {expected_run_id!r} was superseded "
            f"(now {header.generation_run_id if header else None!r})"
        )
    if header is None:
        header = SemanticModelRow(connection_id=connection_id)
        db.add(header)
        await db.flush()
    run_id = header.generation_run_id

    existing_tables: dict[str, SemanticTableRow] = {}
    for chunk in _chunks(list(fitted_tables)):
        rows = await db.scalars(
            select(SemanticTableRow).where(
                SemanticTableRow.model_id == header.id,
                SemanticTableRow.table_key.in_(list(chunk)),
            )
        )
        existing_tables.update({row.table_key: row for row in rows})

    new_table_rows: list[SemanticTableRow] = []
    for table_key, (generated_values, _columns) in fitted_tables.items():
        row = existing_tables.get(table_key)
        if row is None:
            row = SemanticTableRow(
                id=str(uuid.uuid4()),
                model_id=header.id,
                connection_id=connection_id,
                generation_run_id=run_id,
                updated_at=_now(),
                updated_by=updated_by,
                **generated_values,
            )
            new_table_rows.append(row)
            existing_tables[table_key] = row
            continue
        row.generation_run_id = run_id
        protected = set(row.user_edited_fields)
        old_value = {field: getattr(row, field) for field in _TABLE_FIELDS}
        new_value = {**old_value}
        for field in _TABLE_FIELDS:
            if field not in protected:
                new_value[field] = generated_values[field]
        if new_value == old_value:
            continue
        for field, value in new_value.items():
            setattr(row, field, value)
        row.version += 1
        row.updated_at = _now()
        row.updated_by = updated_by
        _write_history(
            db,
            connection_id=connection_id,
            entity_type="table",
            entity_id=row.id,
            entity_key=table_key,
            action="update",
            source="generation",
            old_value=old_value,
            new_value=new_value,
            changed_by=updated_by,
        )
    for chunk in _chunks(new_table_rows):
        db.add_all(chunk)
    await db.flush()

    table_ids = [row.id for row in existing_tables.values()]
    existing_columns: dict[tuple[str, str], SemanticColumnRow] = {}
    for chunk in _chunks(table_ids):
        cols = await db.scalars(
            select(SemanticColumnRow).where(SemanticColumnRow.table_id.in_(list(chunk)))
        )
        existing_columns.update({(c.table_id, c.column_name): c for c in cols})

    new_column_rows: list[SemanticColumnRow] = []
    for table_key, (_table_values, columns) in fitted_tables.items():
        table_row = existing_tables[table_key]
        for column_name, generated_col_values in columns.items():
            col = existing_columns.get((table_row.id, column_name))
            if col is None:
                new_column_rows.append(
                    SemanticColumnRow(
                        table_id=table_row.id,
                        updated_at=_now(),
                        updated_by=updated_by,
                        **generated_col_values,
                    )
                )
                continue
            protected = set(col.user_edited_fields)
            old_value = {field: getattr(col, field) for field in _COLUMN_FIELDS}
            new_value = {**old_value}
            for field in _COLUMN_FIELDS:
                if field not in protected:
                    new_value[field] = generated_col_values[field]
            # Generation never clears a flag, whoever set it.
            new_value["is_sensitive"] = old_value["is_sensitive"] or new_value["is_sensitive"]
            if new_value == old_value:
                continue
            for field, value in new_value.items():
                setattr(col, field, value)
            col.version += 1
            col.updated_at = _now()
            col.updated_by = updated_by
            _write_history(
                db,
                connection_id=connection_id,
                entity_type="column",
                entity_id=col.id,
                entity_key=f"{table_key}.{column_name}",
                action="update",
                source="generation",
                old_value=old_value,
                new_value=new_value,
                changed_by=updated_by,
            )
    for chunk in _chunks(new_column_rows):
        db.add_all(chunk)
    await db.flush()


# ── Provenance ────────────────────────────────────────────────────────────────


async def provenance_map(db: AsyncSession, connection_id: str) -> ProvenanceMap:
    """Every row's ``user_edited_fields``, keyed by entity so a full save can restore it.

    Reads fresh (``populate_existing``): it runs right before a full rebuild and
    must see committed state, not stale identity-mapped rows.
    """
    out: ProvenanceMap = {
        "model": {},
        "table": {},
        "column": {},
        "metric": {},
        "global_item": {},
        "relationship": {},
    }
    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None:
        return out
    if header.user_edited_fields:
        out["model"][_HEADER_KEY] = list(header.user_edited_fields)
    tables = list(
        (
            await db.scalars(
                select(SemanticTableRow)
                .where(SemanticTableRow.model_id == header.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    key_by_table_id = {row.id: row.table_key for row in tables}
    for row in tables:
        if row.user_edited_fields:
            out["table"][row.table_key] = list(row.user_edited_fields)
    for chunk in _chunks(list(key_by_table_id)):
        cols = await db.scalars(
            select(SemanticColumnRow)
            .where(SemanticColumnRow.table_id.in_(list(chunk)))
            .execution_options(populate_existing=True)
        )
        for col in cols:
            if col.user_edited_fields:
                key = f"{key_by_table_id[col.table_id]}.{col.column_name}"
                out["column"][key] = list(col.user_edited_fields)
    metrics = await db.scalars(
        select(SemanticMetricRow)
        .where(SemanticMetricRow.model_id == header.id)
        .execution_options(populate_existing=True)
    )
    for m in metrics:
        if m.user_edited_fields:
            out["metric"][m.name] = list(m.user_edited_fields)
    items = await db.scalars(
        select(SemanticGlobalItemRow)
        .where(SemanticGlobalItemRow.model_id == header.id)
        .execution_options(populate_existing=True)
    )
    for item in items:
        if item.user_edited_fields:
            out["global_item"][f"{item.kind}:{item.name}"] = list(item.user_edited_fields)
    rels = await db.scalars(
        select(SemanticRelationshipRow)
        .where(SemanticRelationshipRow.model_id == header.id)
        .execution_options(populate_existing=True)
    )
    for rel in rels:
        if rel.user_edited_fields:
            out["relationship"][relationship_key(_relationship_row_to_dict(rel))] = list(
                rel.user_edited_fields
            )
    return out


async def _protected_values(
    db: AsyncSession, model_id: str, provenance: ProvenanceMap
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{"table": {key: {field: stored_value}}, "column": {...}}`` for edited fields.

    Read before ``save_model`` deletes the rows, and fresh (``populate_existing``)
    so the values are the committed ones.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {"table": {}, "column": {}}
    if not provenance.get("table") and not provenance.get("column"):
        return out
    tables = list(
        (
            await db.scalars(
                select(SemanticTableRow)
                .where(SemanticTableRow.model_id == model_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    key_by_id = {row.id: row.table_key for row in tables}
    for row in tables:
        fields = provenance.get("table", {}).get(row.table_key)
        if fields:
            out["table"][row.table_key] = {f: getattr(row, f) for f in fields if f in _TABLE_FIELDS}
    wanted = provenance.get("column", {})
    if wanted:
        for chunk in _chunks(list(key_by_id)):
            cols = await db.scalars(
                select(SemanticColumnRow)
                .where(SemanticColumnRow.table_id.in_(list(chunk)))
                .execution_options(populate_existing=True)
            )
            for col in cols:
                key = f"{key_by_id[col.table_id]}.{col.column_name}"
                fields = wanted.get(key)
                if fields:
                    out["column"][key] = {f: getattr(col, f) for f in fields if f in _COLUMN_FIELDS}
    return out


def carry_edited_relationships(
    current: Sequence[RelationshipEdge],
    fresh: Sequence[RelationshipEdge],
    provenance: ProvenanceMap,
    *,
    render_tables: set[str],
) -> list[RelationshipEdge]:
    """The relationship list a rebuild saves: ``fresh`` bounded to
    ``render_tables``, with every marked edge of ``current`` carried forward in
    place of its rebuilt twin.

    A marked edge — a user-authored join the catalog has no row for, or an FK
    edge whose description a user rewrote — is kept as stored. Globals passes
    the set of *annotated* tables as ``render_tables``, and under
    ``tables_partial`` ("finish with what's generated") a user-authored edge to
    a real but unannotated table is not evidence of anything wrong, so kept
    edges are not bounded by it.
    """
    marked = provenance.get("relationship", {})
    kept = [r for r in current if relationship_key(r) in marked]
    kept_keys = {relationship_key(r) for r in kept}
    rebuilt = [
        e
        for e in fresh
        if e.from_table in render_tables
        and e.to_table in render_tables
        and relationship_key(e) not in kept_keys
    ]
    return rebuilt + kept


# ── Member patch and optimistic locking ───────────────────────────────────────


class SemanticStoreConflict(Exception):  # noqa: N818 — the router maps it to 409
    """An optimistic-lock update matched no row — someone else wrote first."""


# A ``None`` on these fields means "not supplied" and must preserve what is
# stored; only ``[]`` is an explicit clear. ``display_name`` is here because it
# is a non-nullable str on the Pydantic side, so a null cannot mean "clear".
# The frontend PUTs the whole draft model, so an untouched section serialised
# as null hits this on an ordinary save.
_TABLE_NOT_SUPPLIED_ON_NULL: frozenset[str] = frozenset({"default_filters", "display_name"})
_COLUMN_NOT_SUPPLIED_ON_NULL: frozenset[str] = frozenset(
    {"value_mappings", "display_name", "is_non_additive", "is_sensitive", "semantic_type"}
)
_GLOBAL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("segment", "segments"),
    ("derived_column", "derived_columns"),
    ("common_join", "common_joins"),
)


_METRIC_ADAPTER: TypeAdapter[Any] = TypeAdapter(BusinessMetric)
_GLOBAL_ITEM_MODELS: dict[str, type[BaseModel]] = {
    "segment": Segment,
    "derived_column": DerivedColumn,
    "common_join": CommonJoin,
}


# A stored value is diffed in the shape ``load_model`` would serve it, not raw:
# migration 0003 copies legacy payloads verbatim, so the echo of a migrated row
# can differ from the raw row only in shape (a missing defaulted key, a coerced
# enum) and would otherwise be marked user-edited, freezing it against
# regeneration. A value that fails validation falls back to raw — the diff path
# never raises.
def _canonical(validate: Callable[[Any], BaseModel], raw: Any) -> Any:
    try:
        return validate(raw).model_dump(mode="json")
    except (ValueError, TypeError):
        return raw


def _canonical_metric(payload: dict[str, Any]) -> dict[str, Any]:
    return _canonical(_METRIC_ADAPTER.validate_python, payload)


def _canonical_global_item(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _canonical(_GLOBAL_ITEM_MODELS[kind].model_validate, payload)


def _canonical_relationship(row: SemanticRelationshipRow) -> dict[str, Any]:
    return _canonical(RelationshipEdge.model_validate, _relationship_row_to_dict(row))


def _canonical_table_row(row: SemanticTableRow) -> dict[str, Any]:
    raw = {field: getattr(row, field) for field in _TABLE_FIELDS}
    canon = _canonical(TableSemantic.model_validate, raw)
    return {field: canon.get(field) for field in _TABLE_FIELDS}


def _canonical_column_row(row: SemanticColumnRow) -> dict[str, Any]:
    canon = _canonical(ColumnSemantic.model_validate, _column_row_to_dict(row))
    return {field: canon.get(field) for field in _COLUMN_FIELDS}


# What a row inserted with nothing sent would hold. A new row is marked only on
# the fields whose sent value differs: a stale draft re-creating a pruned column
# echoes ``is_sensitive=False`` and friends, and marking those would stop
# ``reconcile_tables`` from ever flagging the column sensitive.
_TABLE_DEFAULTS: dict[str, Any] = TableSemantic(display_name="").model_dump(
    mode="json", exclude={"columns"}
)
_COLUMN_DEFAULTS: dict[str, Any] = ColumnSemantic(display_name="").model_dump(mode="json")


def _insert_marks(values: Mapping[str, Any], defaults: Mapping[str, Any]) -> list[str]:
    """Provenance for a newly inserted row: the sent fields that differ from the default."""
    return sorted(field for field, value in values.items() if value != defaults.get(field))


async def update_table_fields(
    db: AsyncSession,
    table_id: str,
    expected_version: int,
    values: dict[str, Any],
    *,
    updated_by: str | None,
    mark_user_edited: bool,
) -> None:
    """Apply ``values`` to one table row under an optimistic lock.

    ``values`` is fitted (``_fit_values``) before the diff, because the stored
    row holds fitted values, and is compared with the stored row in its
    canonical shape (``_canonical_table_row``). Only fields that then differ are
    written — an echoed field bumps neither ``version`` nor
    ``user_edited_fields`` nor the history log.

    The write is ``UPDATE … WHERE id = :id AND version = :version`` issued as a
    Core statement so the predicate is part of the SQL: under READ COMMITTED two
    overlapping transactions can both read the same version, and only the
    predicate in the UPDATE tells them apart. A zero rowcount means a concurrent
    writer moved the row on, and raises ``SemanticStoreConflict``.
    """
    values = _fit_values(SemanticTableRow, values)
    row = await db.scalar(
        select(SemanticTableRow)
        .where(SemanticTableRow.id == table_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise SemanticStoreConflict(f"semantic_tables row {table_id!r} changed under this edit")

    old_value = {field: getattr(row, field) for field in _TABLE_FIELDS}
    canonical = _canonical_table_row(row)
    diff = {field: value for field, value in values.items() if canonical.get(field) != value}
    if not diff:
        return

    connection_id = row.connection_id
    table_key = row.table_key
    update_values: dict[str, Any] = {
        **diff,
        "version": expected_version + 1,
        "updated_at": _now(),
        "updated_by": updated_by,
    }
    if mark_user_edited:
        update_values["user_edited_fields"] = sorted(set(row.user_edited_fields) | set(diff))

    result = await db.execute(
        update(SemanticTableRow)
        .where(SemanticTableRow.id == table_id, SemanticTableRow.version == expected_version)
        .values(**update_values)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise SemanticStoreConflict(f"semantic_tables row {table_id!r} changed under this edit")
    # The session's default ``synchronize_session`` already updates the
    # identity-mapped ``row`` in memory; do not ``expire`` it (a sync read of
    # ``row.version`` straight after would raise MissingGreenlet).

    _write_history(
        db,
        connection_id=connection_id,
        entity_type="table",
        entity_id=table_id,
        entity_key=table_key,
        action="update",
        source="user" if mark_user_edited else "generation",
        old_value=old_value,
        new_value={**old_value, **diff},
        changed_by=updated_by,
    )
    await db.flush()


def _sent(patch: BaseModel, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The fields the client sent, each dumped in full.

    ``model_dump(exclude_unset=True)`` would also drop unset keys *inside* each
    nested item (a metric, a value mapping), storing it partial; ``load_model``
    then fills the defaults and the next echo of it reads as a change. So
    ``model_fields_set`` decides which fields were sent, and their contents are
    dumped whole.
    """
    dumped = patch.model_dump(mode="json", exclude=set(exclude))
    return {key: dumped[key] for key in patch.model_fields_set if key not in exclude}


def _supplied(patch: dict[str, Any], not_supplied_on_null: frozenset[str]) -> dict[str, Any]:
    """Drop keys whose null means "not supplied" rather than "clear"."""
    return {
        key: value
        for key, value in patch.items()
        if not (value is None and key in not_supplied_on_null)
    }


def _write_section_history(
    db: AsyncSession,
    header: SemanticModelRow,
    *,
    entity_type: str,
    section: str,
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    changed_by: str | None,
) -> None:
    """One history row for a wholesale-rewritten list section, keyed by its field
    name; ``items`` are the payloads in ordinal order."""
    _write_history(
        db,
        connection_id=header.connection_id,
        entity_type=entity_type,
        entity_id=header.id,
        entity_key=section,
        action="update",
        source="user",
        old_value={"items": old_items},
        new_value={"items": new_items},
        changed_by=changed_by,
    )


# {table_key: (table values, {column_name: column values} or None)}, all fitted.
_FittedTables = dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]] | None]]


def _fit_table_patches(patch: SemanticModelUpdate) -> _FittedTables:
    """Fit every table and column patch, keyed by fitted key; a later key that
    fits to the same string as an earlier one replaces it."""
    out: _FittedTables = {}
    for raw_key, table_patch in (patch.tables or {}).items():
        values = _fit_values(
            SemanticTableRow,
            {
                "table_key": raw_key,
                **_supplied(
                    _sent(table_patch, exclude=frozenset({"columns"})),
                    _TABLE_NOT_SUPPLIED_ON_NULL,
                ),
            },
        )
        table_key = values.pop("table_key")
        columns: dict[str, dict[str, Any]] | None = None
        if table_patch.columns is not None:
            columns = {}
            for raw_name, column_patch in table_patch.columns.items():
                col_values = _fit_values(
                    SemanticColumnRow,
                    {
                        "column_name": raw_name,
                        **_supplied(_sent(column_patch), _COLUMN_NOT_SUPPLIED_ON_NULL),
                    },
                )
                columns[col_values.pop("column_name")] = col_values
        out[table_key] = (values, columns)
    return out


async def apply_user_patch(
    db: AsyncSession,
    connection_id: str,
    patch: SemanticModelUpdate,
    *,
    updated_by: str,
) -> SemanticModel:
    """Apply a user's partial update, recording per-field provenance.

    Touching a field marks it in that row's ``user_edited_fields``, which is
    what makes the next regenerate skip it. Global sections stay wholesale
    replacements when supplied (the existing API contract), but their
    provenance is per item: only an item that is new or whose payload changed
    is marked. Every comparison is against what is *stored*, because the
    frontend PUTs the whole draft on every save — and every incoming value is
    fitted (``_fit_values``) first, because what is stored is fitted.

    Every change is audited: table and column edits per row, a changed
    ``notes``/``is_user_reviewed`` as one ``model`` history row holding just the
    changed fields, and each rewritten list section as one row keyed by its
    field name. An echoed section or field writes no history.

    Stored values are compared in their canonical shape (``_canonical_*``), so a
    migrated row whose raw payload differs from an echo only in shape is not
    marked user-edited.

    The caller must hold the connection row lock (``lock_and_reread_connection``)
    for the whole call: table rows carry an optimistic version check
    (``update_table_fields``), but column rows and the rewritten list sections
    do not.

    Raises ``ValueError`` (before touching the session) for an over-long table
    key, column name, metric name, global-item name or relationship endpoint.
    """
    # Fit everything first: a ValueError must leave the session untouched.
    fitted_tables = _fit_table_patches(patch)
    section_dump = {
        key: value
        for key, value in _sent(patch, exclude=frozenset({"tables"})).items()
        if value is not None
    }

    fitted_globals: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, field in _GLOBAL_SECTIONS:
        if field in section_dump:
            fitted_globals[kind, field] = _dedupe_last_wins(
                [
                    _fit_values(
                        SemanticGlobalItemRow,
                        {"name": _global_item_name(kind, payload), "payload": payload},
                    )
                    for payload in section_dump[field]
                ],
                key=lambda values: values["name"],
            )
    fitted_metrics: list[dict[str, Any]] | None = None
    if "business_metrics" in section_dump:
        fitted_metrics = _dedupe_last_wins(
            [
                _fit_values(
                    SemanticMetricRow,
                    {
                        "name": str(payload.get("name", "")),
                        "metric_type": str(payload.get("metric_type", "simple")),
                        "payload": payload,
                    },
                )
                for payload in section_dump["business_metrics"]
            ],
            key=lambda values: values["name"],
        )
    fitted_rels: list[dict[str, Any]] | None = None
    if "relationships" in section_dump:
        fitted_rels = [
            _fit_values(SemanticRelationshipRow, _relationship_patch_to_dict(payload))
            for payload in section_dump["relationships"]
        ]
    fitted_header = _fit_values(
        SemanticModelRow,
        {f: section_dump[f] for f in ("notes", "is_user_reviewed") if f in section_dump},
    )

    header = await db.scalar(
        select(SemanticModelRow)
        .where(SemanticModelRow.connection_id == connection_id)
        .execution_options(populate_existing=True)
    )
    if header is None:
        await save_model(db, connection_id, SemanticModel(), updated_by=updated_by)
        header = await db.scalar(
            select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
        )
        assert header is not None

    for table_key, (table_values, column_values) in fitted_tables.items():
        await _apply_table_patch(
            db,
            header,
            connection_id,
            table_key,
            table_values,
            column_values,
            updated_by=updated_by,
        )

    for (kind, field), items in fitted_globals.items():
        existing_rows = list(
            (
                await db.scalars(
                    select(SemanticGlobalItemRow)
                    .where(
                        SemanticGlobalItemRow.model_id == header.id,
                        SemanticGlobalItemRow.kind == kind,
                    )
                    .order_by(SemanticGlobalItemRow.ordinal)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        canonical_items = [_canonical_global_item(kind, row.payload) for row in existing_rows]
        if _payload_lists_equal(canonical_items, (item["payload"] for item in items)):
            continue
        marks = _stored_marks(
            (row.name, canonical, row.user_edited_fields)
            for row, canonical in zip(existing_rows, canonical_items, strict=True)
        )
        _write_section_history(
            db,
            header,
            entity_type="global_item",
            section=field,
            old_items=[row.payload for row in existing_rows],
            new_items=[item["payload"] for item in items],
            changed_by=updated_by,
        )
        await db.execute(
            delete(SemanticGlobalItemRow).where(
                SemanticGlobalItemRow.model_id == header.id,
                SemanticGlobalItemRow.kind == kind,
            )
        )
        for ordinal, item in enumerate(items):
            db.add(
                SemanticGlobalItemRow(
                    model_id=header.id,
                    connection_id=connection_id,
                    kind=kind,
                    name=item["name"],
                    payload=item["payload"],
                    ordinal=ordinal,
                    user_edited_fields=_item_marks(marks, item["name"], item["payload"]),
                    updated_at=_now(),
                    updated_by=updated_by,
                )
            )

    if fitted_metrics is not None:
        existing_metric_rows = list(
            (
                await db.scalars(
                    select(SemanticMetricRow)
                    .where(SemanticMetricRow.model_id == header.id)
                    .order_by(SemanticMetricRow.ordinal)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        canonical_metrics = [_canonical_metric(row.payload) for row in existing_metric_rows]
        if not _payload_lists_equal(
            canonical_metrics, (metric["payload"] for metric in fitted_metrics)
        ):
            marks = _stored_marks(
                (row.name, canonical, row.user_edited_fields)
                for row, canonical in zip(existing_metric_rows, canonical_metrics, strict=True)
            )
            _write_section_history(
                db,
                header,
                entity_type="metric",
                section="business_metrics",
                old_items=[row.payload for row in existing_metric_rows],
                new_items=[metric["payload"] for metric in fitted_metrics],
                changed_by=updated_by,
            )
            await db.execute(
                delete(SemanticMetricRow).where(SemanticMetricRow.model_id == header.id)
            )
            for ordinal, metric in enumerate(fitted_metrics):
                db.add(
                    SemanticMetricRow(
                        model_id=header.id,
                        connection_id=connection_id,
                        ordinal=ordinal,
                        user_edited_fields=_item_marks(marks, metric["name"], metric["payload"]),
                        updated_at=_now(),
                        updated_by=updated_by,
                        **metric,
                    )
                )

    if fitted_rels is not None:
        existing_rel_rows = list(
            (
                await db.scalars(
                    select(SemanticRelationshipRow)
                    .where(SemanticRelationshipRow.model_id == header.id)
                    .order_by(SemanticRelationshipRow.ordinal)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        existing_rel = [_canonical_relationship(row) for row in existing_rel_rows]
        if not _payload_lists_equal(existing_rel, fitted_rels):
            marks = _stored_marks(
                (_relationship_identity(edge), edge, row.user_edited_fields)
                for row, edge in zip(existing_rel_rows, existing_rel, strict=True)
            )
            _write_section_history(
                db,
                header,
                entity_type="relationship",
                section="relationships",
                old_items=[_relationship_row_to_dict(row) for row in existing_rel_rows],
                new_items=fitted_rels,
                changed_by=updated_by,
            )
            await db.execute(
                delete(SemanticRelationshipRow).where(SemanticRelationshipRow.model_id == header.id)
            )
            for ordinal, edge in enumerate(fitted_rels):
                db.add(
                    SemanticRelationshipRow(
                        model_id=header.id,
                        connection_id=connection_id,
                        ordinal=ordinal,
                        user_edited_fields=_item_marks(marks, _relationship_identity(edge), edge),
                        updated_at=_now(),
                        updated_by=updated_by,
                        **edge,
                    )
                )

    touched_header = {
        field: value for field, value in fitted_header.items() if getattr(header, field) != value
    }
    if touched_header:
        old_header = {field: getattr(header, field) for field in touched_header}
        for field, value in touched_header.items():
            setattr(header, field, value)
        _write_history(
            db,
            connection_id=connection_id,
            entity_type="model",
            entity_id=header.id,
            entity_key=_HEADER_KEY,
            action="update",
            source="user",
            old_value=old_header,
            new_value=touched_header,
            changed_by=updated_by,
        )
        header.user_edited_fields = sorted(set(header.user_edited_fields) | set(touched_header))
        header.version += 1
        header.updated_at = _now()
        header.updated_by = updated_by
    await db.flush()

    loaded = await load_model(db, connection_id)
    assert loaded is not None
    return loaded


async def _apply_table_patch(
    db: AsyncSession,
    header: SemanticModelRow,
    connection_id: str,
    table_key: str,
    values: dict[str, Any],
    columns: dict[str, dict[str, Any]] | None,
    *,
    updated_by: str,
) -> None:
    """Apply one table's fitted ``values`` and its fitted column patches."""
    row = await db.scalar(
        select(SemanticTableRow)
        .where(
            SemanticTableRow.model_id == header.id,
            SemanticTableRow.table_key == table_key,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        row = SemanticTableRow(
            id=str(uuid.uuid4()),
            model_id=header.id,
            connection_id=connection_id,
            table_key=table_key,
            updated_at=_now(),
            updated_by=updated_by,
            user_edited_fields=_insert_marks(values, _TABLE_DEFAULTS),
        )
        for field, value in values.items():
            setattr(row, field, value)
        db.add(row)
        await db.flush()
        _write_history(
            db,
            connection_id=connection_id,
            entity_type="table",
            entity_id=row.id,
            entity_key=table_key,
            action="insert",
            source="user",
            old_value=None,
            new_value=values,
            changed_by=updated_by,
        )
    elif values:
        await update_table_fields(
            db, row.id, row.version, values, updated_by=updated_by, mark_user_edited=True
        )

    for column_name, column_values in (columns or {}).items():
        await _apply_column_patch(db, row, column_name, column_values, updated_by=updated_by)


async def _apply_column_patch(
    db: AsyncSession,
    table_row: SemanticTableRow,
    column_name: str,
    values: dict[str, Any],
    *,
    updated_by: str,
) -> None:
    """Apply one column's fitted ``values``, writing only what differs from storage."""
    row = await db.scalar(
        select(SemanticColumnRow)
        .where(
            SemanticColumnRow.table_id == table_row.id,
            SemanticColumnRow.column_name == column_name,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        row = SemanticColumnRow(
            table_id=table_row.id,
            column_name=column_name,
            updated_at=_now(),
            updated_by=updated_by,
            user_edited_fields=_insert_marks(values, _COLUMN_DEFAULTS),
        )
        for field, value in values.items():
            setattr(row, field, value)
        db.add(row)
        await db.flush()
        _write_history(
            db,
            connection_id=table_row.connection_id,
            entity_type="column",
            entity_id=row.id,
            entity_key=f"{table_row.table_key}.{column_name}",
            action="insert",
            source="user",
            old_value=None,
            new_value=values,
            changed_by=updated_by,
        )
        return

    old_value = {field: getattr(row, field) for field in _COLUMN_FIELDS}
    canonical = _canonical_column_row(row)
    diff_values = {field: value for field, value in values.items() if canonical.get(field) != value}
    if not diff_values:
        # An echo, not an edit: no setattr, no version bump, no history row, and
        # ``user_edited_fields`` is left exactly as it was.
        return
    for field, value in diff_values.items():
        setattr(row, field, value)
    row.user_edited_fields = sorted(set(row.user_edited_fields) | set(diff_values))
    row.version += 1
    row.updated_at = _now()
    row.updated_by = updated_by
    _write_history(
        db,
        connection_id=table_row.connection_id,
        entity_type="column",
        entity_id=row.id,
        entity_key=f"{table_row.table_key}.{column_name}",
        action="update",
        source="user",
        old_value=old_value,
        new_value={**old_value, **diff_values},
        changed_by=updated_by,
    )
    await db.flush()
