# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Schema serialization, SQL table extraction, embedding encoding, and query validation helpers."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
import sqlparse
from sqlparse.exceptions import SQLParseError
import sqlparse.sql
import sqlparse.tokens

from ..datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
    table_key,
)
from ..datasources.registry import create_datasource
from ..datasources.sampling import withhold_enum_labels
from ..models.cache import QueryCacheEntry
from ..models.connection import Connection
from ..models.table_embedding_cache import TableEmbeddingCache
from ..models.user_schema_cache import UserSchemaCache
from ..semantic.generator import SemanticModelGenerator, policy_hides_table
from ..utils.encryption import decrypt_value

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

LARGE_TABLE_ROW_THRESHOLD = 1_000_000


async def get_or_refresh_schema(
    conn: Connection,
    user_id: str,
    db: AsyncSession,
    encryption_key: str,
) -> DataSourceSchema:
    """Return the cached schema for this user+connection, or introspect live and cache it.

    Used when no pre-existing adapter session is available (e.g. semantic model generation).
    For the chat pipeline path use ``_resolve_schema`` in ``schema_pruning`` instead, which
    reuses the adapter connection already established for query execution.

    A miss is ``refresh_user_schema_cache`` with ``flush="never"``, committed here. It
    raises ``SchemaRefreshError`` when the source cannot be reached (nothing written).
    """
    usc = (
        await db.execute(
            select(UserSchemaCache).where(
                UserSchemaCache.connection_id == conn.id,
                UserSchemaCache.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if usc and usc.schema_cache:
        schema = _schema_from_dict(usc.schema_cache)
    else:
        # No flush: there is nothing stale to flush. A user's row is missing either
        # because they never loaded this connection — and the chat path writes their
        # table embeddings only after writing that row — or because
        # ``_invalidate_connection_caches`` deleted it, which deleted the connection's
        # embeddings and cached answers in the same transaction.
        schema = await refresh_user_schema_cache(conn, user_id, db, encryption_key, flush="never")
        await db.commit()

    privacy = (
        PrivacySettings.from_dict(conn.privacy_settings)
        if conn.privacy_settings
        else PrivacySettings()
    )
    schema = _apply_privacy_to_schema(schema, privacy)
    return schema


class SchemaRefreshError(Exception):
    """The data source could not be connected to or introspected."""


# When ``refresh_user_schema_cache`` deletes the caches built from the schema (table
# embeddings, cached query answers): "changed" when the stored hash differs (or there
# was no row), "always" for the explicit Refresh, "never" for a first load.
SchemaCacheFlush = Literal["changed", "always", "never"]


async def refresh_user_schema_cache(
    conn: Connection,
    user_id: str,
    db: AsyncSession,
    encryption_key: str,
    *,
    flush: SchemaCacheFlush = "changed",
) -> DataSourceSchema:
    """Introspect ``conn`` live and store the result as this user's schema cache.

    Returns the schema as introspected (privacy applied at introspection, not
    ``_apply_privacy_to_schema``). Table embeddings and cached query answers are
    built from the schema, so they are flushed per ``flush`` (``SchemaCacheFlush``).
    Raises ``SchemaRefreshError`` when the source cannot be reached; nothing is
    written then. The caller commits.
    """
    config_dict = json.loads(decrypt_value(conn.config_encrypted, encryption_key))
    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    adapter = create_datasource(conn.source_type)
    try:
        result = await adapter.connect(config_dict)
        if result is not None and result.success is False:
            raise SchemaRefreshError(result.message)
        schema = await adapter.introspect(privacy)
    except SchemaRefreshError:
        raise
    except Exception as exc:
        raise SchemaRefreshError(str(exc) or repr(exc)) from exc
    finally:
        await adapter.disconnect()

    schema_hash = SemanticModelGenerator().compute_schema_hash(schema)
    changed = await _store_user_schema_cache(db, conn.id, user_id, schema, schema_hash)
    # Schema cache, then embeddings, then cached answers: the order
    # ``_invalidate_connection_caches`` deletes in too, because two transactions that
    # lock the same rows in opposite orders can deadlock. The schema-cache write comes
    # first even when it is pending in the session: the next execute autoflushes it.
    if flush == "always" or (flush == "changed" and changed):
        await db.execute(
            delete(TableEmbeddingCache).where(TableEmbeddingCache.connection_id == conn.id)
        )
        await db.execute(delete(QueryCacheEntry).where(QueryCacheEntry.connection_id == conn.id))
    return schema


async def _store_user_schema_cache(
    db: AsyncSession,
    connection_id: str,
    user_id: str,
    schema: DataSourceSchema,
    schema_hash: str,
) -> bool:
    """Upsert the user's schema cache row; whether its hash changed (a new row has).

    Select, then insert or update. Two first refreshes for one user and connection
    (two tabs running Check Drift) both find no row and both insert; the second
    insert violates ``UniqueConstraint(connection_id, user_id)``. The insert runs in
    a savepoint so that conflict rolls back only the insert, and the loser updates
    the winner's row instead of failing the request with a 500.
    """
    lookup = select(UserSchemaCache).where(
        UserSchemaCache.connection_id == connection_id,
        UserSchemaCache.user_id == user_id,
    )
    now = datetime.now(UTC)
    usc = (await db.execute(lookup)).scalar_one_or_none()
    if usc is None:
        try:
            async with db.begin_nested():
                db.add(
                    UserSchemaCache(
                        connection_id=connection_id,
                        user_id=user_id,
                        schema_cache=_schema_to_dict(schema),
                        schema_cached_at=now,
                        schema_hash=schema_hash,
                        created_at=now,
                        updated_at=now,
                    )
                )
            return True
        except IntegrityError:
            usc = (await db.execute(lookup)).scalar_one_or_none()
            if usc is None:
                raise
    changed = usc.schema_hash != schema_hash
    usc.schema_cache = _schema_to_dict(schema)
    usc.schema_cached_at = now
    usc.schema_hash = schema_hash
    usc.updated_at = now
    return changed


def _extract_tables_from_sql(sql: str) -> list[str]:
    """Extract referenced table names from a SQL string using sqlparse."""
    tables: list[str] = []
    try:
        parsed = sqlparse.parse(sql)
    except SQLParseError as e:
        # sqlparse >= 0.5.5 raises when its DoS guards trip. Both callers are advisory
        # (audit `tables_accessed`, the large-table full-scan check) and run on a query
        # `validate_query()` already accepted, so degrade to "no tables known" rather
        # than failing the turn.
        logger.warning("Table extraction skipped, SQL could not be parsed: %s", e)
        return []
    for statement in parsed:
        _collect_tables(statement, tables)
    seen: set[str] = set()
    result: list[str] = []
    for t in tables:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)
    return result


def _collect_tables(token: sqlparse.sql.TokenList, tables: list[str]) -> None:
    """Recursively walk a parsed SQL token tree collecting table names."""
    _kw = sqlparse.tokens.Keyword
    _ws = sqlparse.tokens.Text.Whitespace
    _nl = sqlparse.tokens.Newline
    _pu = sqlparse.tokens.Punctuation
    from_seen = False
    join_seen = False
    for item in token.tokens:
        if item.ttype is _kw and item.normalized.upper() in ("FROM", "INTO", "UPDATE"):
            from_seen = True
            join_seen = False
        elif item.ttype is _kw and "JOIN" in item.normalized.upper():
            join_seen = True
            from_seen = False
        elif from_seen or join_seen:
            if item.ttype is _ws:
                continue
            if isinstance(item, sqlparse.sql.Identifier):
                tables.append(item.get_real_name() or "")
                from_seen = False
                join_seen = False
            elif isinstance(item, sqlparse.sql.IdentifierList):
                for identifier in item.get_identifiers():
                    if isinstance(identifier, sqlparse.sql.Identifier):
                        tables.append(identifier.get_real_name() or "")
                from_seen = False
                join_seen = False
            elif item.ttype not in (_ws, _nl, _pu):
                from_seen = False
                join_seen = False
        if isinstance(item, sqlparse.sql.TokenList):
            _collect_tables(item, tables)


# ── Serialization helpers ──────────────────────────────────────────────────────


def _schema_to_dict(schema: DataSourceSchema) -> dict:
    """Serialize a DataSourceSchema to a JSON-safe dict for DB storage."""
    return asdict(schema)


def _schema_from_dict(d: dict) -> DataSourceSchema:
    """Reconstruct a DataSourceSchema from a stored dict."""
    return DataSourceSchema(
        source_type=d["source_type"],
        schemas=[SchemaInfo(**s) for s in d.get("schemas", [])],
        tables=[
            TableInfo(
                catalog=t.get("catalog"),
                schema_name=t["schema_name"],
                name=t["name"],
                table_type=t["table_type"],
                columns=[ColumnInfo(**c) for c in t.get("columns", [])],
                row_count_approx=t.get("row_count_approx"),
                description=t.get("description"),
            )
            for t in d.get("tables", [])
        ],
        relationships=[RelationshipInfo(**r) for r in d.get("relationships", [])],
        metadata=d.get("metadata", {}),
        unreadable_tables=list(d.get("unreadable_tables", [])),
    )


def _withhold_column_values(table: TableInfo, privacy: PrivacySettings) -> TableInfo:
    """Drop sample values and ENUM/SET member lists from columns whose values may not reach the LLM.

    The column itself stays — the sensitive patterns mark a column, they do not hide it.
    Runs on every cached read, so a schema cached under older settings, or before this
    rule existed, needs no migration. Returns a new TableInfo; the cached one is untouched.
    """
    columns = [
        col
        if privacy.column_values_allowed(col.name)
        else replace(col, sample_values=None, native_type=withhold_enum_labels(col.native_type))
        for col in table.columns
    ]
    return replace(table, columns=columns)


def _apply_privacy_to_schema(
    schema: DataSourceSchema, privacy: PrivacySettings
) -> DataSourceSchema:
    """Return a copy of schema with excluded schemas, tables, columns and relationships removed.

    Ensures _validate_columns_against_schema enforces privacy settings,
    not just format_schema_for_llm. Relationships are filtered so that
    FK annotations cannot reveal excluded table or column names to the LLM.

    Columns ``PrivacySettings.hides_column`` names (the adapters' rule at
    introspection) are removed as well: introspection already
    omits them, but a schema cache written before a privacy change still carries
    them, and every consumer of this function — chat validation and pruning,
    drift, generation — must not see one. The sensitive-name patterns are
    deliberately not applied here: the adapters still introspect those columns.
    Their *values* are withheld, though: sample values and ENUM/SET member lists survive only
    on columns ``column_values_allowed`` admits.
    """
    visible = {
        (t.schema_name, t.name)
        for t in schema.tables
        if not privacy.is_table_excluded(t.schema_name, t.name)
    }
    return DataSourceSchema(
        source_type=schema.source_type,
        schemas=[s for s in schema.schemas if s.name not in privacy.excluded_schemas],
        tables=[
            _withhold_column_values(
                replace(
                    t,
                    columns=[
                        c for c in t.columns if not privacy.hides_column(table_key(t), c.name)
                    ],
                ),
                privacy,
            )
            for t in schema.tables
            if (t.schema_name, t.name) in visible
        ],
        relationships=[
            r
            for r in schema.relationships
            if (r.from_schema, r.from_table) in visible
            and (r.to_schema, r.to_table) in visible
            and not privacy.hides_column(f"{r.from_schema}.{r.from_table}", r.from_column)
            and not privacy.hides_column(f"{r.to_schema}.{r.to_table}", r.to_column)
        ],
        metadata=schema.metadata,
        # Introspection leaves excluded tables out of this list, but a cache written
        # before a privacy change can still name one.
        unreadable_tables=[
            key for key in schema.unreadable_tables if not policy_hides_table(key, privacy)
        ],
    )


def _is_fallback_query(query: str) -> bool:
    """Return True when the LLM generated a string-literal SELECT instead of a real query.

    Detects two patterns: a leading comment declaring the schema lacks the requested data,
    or a SELECT with no FROM clause referencing an actual table.
    """
    stripped = query.strip()
    # Pattern 1: leading comment with schema-unavailability language
    if re.match(
        r"^\s*--[^\n]*(?:schema|database)[^\n]*(?:not|does not|NOT)",
        stripped,
        re.IGNORECASE,
    ):
        return True
    # Pattern 2: SELECT with no real table reference
    no_strings = re.sub(r"'[^']*'", "''", stripped)
    return bool(
        re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE)
        and not re.search(r"\bFROM\s+[\[a-z_`\"]", no_strings, re.IGNORECASE)
    )


# SQL reserved words and pseudo-functions that can appear in FROM/JOIN position
# but are not real schema table names.
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "where",
        "on",
        "set",
        "inner",
        "outer",
        "left",
        "right",
        "full",
        "cross",
        "natural",
        "using",
        "group",
        "order",
        "having",
        "limit",
        "offset",
        "union",
        "except",
        "intersect",
        "select",
        "from",
        "join",
        "with",
        "as",
        "and",
        "or",
        "not",
        "lateral",
        # PostgreSQL date/time pseudo-functions (appear after FROM in EXTRACT)
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "current_schema",
        "current_catalog",
        "localtime",
        "localtimestamp",
        # Table-valued functions and pseudo-schemas
        "unnest",
        "generate_series",
        "generate_subscripts",
        "json_each",
        "json_each_text",
        "json_array_elements",
        "json_array_elements_text",
        "jsonb_each",
        "jsonb_each_text",
        "jsonb_array_elements",
        "jsonb_array_elements_text",
        "json_table",
        "string_to_table",
        "regexp_split_to_table",
        "information_schema",
        "pg_catalog",
    }
)


