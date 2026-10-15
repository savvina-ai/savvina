# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Backfill the normalised semantic tables from Connection.semantic_model.

Deliberately self-contained: it reads the blob with plain SQLAlchemy Core and
writes rows field by field, importing nothing from ``app.semantic``. An
application import would make this migration's behaviour change whenever the
Pydantic model does, and a migration has to mean the same thing forever. The
field-name lists below duplicate ``app/semantic/store.py``'s on purpose —
``tests/test_semantic/test_store.py::TestFieldListsMatchTheMigration`` is the
tripwire that keeps them aligned.

``user_edited_fields`` is seeded empty for every row: nothing in the blob
distinguishes LLM output from a hand edit. The first regenerate after this
migration therefore treats every pre-existing value as regenerable — exactly
today's behaviour, so nothing gets worse.

A blob that is not a JSON object is skipped rather than aborting the whole
migration: one connection's bad data must not block every other connection's
backfill.

Over-long text is truncated to each bounded column's reflected length.
NUL characters are stripped from text columns; a NUL inside a JSON payload still
fails that connection on PostgreSQL (JSONB rejects \\u0000) and is reported in
the summary line. Identity values (table and column keys, metric names,
relationship endpoints) are never truncated: a row whose identity does not fit
is skipped with a warning instead.

``connections.semantic_model`` is left in place and unread. It is the fallback
for one release and is dropped in a later migration. That migration must first
verify that no connection has a blob that is a non-empty JSON object (not SQL
NULL, JSON null or ``{}``) and no ``semantic_models`` row, because for a
connection this backfill skipped or failed on, the blob is the only copy.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from typing import Any
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)

