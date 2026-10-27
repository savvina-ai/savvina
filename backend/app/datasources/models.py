# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class ColumnInfo:
    name: str
    data_type: str  # Normalized type name
    native_type: str  # Original type from source
    nullable: bool = True
    is_primary_key: bool = False
    is_partition_key: bool = False
    description: str | None = None
    sample_values: list[str] | None = None


@dataclass
class RelationshipInfo:
    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str
    relationship_type: str = "foreign_key"


@dataclass
class TableInfo:
    catalog: str | None
    schema_name: str
    name: str
    table_type: str  # 'table', 'view', 'external', 'materialized_view'
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_approx: int | None = None
    description: str | None = None
    metadata: dict = field(default_factory=dict)  # adapter-specific extras (e.g. view_definition)


def table_key(table: TableInfo) -> str:
    """The ``schema.name`` key the semantic store, drift check and generation use for a table.

    One encoding, defined once. A schema or table name that itself contains a dot
    makes the key ambiguous to split; the readers that must split one
    (``semantic.drift.policy_hides_table``) try every split rather than guess.
    """
    return f"{table.schema_name}.{table.name}"


@dataclass
class SchemaInfo:
    name: str
    description: str | None = None


@dataclass
class DataSourceSchema:
    source_type: str
    schemas: list[SchemaInfo] = field(default_factory=list)
    tables: list[TableInfo] = field(default_factory=list)
    relationships: list[RelationshipInfo] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # ``table_key`` of each table that exists in the database but that the connection's
    # role cannot see (no privilege on it, or no USAGE on its schema), so it is absent
    # from ``tables``. Unreadable is not dropped: whatever persists the semantic model
    # keeps such a table, and drift does not report it as removed. Empty for an adapter
    # that cannot tell the two apart, and for a schema cache written before this field.
    unreadable_tables: list[str] = field(default_factory=list)


@dataclass
class QueryResult:
    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    execution_time_ms: float = 0.0
    bytes_scanned: int | None = None
    query_id: str | None = None


@dataclass
class ConnectionResult:
    success: bool
    message: str
    server_version: str | None = None


@dataclass
class ValidationResult:
    is_valid: bool
    error_message: str | None = None
    sanitized_query: str | None = None


@dataclass
class PrivacySettings:
    """Per-connection privacy controls for what metadata reaches the LLM."""

    # Off until an admin turns it on. A stored settings dict that lacks the key reads as
    # off too (from_dict falls back to this default) — the UI has always shown it off.
    include_sample_values: bool = False
    include_column_comments: bool = False
    include_row_counts: bool = True
    sensitive_column_patterns: list[str] = field(
        default_factory=lambda: [
            "email",
            "ssn",
            "social_security",
            "password",
            "passwd",
            "secret",
            "token",
            "api_key",
            "credit_card",
            "card_number",
            "cvv",
            "phone",
            "mobile",
            "address",
            "salary",
            "wage",
            "income",
            "bank_account",
            "routing_number",
            "dob",
            "date_of_birth",
            "national_id",
            "passport",
            "license_number",
            "tax_id",
        ]
    )
    excluded_schemas: list[str] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
    excluded_columns: list[str] = field(default_factory=list)  # format: "schema.table.column"
    # format: "schema.table" or bare "table" — same syntax as excluded_tables
    always_include_tables: list[str] = field(default_factory=list)
    schema_pruning_threshold: float = 0.30  # per-connection cosine threshold for table relevance
    # Optional SQL condition every executed statement is wrapped in as a mandatory row-level
    # filter, e.g. "tenant_id = 'acme-corp'". Validated on write (services/row_filter.py:
    # validate_row_filter) and applied by _inject_row_filter.
    row_filter_sql: str | None = None

    def is_column_sensitive(self, column_name: str) -> bool:
        """Check if a column name matches any sensitive pattern."""
        col_lower = column_name.lower()
        return any(pattern in col_lower for pattern in self.sensitive_column_patterns)

    def column_values_allowed(self, column_name: str) -> bool:
        """Whether a real value from *column_name* may reach the LLM.

        The single rule every value channel reads — sample values, ENUM/SET member lists,
        entity-resolution hints, catalog statistics: the toggle must be on and the column
        must not match a sensitive pattern.
        """
        return self.include_sample_values and not self.is_column_sensitive(column_name)

    def is_table_excluded(self, schema_name: str | None, table_name: str) -> bool:
        """Check if a table should be excluded from LLM context."""
        full_name = f"{schema_name}.{table_name}" if schema_name else table_name
        return (
            (schema_name is not None and schema_name in self.excluded_schemas)
            or table_name in self.excluded_tables
            or full_name in self.excluded_tables
        )

    def is_column_excluded(
        self, schema_name: str | None, table_name: str, column_name: str
    ) -> bool:
        """Check if a column should be excluded from LLM context."""
        full_name = (
            f"{schema_name}.{table_name}.{column_name}"
            if schema_name
            else f"{table_name}.{column_name}"
        )
        return full_name in self.excluded_columns or self.is_column_sensitive(column_name)

    def hides_column(self, table_key: str, column_name: str) -> bool:
        """Exact ``schema.table.column`` membership in ``excluded_columns``.

        The rule the adapters apply at introspection and ``_apply_privacy_to_schema``
        applies to a cached schema. Unlike ``is_column_excluded`` it ignores the
        sensitive-name patterns, which the adapters still introspect.
        """
        return f"{table_key}.{column_name}" in self.excluded_columns

    @classmethod
    def from_dict(cls, data: dict) -> "PrivacySettings":
        """Reconstruct a PrivacySettings instance from a stored JSON dict.

        Unknown keys are silently ignored so stored settings survive future
        additions to the dataclass without raising TypeError.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
