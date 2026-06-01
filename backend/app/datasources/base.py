# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Abstract base class for all data source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from .models import (
    ConnectionResult,
    DataSourceSchema,
    PrivacySettings,
    QueryResult,
    ValidationResult,
)


class BaseDataSource(ABC):
    """Abstract base class for all data source adapters.

    Every adapter must:
    1. Set class-level metadata (source_type, display_name, query_dialect, icon)
    2. Implement all abstract methods
    3. Register itself via @register_datasource decorator
    """

    source_type: str = ""
    display_name: str = ""
    query_dialect: str = ""
    icon: str = ""

    @abstractmethod
    async def connect(self, config: dict) -> ConnectionResult:
        """Establish connection using decrypted config dict."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connection resources."""
        ...

    @abstractmethod
    async def test_connection(self, config: dict) -> ConnectionResult:
        """Test connectivity without persisting a connection."""
        ...

    @abstractmethod
    async def introspect(self, privacy: PrivacySettings | None = None) -> DataSourceSchema:
        """Discover schemas, tables, columns, types, relationships.

        OVERHEAD CONTRACT: All adapter implementations MUST use only catalog/metadata
        tables for introspection. Never run COUNT(*), COUNT(DISTINCT), MIN, MAX, or
        full table scans during introspect(). Sample values must use pg_stats or
        equivalent dialect-specific statistics tables.

        This method is called on-demand (user refreshes schema) and on first connection.
        It must be safe to run against production databases at any time.

        Respects privacy settings: skips excluded tables/columns,
        skips sample values for sensitive columns, etc.
        """
        ...

    @abstractmethod
    async def get_sample_values(
        self,
        schema: str,
        table: str,
        column: str,
        limit: int = 5,
    ) -> list[str]:
        """Retrieve sample distinct values for a column."""
        ...

    @abstractmethod
    async def execute_query(
        self,
        query: str,
        timeout: int = 30,  # noqa: ASYNC109
        max_rows: int = 1000,
    ) -> QueryResult:
        """Execute a read-only query and return standardized results."""
        ...

    async def execute_query_stream(
        self,
        query: str,
        timeout: int = 30,  # noqa: ASYNC109
        batch_size: int = 50,
        max_rows: int = 1000,
    ) -> AsyncGenerator[QueryResult, None]:
        """Stream query results in row batches.

        Default implementation calls execute_query() and slices results into
        batches. Adapters that support true server-side cursor streaming
        (e.g. PostgreSQL via asyncpg) should override this method.
        """
        result = await self.execute_query(query, timeout=timeout, max_rows=max_rows)
        rows = result.rows
        for i in range(0, len(rows), batch_size):
            yield QueryResult(
                columns=result.columns,
                column_types=result.column_types,
                rows=rows[i : i + batch_size],
                row_count=len(rows),
                truncated=result.truncated,
                execution_time_ms=result.execution_time_ms if i == 0 else 0.0,
                bytes_scanned=result.bytes_scanned if i == 0 else None,
            )

    @abstractmethod
    def validate_query(self, query: str) -> ValidationResult:
        """Validate query is safe (read-only, no dangerous patterns)."""
        ...

    def _quote_identifier(self, name: str) -> str:
        """Return a dialect-appropriate quoted identifier. Default: no quoting."""
        return name

    def _schema_section_label(self) -> str:
        """Label used in section headers, e.g. 'Schema' or 'Database'."""
        return "Schema"

    def format_schema_for_llm(
        self,
        schema: DataSourceSchema,
        privacy: PrivacySettings | None = None,
    ) -> str:
        """Format schema as CREATE TABLE DDL for LLM consumption.

        Respects privacy settings:
        - Excludes tables/columns marked as excluded
        - Omits sample values for sensitive columns (shows [SENSITIVE] instead)
        - Omits comments if include_column_comments is False
        - Omits row counts if include_row_counts is False

        Dialect-specific identifier quoting is delegated to _quote_identifier();
        the section label ("Schema" vs "Database") to _schema_section_label().
        """
        if privacy is None:
            privacy = PrivacySettings()

        q = self._quote_identifier
        label = self._schema_section_label()

        fk_index: dict[tuple[str, str, str], str] = {
            (r.from_schema, r.from_table, r.from_column): (
                f"{q(r.to_schema)}.{q(r.to_table)}.{q(r.to_column)}"
            )
            for r in schema.relationships
        }

        parts: list[str] = []
        current_schema: str | None = None

        for table in schema.tables:
            if privacy.is_table_excluded(table.schema_name, table.name):
                continue

            if table.schema_name != current_schema:
                parts.append(f"\n-- {label}: {table.schema_name}")
                current_schema = table.schema_name

            header = f"-- {table.table_type.capitalize()}: {q(table.schema_name)}.{q(table.name)}"
            if table.row_count_approx is not None and privacy.include_row_counts:
                header += f" (≈{table.row_count_approx:,} rows)"
            parts.append(header)
            if table.description and privacy.include_column_comments:
                parts.append(f"-- {table.description}")

            col_lines: list[str] = []
            for col in table.columns:
                if f"{table.schema_name}.{table.name}.{col.name}" in privacy.excluded_columns:
                    continue

                nullable_suffix = "" if col.nullable else " NOT NULL"
                annotations: list[str] = []

                if col.is_primary_key:
                    annotations.append("Primary Key")

                fk_target = fk_index.get((table.schema_name, table.name, col.name))
                if fk_target:
                    annotations.append(f"FK: {q(col.name)} -> {fk_target}")

                if privacy.include_column_comments and col.description:
                    annotations.append(col.description)

                if privacy.is_column_sensitive(col.name):
                    annotations.append("[SENSITIVE]")
                elif col.sample_values and privacy.include_sample_values:
                    sample_str = ", ".join(f"'{v}'" for v in col.sample_values[:3])
                    annotations.append(f"e.g. {sample_str}")

                annotation = f"  -- {', '.join(annotations)}" if annotations else ""
                col_lines.append(f"    {q(col.name)} {col.data_type}{nullable_suffix}{annotation}")

            if col_lines:
                parts.append(
                    f"CREATE TABLE {q(table.schema_name)}.{q(table.name)} (\n"
                    + ",\n".join(col_lines)
                    + "\n);\n"
                )

        return "\n".join(parts)

    @abstractmethod
    def get_system_prompt_additions(self) -> str:
        """Return dialect-specific instructions for the LLM."""
        ...

    @classmethod
    @abstractmethod
    def get_config_schema(cls) -> dict:
        """Return JSON schema describing required connection fields.

        The frontend uses this to dynamically render the connection form.
        Structure: {"fields": [{"name": ..., "type": ..., "label": ..., ...}]}
        """
        ...


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _serialize(value: Any) -> Any:
    """Convert adapter row values to JSON-serialisable Python primitives.

    - Pass through None, int, float, str, bool unchanged.
    - Convert bytes/bytearray to None (binary data cannot be safely JSON-serialised).
    - Stringify everything else (Decimal, datetime, UUID, …).

    Shared by all adapter implementations; previously duplicated in each adapter.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return None  # binary data: omit rather than corrupt
    return str(value)
