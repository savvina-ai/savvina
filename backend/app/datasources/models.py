# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from dataclasses import dataclass, field, fields
import re
from typing import Any, Literal


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
    # Where sample_values came from: "stats" (catalog statistics over the rows, PostgreSQL
    # pg_stats) or "type" (the column's declared ENUM/SET members on MySQL). None when there
    # are no sample values, or on a cache written before the field existed; readers treat
    # None as "stats".
    sample_source: Literal["stats", "type"] | None = None


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
class PrivilegeGrant:
    """One privilege the connection role holds, as the catalog stores it: the scope, the
    privilege names and the grantee a ``REVOKE`` must name. Built by the adapters, turned
    into remediation lines by ``services/privilege_audit``."""

    schema_name: str | None  # None: global (MySQL ``*.*``); never None on PostgreSQL
    table_name: str | None  # None: the whole schema (MySQL ``db.*``)
    privileges: list[str]  # upper-case, sorted: ["INSERT", "UPDATE"]; or ["ALL PRIVILEGES"]
    # None: ownership (PostgreSQL: the owner or a role inheriting it); "PUBLIC"; a PostgreSQL
    # role name; a MySQL account or role as "u@h" (the ``CURRENT_USER()`` form); a MariaDB
    # role bare ("reader": its ``CURRENT_ROLE()`` prints no host).
    grantee: str | None
    columns: list[str] = field(default_factory=list)  # a column-level grant; [] = table level

    @property
    def key(self) -> str | None:
        """``schema.table`` for a table- or column-level grant, else None."""
        if self.schema_name is None or self.table_name is None:
            return None
        return f"{self.schema_name}.{self.table_name}"


@dataclass
class TablePrivilegeFacts:
    """What the catalog says about the connection role and one table. Built by the adapters
    (``BaseDataSource.audit_privileges``), judged by ``services/privilege_audit``.

    ``schema_name`` is ``None`` only for a bare ``excluded_tables`` entry that exists in no
    schema. The PostgreSQL-only flags (``is_owner``, ``rls_*``, ``select_policy_applies``)
    are ``False`` on MySQL.
    """

    schema_name: str | None
    table_name: str
    # MySQL: visible to the account (information_schema.tables lists only the tables it
    # has some privilege on), so a table it cannot read at all reads False.
    exists: bool
    # PostgreSQL: SELECT on the table and USAGE on its schema. MySQL: SELECT (or ALL) granted
    # at the global, schema or table level.
    can_select: bool = False
    # The table-level write flag the adapters report: any of INSERT, UPDATE, DELETE (MySQL:
    # any privilege of the MySQL adapter's write set, ``_WRITE_PRIVILEGES``, or ALL, from a
    # global, schema, table or column grant). True also when the table has an entry in
    # ``PrivilegeFacts.writes``. ``services/privilege_audit`` does not read
    # it: WRITE_PRIVILEGES comes from ``PrivilegeFacts.writes``.
    can_write: bool = False
    # PostgreSQL: the owner, or a role whose privileges it inherits; both bypass RLS
    # unless the table forces it (the engine's own rule, has_privs_of_role).
    is_owner: bool = False
    rls_enabled: bool = False  # relrowsecurity
    rls_forced: bool = False  # relforcerowsecurity
    # A policy with cmd SELECT or ALL whose roles include the role, a role whose privileges
    # it inherits (pg_has_role ... 'USAGE'), or PUBLIC.
    select_policy_applies: bool = False
    # The grants through which the role holds SELECT on the table, each at the scope it is
    # granted at, with the grantee a REVOKE must name (PrivilegeGrant.grantee's encoding).
    # PostgreSQL: the table itself, grantee None for ownership. MySQL: ``*.*`` (schema None),
    # the schema literal as the grant prints it (table None, escapes kept: ``sample\_x``),
    # or the table. Privileges ``["SELECT"]``. [] when can_select is False.
    select_grants: list[PrivilegeGrant] = field(default_factory=list)

    @property
    def key(self) -> str | None:
        """``schema.table``, the row-filter and finding key (the encoding of ``table_key``);
        ``None`` without a schema."""
        return None if self.schema_name is None else f"{self.schema_name}.{self.table_name}"


_GRANT_PATTERN_ESCAPE = re.compile(r"\\([_%\\])")