def _extract_cte_names(query: str) -> set[str]:
    """Return the set of CTE alias names defined by WITH ... AS ( clauses."""
    return {
        m.group(1).lower()
        for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\s+AS\s*\(", query, re.IGNORECASE)
    }


def _build_alias_map(
    query: str,
    table_columns: dict[str, set[str]],
    cte_names: set[str],
) -> tuple[dict[str, str], str | None]:
    """Check FROM/JOIN table existence and build alias→table_key map.

    Returns (alias_map, error_string_or_None).  The error is non-None when at
    least one table referenced in FROM/JOIN is absent from the schema.
    """
    alias_map: dict[str, str] = {}
    bad: list[str] = []

    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_.]*)\b",
        query,
        re.IGNORECASE,
    ):
        table_ref = m.group(1).lower()
        bare = table_ref.rpartition(".")[2]

        if bare in cte_names or table_ref in cte_names:
            continue
        if bare in _SQL_KEYWORDS:
            continue
        # Any identifier immediately followed by ( is a table-valued function call.
        if query[m.end() :].lstrip().startswith("("):
            continue

        if table_ref not in table_columns and bare not in table_columns:
            bad.append(f'table "{bare}" does not exist in schema')
            continue

        key = table_ref if table_ref in table_columns else bare
        alias_map[bare] = key

    if bad:
        return {}, "Query references tables not found in schema: " + "; ".join(dict.fromkeys(bad))

    # Re-parse to capture explicit aliases (FROM table AS alias / FROM table alias)
    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_.]*)\s+(?:AS\s+)?([a-z_][a-z0-9_]*)\b",
        query,
        re.IGNORECASE,
    ):
        table_ref = m.group(1).lower()
        alias = m.group(2).lower()
        if alias in _SQL_KEYWORDS:
            continue
        bare = table_ref.rpartition(".")[2]
        if bare in cte_names or table_ref in cte_names:
            continue
        key = table_ref if table_ref in table_columns else bare
        if key in table_columns:
            alias_map[alias] = key

    return alias_map, None