_MODEL_FIELDS = (
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
_MODEL_DEFAULTS: dict[str, Any] = {
    "source_dialect": "sql",
    "is_user_reviewed": False,
    "generation_warnings": [],
    "notes": [],
    "time_expressions": {},
}
_TABLE_FIELDS = (
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
_TABLE_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "default_filters": [],
    "hierarchies": [],
    "partition_columns": [],
    "cluster_columns": [],
}
_COLUMN_FIELDS = (
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
_COLUMN_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "is_sensitive": False,
    "semantic_type": "unknown",
    "is_non_additive": False,
    "value_mappings": [],
}
_CHUNK = 500

# Legacy blob values can be truthy-but-not-bool; the target columns are Boolean.
_BOOL_FIELDS = frozenset({"is_user_reviewed", "is_sensitive", "is_non_additive"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})
_TARGET_TABLES = (
    "semantic_models",
    "semantic_tables",
    "semantic_columns",
    "semantic_metrics",
    "semantic_relationships",
    "semantic_global_items",
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _provenance(now: datetime) -> dict[str, Any]:
    return {"user_edited_fields": [], "version": 1, "updated_at": now, "updated_by": None}


def _as_bool(value: Any) -> bool:
    """``bool()`` with the legacy string spellings of false understood."""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_STRINGS
    return bool(value)


def _pick(source: dict[str, Any], fields: tuple[str, ...], defaults: dict[str, Any]) -> dict:
    out: dict[str, Any] = {}
    for field in fields:
        value = source.get(field)
        picked = defaults.get(field) if value is None else value
        if field in _BOOL_FIELDS:
            picked = _as_bool(picked)
        out[field] = picked
    return out


def _as_dict(raw: Any) -> dict[str, Any] | None:
    """The column is plain JSON; some drivers hand back a string."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return raw if isinstance(raw, dict) else None


def _is_json_null(raw: Any) -> bool:
    """SQL NULL or the JSON value ``null`` (what ``DELETE /semantic`` leaves behind)."""
    if raw is None:
        return True
    if isinstance(raw, str):
        try:
            return json.loads(raw) is None
        except ValueError:
            return False
    return False


def _reflect(conn: sa.engine.Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=conn) for name in _TARGET_TABLES}


def _clean_row(row: dict[str, Any], target: sa.Table) -> dict[str, Any]:
    """Make every top-level string fit its column.

    PostgreSQL rejects a NUL character in text and an over-long value in a
    bounded VARCHAR; either would raise and, via the per-connection SAVEPOINT,
    discard that connection's whole model. Strings inside JSON payloads are left
    alone. Identity values (keys, names) are checked for length *before* they
    reach here (``_fit_identity``) because truncating one would change what it
    means; they pass through unchanged.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        column = target.c.get(key)
        if isinstance(value, str) and column is not None and isinstance(column.type, sa.String):
            value = value.replace("\x00", "")
            length = column.type.length
            if length is not None:
                value = value[:length]
        out[key] = value
    return out


def _fit_identity(value: Any, column: sa.Column) -> str | None:
    """The identity value as text, NULs removed, or ``None`` if it exceeds the column."""
    text = str(value).replace("\x00", "")
    length = getattr(column.type, "length", None)
    if length is not None and len(text) > length:
        return None
    return text


# Same rule as ``store._MAX_INDEXED_TEXT_BYTES``: ``semantic_global_items.name`` is
# Text but sits in a unique btree index, which PostgreSQL caps at about a third of
# a page. Measured in UTF-8 bytes, which is what the index measures.
_MAX_INDEXED_NAME_BYTES = 2000


def _warn_skipped(
    connection_id: Any,
    what: str,
    key: Any,
    *,
    reason: str = "identity value exceeds its column length",
) -> None:
    logger.warning(
        "connection %s: skipping %s %r: %s",
        connection_id,
        what,
        str(key)[:80],
        reason,
    )


def _insert_chunked(
    conn: sa.engine.Connection, table: str, rows: list[dict], reflected: dict[str, sa.Table]
) -> None:
    if not rows:
        return
    target = reflected[table]
    cleaned = [_clean_row(row, target) for row in rows]
    for start in range(0, len(cleaned), _CHUNK):
        conn.execute(sa.insert(target), cleaned[start : start + _CHUNK])


def backfill_connection(
    conn: sa.engine.Connection, row: sa.Row, reflected: dict[str, sa.Table] | None = None
) -> None:
    """Write one connection's blob out as rows. Skips a connection already done."""
    connection_id, blob = row
    if _is_json_null(blob):
        # A deleted model is stored as JSON ``null``, which ``IS NOT NULL`` selects.
        return
    model = _as_dict(blob)
    if model is None:
        logger.warning(
            "connection %s: semantic_model is not a JSON object, skipping backfill",
            connection_id,
        )
        return
    if not model:
        return

    if reflected is None:
        reflected = _reflect(conn)
    models_t = reflected["semantic_models"]
    existing = conn.execute(
        sa.select(models_t.c.id).where(models_t.c.connection_id == connection_id)
    ).scalar()
    if existing is not None:
        return

    now = _now()
    model_id = _uuid()
    conn.execute(
        sa.insert(models_t).values(
            **_clean_row(
                {
                    "id": model_id,
                    "connection_id": connection_id,
                    "created_at": now,
                    "generation_status": model.get("generation_status") or "idle",
                    "generation_progress": model.get("generation_progress"),
                    **_pick(model, _MODEL_FIELDS, _MODEL_DEFAULTS),
                    **_provenance(now),
                },
                models_t,
            )
        )
    )

    tables_t = reflected["semantic_tables"]
    columns_t = reflected["semantic_columns"]
    table_rows: list[dict] = []
    column_rows: list[dict] = []
    seen_table_keys: set[str] = set()
    tables = model.get("tables")
    if not isinstance(tables, dict):
        tables = {}
    for raw_table_key, table in tables.items():
        if not isinstance(table, dict):
            continue
        table_key = _fit_identity(raw_table_key, tables_t.c.table_key)
        if table_key is None:
            # Its columns go with it: they would have no parent row.
            _warn_skipped(connection_id, "table", raw_table_key)
            continue
        if table_key in seen_table_keys:  # two keys that differ only by a NUL
            continue
        seen_table_keys.add(table_key)
        table_id = _uuid()
        table_rows.append(
            {
                "id": table_id,
                "model_id": model_id,
                "connection_id": connection_id,
                "table_key": table_key,
                "generation_run_id": None,
                **_pick(table, _TABLE_FIELDS, _TABLE_DEFAULTS),
                **_provenance(now),
            }
        )
        columns = table.get("columns")
        if not isinstance(columns, dict):
            columns = {}
        seen_column_names: set[str] = set()
        for raw_column_name, column in columns.items():
            if not isinstance(column, dict):
                continue
            column_name = _fit_identity(raw_column_name, columns_t.c.column_name)
            if column_name is None:
                _warn_skipped(connection_id, f"column of {table_key}", raw_column_name)
                continue
            if column_name in seen_column_names:
                continue
            seen_column_names.add(column_name)
            column_rows.append(
                {
                    "id": _uuid(),
                    "table_id": table_id,
                    "column_name": column_name,
                    **_pick(column, _COLUMN_FIELDS, _COLUMN_DEFAULTS),
                    **_provenance(now),
                }
            )
    _insert_chunked(conn, "semantic_tables", table_rows, reflected)
    _insert_chunked(conn, "semantic_columns", column_rows, reflected)

    metrics_t = reflected["semantic_metrics"]
    metric_rows: list[dict] = []
    seen_metric_names: set[str] = set()
    business_metrics = model.get("business_metrics")
    if not isinstance(business_metrics, list):
        business_metrics = []
    for metric in business_metrics:
        if not isinstance(metric, dict):
            continue
        name = _fit_identity(metric.get("name") or "", metrics_t.c.name)
        if name is None:
            _warn_skipped(connection_id, "metric", metric.get("name"))
            continue
        # Unique on (connection_id, name); a legacy blob can carry duplicates.
        # First occurrence wins here (a faithful port of prod 0008), whereas the
        # runtime store's ``save_model`` is last-wins. Deliberate: a legacy blob's
        # first entry is what the old code rendered first.
        if name in seen_metric_names:
            continue
        seen_metric_names.add(name)
        metric_rows.append(
            {
                "id": _uuid(),
                "model_id": model_id,
                "connection_id": connection_id,
                "name": name,
                "metric_type": str(metric.get("metric_type") or "simple"),
                "payload": metric,
                "ordinal": len(metric_rows),
                **_provenance(now),
            }
        )
    _insert_chunked(conn, "semantic_metrics", metric_rows, reflected)

    rels_t = reflected["semantic_relationships"]
    rel_rows: list[dict] = []
    relationships = model.get("relationships")
    if not isinstance(relationships, list):
        relationships = []
    for edge in relationships:
        if not isinstance(edge, dict):
            continue
        endpoints = {
            field: edge.get(field)
            for field in ("from_table", "from_column", "to_table", "to_column")
        }
        # NOT NULL with no server default; an edge missing an endpoint, or with a
        # non-string one, is not usable.
        if not all(isinstance(value, str) and value for value in endpoints.values()):
            continue
        fitted = {
            field: _fit_identity(value, rels_t.c[field]) for field, value in endpoints.items()
        }
        if any(value is None for value in fitted.values()):
            _warn_skipped(
                connection_id,
                "relationship",
                f"{endpoints['from_table']}.{endpoints['from_column']}",
            )
            continue
        rel_rows.append(
            {
                "id": _uuid(),
                "model_id": model_id,
                "connection_id": connection_id,
                **fitted,
                "join_sql": edge.get("join_sql") or "",
                "relationship_type": edge.get("relationship_type") or "many_to_one",
                "is_required": _as_bool(edge.get("is_required", False)),
                "description": edge.get("description"),
                "entity_type": edge.get("entity_type"),
                "ordinal": len(rel_rows),
                **_provenance(now),
            }
        )
    _insert_chunked(conn, "semantic_relationships", rel_rows, reflected)

    global_rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, field in (
        ("segment", "segments"),
        ("derived_column", "derived_columns"),
        ("common_join", "common_joins"),
    ):
        items = model.get(field)
        if not isinstance(items, list):
            items = []
        kind_ordinal = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            # ``name`` is unbounded Text, but the unique index it sits in is not.
            raw_name = (
                item.get("join_pattern") if kind == "common_join" else item.get("name")
            ) or ""
            name = str(raw_name).replace("\x00", "")
            if len(name.encode("utf-8")) > _MAX_INDEXED_NAME_BYTES:
                _warn_skipped(
                    connection_id,
                    kind,
                    raw_name,
                    reason=f"name exceeds the unique index's {_MAX_INDEXED_NAME_BYTES}-byte limit",
                )
                continue
            # First occurrence wins, as for metrics above.
            if (kind, name) in seen:
                continue
            seen.add((kind, name))
            global_rows.append(
                {
                    "id": _uuid(),
                    "model_id": model_id,
                    "connection_id": connection_id,
                    "kind": kind,
                    "name": name,
                    "payload": item,
                    "ordinal": kind_ordinal,
                    **_provenance(now),
                }
            )
            kind_ordinal += 1
    _insert_chunked(conn, "semantic_global_items", global_rows, reflected)


def backfill_all(conn: sa.engine.Connection) -> None:
    """Backfill every active connection that carries a semantic blob.

    A soft-deleted connection (``is_active = false``) is skipped: no route reads or
    deletes its semantic rows, so backfilling it would only leave orphans.
    """
    connections = sa.Table("connections", sa.MetaData(), autoload_with=conn)
    rows = conn.execute(
        sa.select(connections.c.id, connections.c.semantic_model).where(
            connections.c.semantic_model.isnot(None),
            connections.c.is_active.is_(True),
        )
    ).all()
    if not rows:
        return
    reflected = _reflect(conn)
    failed: list[str] = []
    for row in rows:
        connection_id = row[0]
        try:
            # A SAVEPOINT per connection: on PostgreSQL a failed statement poisons
            # the outer transaction, so without this one bad blob would abort every
            # later connection's backfill too.
            with conn.begin_nested():
                backfill_connection(conn, row, reflected)
        except Exception:
            # entrypoint.sh runs ``alembic upgrade head`` under ``set -e`` before
            # uvicorn starts; an unhandled exception here means the container
            # never boots. Log and move on — that connection can be regenerated.
            logger.exception(
                "connection %s: semantic_model backfill failed, skipping", connection_id
            )
            failed.append(str(connection_id))
    summary = "0003 backfill: %d/%d connections backfilled; failed: %s"
    args = (len(rows) - len(failed), len(rows), ", ".join(failed) or "none")
    if failed:
        logger.error(summary, *args)
    else:
        logger.info(summary, *args)


def upgrade() -> None:
    if op.get_context().as_sql:
        # A data migration reads each connection's blob, so it cannot be rendered
        # as static SQL. Skipping it would emit a script that silently omits the
        # backfill.
        raise RuntimeError(
            "0003_semantic_blob_backfill is a data migration and cannot run in "
            "offline (--sql) mode; run `alembic upgrade head` against the database."
        )
    backfill_all(op.get_bind())


def downgrade() -> None:
    # Deliberately a no-op. Nothing writes ``connections.semantic_model`` any more
    # after this migration (``store.delete_model`` only clears it), so the blob is
    # frozen at what it held when this ran;
    # deleting the rows on downgrade would discard every edit made since.
    # Downgrading past 0003 is not supported; roll forward instead.
    pass