def grant_schema_name(literal: str) -> str:
    """The schema name a MySQL schema-level grant matches, from the name as the grant is
    written: ``\\_`` -> ``_``, ``\\%`` -> ``%``, ``\\\\`` -> ``\\``, in one pass.

    ``SHOW GRANTS`` and ``mysql.db`` keep the pattern escapes of a schema-level grant
    (``sample\\_delivery``): an unescaped ``_`` or ``%`` there is a wildcard, which the audit
    does not expand. A REVOKE must name the grant as stored; matching a grant to a table
    needs the name it matches.
    """
    return _GRANT_PATTERN_ESCAPE.sub(r"\1", literal)


@dataclass
class RoleMembership:
    """A PostgreSQL predefined role whose privileges the connection role holds
    (``pg_read_all_data``, ``pg_write_all_data``) and the role the membership is granted to:
    the connection role itself or a role it inherits. A ``REVOKE <role> FROM <member>``
    removes it; ``GRANTED BY <grantor>`` is added when the membership was granted by a role
    other than the bootstrap superuser (a plain REVOKE then only raises a NOTICE). Built by
    the PostgreSQL adapter; never on MySQL."""

    role: str
    member: str
    # The role that granted the membership; None for the bootstrap superuser (oid 10), the
    # grantor a plain REVOKE run by a superuser matches.
    grantor: str | None = None


@dataclass
class PrivilegeFacts:
    """The connection role's standing as the catalog reports it. Facts, not findings.

    ``is_superuser`` on MySQL means global ``ALL PRIVILEGES``, or global ``SUPER`` plus every
    privilege of the MySQL write set (MySQL 8 prints ``GRANT ALL ON *.*`` as the expanded
    list); global ``SUPER`` without that is ``has_super``.
    """

    role: str  # current_user / CURRENT_USER() ("user@host")
    # rolsuper; MySQL: global ALL PRIVILEGES, or global SUPER plus every write privilege.
    is_superuser: bool = False
    bypasses_rls: bool = False  # rolbypassrls; always False on MySQL
    # MySQL: global SUPER when is_superuser is False (no global ALL PRIVILEGES and not every
    # write privilege). Always False on PostgreSQL.
    has_super: bool = False
    # MySQL, when has_super: the grantees ("u@h", or a MariaDB role bare) that hold SUPER at
    # the global level, the account or an active role; each needs its own REVOKE. [] otherwise.
    super_grantees: list[str] = field(default_factory=list)
    # Every write grant the role holds, at every level and through every grantee, capped at
    # 500 by the adapter (writes_truncated says the cap was hit). PostgreSQL: every
    # non-system table, table- and column-level, plus one ownership entry per owned table.
    # MySQL: global, schema, table and column level, from the account and its active roles.
    writes: list[PrivilegeGrant] = field(default_factory=list)
    writes_truncated: bool = False
    # PostgreSQL only: memberships in pg_read_all_data / pg_write_all_data, which give SELECT
    # / INSERT, UPDATE, DELETE on every table without an ACL entry. Only memberships whose
    # privileges the role actually inherits. [] for a superuser and on MySQL.
    predefined_roles: list[RoleMembership] = field(default_factory=list)
    # The non-system schemas (PostgreSQL) or databases (MySQL); input of the dedicated-role
    # script.
    schemas: list[str] = field(default_factory=list)
    # PostgreSQL: the functions of the deny list outside pg_catalog that the role can
    # EXECUTE, as "schema.name(argtypes)". [] on MySQL.
    sql_from_string_functions: list[str] = field(default_factory=list)
    tables: list[TablePrivilegeFacts] = field(default_factory=list)


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
    # Per-table row filters: {"schema.table": "<condition without WHERE>"}, keyed by the
    # names as introspection reports them. Validated on write
    # (services/row_filter.py: validate_row_filters) and applied by
    # services/row_filter_rewrite.apply_row_filters, which replaces each listed table
    # reference with a filtered derived table. Tables not listed are unfiltered.
    row_filters: dict[str, str] = field(default_factory=dict)
    # Legacy (before per-table filters): the whole-query condition. Read-only; nothing
    # writes it any more. A connection that still has one and no row_filters is in the
    # migration state (row_filter_needs_migration) and runs nothing until the admin acts.
    row_filter_sql: str | None = None

    @property
    def row_filter_needs_migration(self) -> bool:
        """The single definition of the migration state: a legacy whole-query filter is
        stored and the per-table map is empty. A stored ``{}`` next to a legacy value is
        still the migration state; the connections router never stores that combination
        (it refuses the empty map or removes the legacy value)."""
        return bool(self.row_filter_sql) and not self.row_filters

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
