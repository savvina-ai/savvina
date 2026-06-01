# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Schema serialization, SQL table extraction, embedding encoding, and query validation helpers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import re
from typing import TYPE_CHECKING

import sqlparse
import sqlparse.sql
import sqlparse.tokens

from ..datasources.models import (
    ColumnInfo,
    DataSourceSchema,
    PrivacySettings,
    RelationshipInfo,
    SchemaInfo,
    TableInfo,
)
from ..datasources.registry import create_datasource
from ..models.connection import Connection
from ..models.user_schema_cache import UserSchemaCache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    """
    from sqlalchemy import select  # local to avoid circular at module level

    usc_result = await db.execute(
        select(UserSchemaCache).where(
            UserSchemaCache.connection_id == conn.id,
            UserSchemaCache.user_id == user_id,
        )
    )
    usc = usc_result.scalar_one_or_none()

    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None

    if usc and usc.schema_cache:
        schema = _schema_from_dict(usc.schema_cache)
    else:
        from ..utils.encryption import decrypt_value

        config_dict = json.loads(decrypt_value(conn.config_encrypted, encryption_key))
        adapter = create_datasource(conn.source_type)
        try:
            await adapter.connect(config_dict)
            schema = await adapter.introspect(privacy)
        finally:
            await adapter.disconnect()

        now = datetime.now(UTC)
        if usc is None:
            db.add(
                UserSchemaCache(
                    connection_id=conn.id,
                    user_id=user_id,
                    schema_cache=_schema_to_dict(schema),
                    schema_cached_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            usc.schema_cache = _schema_to_dict(schema)
            usc.schema_cached_at = now
            usc.updated_at = now
        await db.commit()

    if privacy:
        schema = _apply_privacy_to_schema(schema, privacy)
    return schema


def _extract_tables_from_sql(sql: str) -> list[str]:
    """Extract referenced table names from a SQL string using sqlparse."""
    tables: list[str] = []
    parsed = sqlparse.parse(sql)
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
    )


def _apply_privacy_to_schema(
    schema: DataSourceSchema, privacy: PrivacySettings
) -> DataSourceSchema:
    """Return a copy of schema with excluded schemas/tables/relationships removed.

    Ensures _validate_columns_against_schema enforces privacy settings,
    not just format_schema_for_llm. Relationships are filtered so that
    FK annotations cannot reveal excluded table names to the LLM.
    """
    visible = {
        (t.schema_name, t.name)
        for t in schema.tables
        if not privacy.is_table_excluded(t.schema_name, t.name)
    }
    return DataSourceSchema(
        source_type=schema.source_type,
        schemas=[s for s in schema.schemas if s.name not in privacy.excluded_schemas],
        tables=[t for t in schema.tables if not privacy.is_table_excluded(t.schema_name, t.name)],
        relationships=[
            r
            for r in schema.relationships
            if (r.from_schema, r.from_table) in visible and (r.to_schema, r.to_table) in visible
        ],
        metadata=schema.metadata,
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
        table_key = alias_map.get(prefix)
        if table_key and table_key in table_columns and column not in table_columns[table_key]:
            available = ", ".join(sorted(table_columns[table_key]))
            bad.append(
                f'column "{m.group(2)}" does not exist in table'
                f' "{table_key.rpartition(".")[2]}" (available: {available})'
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