def _check_column_references(
    query: str,
    alias_map: dict[str, str],
    table_columns: dict[str, set[str]],
) -> str | None:
    """Validate all prefix.column references against the alias map and schema columns.

    Returns an error string listing every missing column, or None if all references
    are valid (or unresolvable aliases that should be skipped).
    """
    bad: list[str] = []
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", query, re.IGNORECASE):
        prefix = m.group(1).lower()
        column = m.group(2).lower()
        resolved = alias_map.get(prefix)
        if resolved and resolved in table_columns and column not in table_columns[resolved]:
            available = ", ".join(sorted(table_columns[resolved]))
            bad.append(
                f'column "{m.group(2)}" does not exist in table'
                f' "{resolved.rpartition(".")[2]}" (available: {available})'
            )
    if bad:
        return "Query references columns not found in schema: " + "; ".join(dict.fromkeys(bad))
    return None


def _validate_columns_against_schema(
    query: str,
    schema: DataSourceSchema,
) -> str | None:
    """Cross-reference table and column references in the query against stored schema.

    1. Extracts CTE names to exclude them from table-existence checks.
    2. Checks every table in FROM/JOIN clauses exists in the schema (missing tables).
    3. Builds alias→table map then checks alias.column references (missing columns).
    CTE names and subquery aliases are skipped to avoid false positives.
    Returns an error string on first problem found, None if the query looks valid.
    """
    # Build lookup: "table_name" and "schema.table_name" → set of column names.
    # schema_name is optional — some adapters expose tables without a namespace.
    table_columns: dict[str, set[str]] = {}
    for t in schema.tables:
        tname = t.name.lower()
        cols = {c.name.lower() for c in t.columns}
        table_columns[tname] = cols
        if t.schema_name:
            table_columns[f"{t.schema_name.lower()}.{tname}"] = cols

    if not table_columns:
        return None

    # Pre-process: strip string literals and SQL functions that embed FROM keywords
    # so their content cannot be mistaken for table/column references.
    query = re.sub(r"'[^']*'", "''", query)
    query = re.sub(
        r"\b(?:EXTRACT|TRIM|SUBSTRING|OVERLAY)\s*\([^)]*\)",
        "FUNC()",
        query,
        flags=re.IGNORECASE,
    )

    cte_names = _extract_cte_names(query)
    alias_map, err = _build_alias_map(query, table_columns, cte_names)
    if err:
        return err
    return _check_column_references(query, alias_map, table_columns)


def _check_query_complexity(sql: str, schema: DataSourceSchema | None = None) -> str | None:
    """Return an error string if the query violates complexity limits, else None.

    Checks:
    - CROSS JOIN (always): produces Cartesian products, almost never intentional in NL-to-SQL.
    - Large-table full-scan (when schema available): a table with > 1M rows and no WHERE
      clause would scan the entire table despite the LIMIT guard on the result set.
    """
    if re.search(r"\bCROSS\s+JOIN\b", sql, re.IGNORECASE):
        return (
            "Query contains a CROSS JOIN which can produce an extremely large "
            "intermediate result set. Rewrite using an explicit JOIN condition."
        )

    if schema is not None and not re.search(r"\bWHERE\b", sql, re.IGNORECASE):
        referenced = {t.lower() for t in _extract_tables_from_sql(sql)}
        for table in schema.tables:
            if (
                table.name.lower() in referenced
                and table.row_count_approx is not None
                and table.row_count_approx > LARGE_TABLE_ROW_THRESHOLD
            ):
                return (
                    f"Query scans '{table.name}' (≈{table.row_count_approx:,} rows) "
                    "without a WHERE clause. Add a filter to reduce the scanned range."
                )
    return None
