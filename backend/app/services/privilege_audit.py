# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The privilege audit of a connection role: catalog facts in, findings out.

The adapters read (``BaseDataSource.audit_privileges`` -> ``PrivilegeFacts``) and do not
judge. This module turns the facts into findings, each with the SQL that fixes it, written
against the grantee the privilege comes from, and computes ``rls_effective`` per
row-filtered table, the input of the fence removal (Phase 3b-2). Pure functions, no I/O:
every finding is testable without a database.

The result is stored on the connection (``connections.privilege_audit``) through
``to_dict`` and read back with ``from_dict``, which drops what it does not know so a later
version can add a finding code without a migration.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import re
from typing import Literal

from ..datasources.models import (
    PrivacySettings,
    PrivilegeFacts,
    PrivilegeGrant,
    RoleMembership,
    TablePrivilegeFacts,
)
from .row_filter import quote_identifier


class Finding(StrEnum):
    SUPERUSER = "superuser"
    BYPASSRLS = "bypassrls"
    OWNER_WITHOUT_FORCE = "owner_without_force"
    # MySQL: global SUPER without the superuser rule (global ALL PRIVILEGES, or global SUPER
    # plus every privilege of the write set).
    SUPER_PRIVILEGE = "super_privilege"
    WRITE_PRIVILEGES = "write_privileges"
    READS_EXCLUDED_TABLES = "reads_excluded_tables"
    SQL_FROM_STRING = "sql_from_string"


# Native row-level security does not apply to the role. Counted as tier 1 (the banner's
# RLS title) only while a row-filtered table has RLS enabled (``PrivilegeAudit.rls_in_use``);
# otherwise they are least-privilege findings like the rest.
TIER_1 = frozenset({Finding.SUPERUSER, Finding.BYPASSRLS, Finding.OWNER_WITHOUT_FORCE})

AuditStatus = Literal["ok", "unavailable"]

# The finding's table list is cut here; more_tables counts the rest.
_TABLES_CAP = 50
# PostgreSQL: more table-level write grants than this for one (schema, grantee) pair are
# revoked with one ON ALL TABLES IN SCHEMA line; more owned tables than this in one schema
# are summarised in one comment.
_PER_SCHEMA_COLLAPSE = 10
_DEDICATED_ROLE = "savvina_reader"
_PASSWORD_PLACEHOLDER = "<choose a password>"  # noqa: S105 - a placeholder, not a secret
_SCRIPT_ABOVE = "-- The read-only role script is in the write finding above."
# A line break in a name would end a ``--`` comment and turn the rest of the name into a
# statement the admin copies: comments carry every control character as ``?``.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_TRUNCATED_COMMENT = "-- The list was cut at 500 grants; check again after revoking."
_WRITE_ALL_DATA = "pg_write_all_data"
_READ_ALL_DATA = "pg_read_all_data"
_WRITE_ALL_NOTE = f"-- Membership in {_WRITE_ALL_DATA} allows writing every table."
# Once, before the first collapsed line: GRANT/REVOKE ... ON ALL TABLES IN SCHEMA fails on
# the first table the running role holds no privilege on.
_ON_ALL_TABLES_NOTE = (
    "-- Run the ON ALL TABLES lines as the tables' owner or a superuser:"
    " a table you hold no privilege on aborts the statement."
)


@dataclass
class AuditFinding:
    code: Finding
    tables: list[str] = field(default_factory=list)  # "schema.table"; [] for a role finding
    functions: list[str] = field(default_factory=list)  # SQL_FROM_STRING only
    remediation: str = ""  # the SQL the admin runs; one statement per line
    more_tables: int = 0  # tables beyond the _TABLES_CAP cut of ``tables``


@dataclass
class PrivilegeAudit:
    checked_at: datetime  # UTC
    role: str
    status: AuditStatus
    error: str | None  # unavailable: one line, redacted by category
    findings: list[AuditFinding]
    rls_in_use: bool  # some row-filtered table has RLS enabled; decides the banner title
    # Per row-filtered table key: the engine enforces a SELECT policy on this role. The
    # input of 3b-2. Every key False on MySQL.
    rls_effective: dict[str, bool]


def _is_mysql(dialect: str) -> bool:
    return (dialect or "").lower() == "mysql"


def _account(role: str, dialect: str) -> str:
    """The role as GRANT/REVOKE names it: ``"r"`` on PostgreSQL; ``` `u`@`h` ``` on MySQL,
    split from the ``u@h`` form at its last ``@``. A MySQL name without ``@`` is a MariaDB
    role (``CURRENT_USER()`` always prints a host; MariaDB's ``CURRENT_ROLE()`` prints the
    bare role name) and a REVOKE names it bare: ``` `r` ```, never ``` `r`@`%` ```, which
    MariaDB would read as an account. The backtick form needs no escape rule beyond
    doubling a backtick."""
    if not _is_mysql(dialect):
        return quote_identifier(role, dialect)
    user, at, host = role.rpartition("@")
    if not at:
        return quote_identifier(role, dialect)
    return f"{quote_identifier(user, dialect)}@{quote_identifier(host, dialect)}"


def _grantee(grantee: str, dialect: str) -> str:
    return "PUBLIC" if grantee == "PUBLIC" else _account(grantee, dialect)


def _qualified(fact: TablePrivilegeFacts, dialect: str) -> str:
    """The quoted name of *fact*'s table, from its schema and table names: its key cannot be
    split back at a dot, since a bare excluded entry can match a table in a schema whose name
    holds one (schema ``a.b``, table ``t``: key ``a.b.t``)."""
    schema = fact.schema_name or ""
    return f"{quote_identifier(schema, dialect)}.{quote_identifier(fact.table_name, dialect)}"


def _scope(grant: PrivilegeGrant, dialect: str) -> str:
    if grant.schema_name is None:
        return "*.*"
    schema = quote_identifier(grant.schema_name, dialect)
    if grant.table_name is None:
        return f"{schema}.*"
    return f"{schema}.{quote_identifier(grant.table_name, dialect)}"


def _revoke(
    privileges: list[str], columns: list[str], scope: str, grantee: str, dialect: str
) -> str:
    if columns:
        cols = ", ".join(quote_identifier(c, dialect) for c in columns)
        text = ", ".join(f"{p} ({cols})" for p in privileges)
    else:
        text = ", ".join(privileges)
    return f"REVOKE {text} ON {scope} FROM {_grantee(grantee, dialect)};"


def _comment(text: str) -> str:
    """A ``--`` comment line of *text*, which may hold catalog names: every control
    character (a line break above all) reads ``?``, so the comment cannot end early. A
    REVOKE or GRANT keeps the exact quoted name: a quoted identifier may hold a line break."""
    return "-- " + _CONTROL_CHARACTERS.sub("?", text)


def _wide_grant_comment(tables: list[str], scope: str) -> str:
    """Before the REVOKE of a schema- or global-scope SELECT grant that covers *tables*."""
    return _comment(
        f"{', '.join(tables)}: SELECT comes from the grant on {scope}; revoking it"  # noqa: S608
        " removes SELECT on every table that grant covers. To keep Savvina's access, grant"
        " SELECT per database or per table instead."
    )


def _read_all_comment(tables: list[str]) -> str:
    """Before the REVOKEs of the pg_read_all_data memberships that let the role read
    *tables*."""
    return _comment(
        f"{', '.join(tables)}: SELECT comes from membership in {_READ_ALL_DATA};"  # noqa: S608
        " revoking it removes SELECT on every table. To keep Savvina's access, grant SELECT"
        " per schema or per table instead."
    )


def _memberships(facts: PrivilegeFacts, predefined: str, dialect: str) -> list[RoleMembership]:
    """The *predefined* role memberships, in order and once per (member, grantor); [] on
    MySQL, which has no predefined roles."""
    if _is_mysql(dialect):
        return []
    found: dict[tuple[str, str | None], RoleMembership] = {}
    for membership in facts.predefined_roles:
        if membership.role == predefined:
            found.setdefault((membership.member, membership.grantor), membership)
    return list(found.values())


def _membership_revokes(
    facts: PrivilegeFacts, predefined: str, memberships: list[RoleMembership], dialect: str
) -> list[str]:
    """One ``REVOKE <predefined> FROM <member> [GRANTED BY <grantor>]`` per membership; a
    member that is not the connection role (a role it inherits) is preceded, once, by a
    comment: the REVOKE reaches every other member of that role too."""
    lines: list[str] = []
    commented: set[str] = set()
    for membership in memberships:
        member = _grantee(membership.member, dialect)
        if membership.member != facts.role and membership.member not in commented:
            commented.add(membership.member)
            lines.append(
                _comment(
                    f"Revoking it from {member} also removes it from every other member"
                    f" of {member}."
                )
            )
        granted_by = (
            f" GRANTED BY {_grantee(membership.grantor, dialect)}" if membership.grantor else ""
        )
        lines.append(f"REVOKE {predefined} FROM {member}{granted_by};")
    return lines


def _owner_comment(scope: str) -> str:
    return _comment(
        f"{scope}: the role owns it or inherits the owner; ownership cannot be revoked."
    )


def _table_index(
    facts: PrivilegeFacts,
) -> tuple[dict[str, TablePrivilegeFacts], dict[str, list[TablePrivilegeFacts]]]:
    """The existing tables of *facts* by key and by bare table name; a fact without a
    schema or with ``exists False`` is left out."""
    by_key: dict[str, TablePrivilegeFacts] = {}
    by_name: dict[str, list[TablePrivilegeFacts]] = {}
    for fact in facts.tables:
        if fact.exists and fact.key is not None:
            by_key[fact.key] = fact
            by_name.setdefault(fact.table_name, []).append(fact)
    return by_key, by_name


def _excluded(
    privacy: PrivacySettings,
    by_key: dict[str, TablePrivilegeFacts],
    by_name: dict[str, list[TablePrivilegeFacts]],
) -> list[TablePrivilegeFacts]:
    """The facts of the excluded tables, read as ``is_table_excluded`` reads the entries: a
    qualified entry is one key, a bare entry is that table in every schema."""
    found: list[TablePrivilegeFacts] = []
    for entry in privacy.excluded_tables:
        if not isinstance(entry, str) or not entry:
            continue
        if "." in entry:
            fact = by_key.get(entry)
            if fact is not None:
                found.append(fact)
        else:
            found.extend(by_name.get(entry, []))
    return found


def dedicated_role_script(
    facts: PrivilegeFacts, privacy: PrivacySettings, dialect: str, database: str | None
) -> str:
    """The statements that create a read-only role for Savvina: the superuser remediation,
    and the tail of a finding whose privilege comes from ownership. The schemas are
    ``facts.schemas`` minus the excluded ones; on MySQL the connection's *database* when
    known. The password is a placeholder the admin replaces.

    The excluded tables stay out: PostgreSQL revokes each one in a granted schema after the
    grants; MySQL cannot revoke one table from a database grant, so a final comment names
    the excluded tables in the granted databases."""
    schemas = sorted(s for s in facts.schemas if s not in privacy.excluded_schemas)
    excluded = {fact.key: fact for fact in _excluded(privacy, *_table_index(facts))}

    def excluded_in(granted: list[str]) -> list[TablePrivilegeFacts]:
        return sorted(
            (fact for fact in excluded.values() if fact.schema_name in granted),
            key=lambda fact: (fact.schema_name or "", fact.table_name),
        )

    if _is_mysql(dialect):
        account = f"{quote_identifier(_DEDICATED_ROLE, dialect)}@{quote_identifier('%', dialect)}"
        lines = [
            "-- Create a read-only account for Savvina and point the connection at it.",
            f"CREATE USER {account} IDENTIFIED BY '{_PASSWORD_PLACEHOLDER}';",
        ]
        granted = [database] if database else schemas
        for db in granted:
            lines.append(f"GRANT SELECT ON {quote_identifier(db, dialect)}.* TO {account};")
        readable = ", ".join(_qualified(fact, dialect) for fact in excluded_in(granted))
        if readable:
            lines.append(
                _comment(
                    "MySQL cannot revoke one table from a database grant: the excluded tables"
                    f" {readable} stay readable by this account."
                )
            )
        return "\n".join(lines)
    role = _DEDICATED_ROLE  # a fixed lower-case name: no quoting needed
    lines = [
        "-- Create a read-only role for Savvina and point the connection at it.",
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PASSWORD_PLACEHOLDER}';",
    ]
    if database:
        lines.append(f"GRANT CONNECT ON DATABASE {quote_identifier(database, dialect)} TO {role};")
    for schema in schemas:
        q = quote_identifier(schema, dialect)
        lines.append(f"GRANT USAGE ON SCHEMA {q} TO {role};")
        lines.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {q} TO {role};")
        lines.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {q} GRANT SELECT ON TABLES TO {role};")
    for fact in excluded_in(schemas):
        table = _qualified(fact, dialect)  # quoted identifiers
        lines.append(f"REVOKE SELECT ON {table} FROM {role};")  # noqa: S608
    return "\n".join(lines)


def _capped(keys: list[str]) -> tuple[list[str], int]:
    return keys[:_TABLES_CAP], max(len(keys) - _TABLES_CAP, 0)


def _merged_writes(writes: list[PrivilegeGrant]) -> list[PrivilegeGrant]:
    """Grants with the same scope, grantee and columns as one, privileges sorted; the
    order of first appearance is kept (the adapters sort by scope)."""
    merged: dict[tuple, PrivilegeGrant] = {}
    for grant in writes:
        key = (grant.schema_name, grant.table_name, grant.grantee, tuple(grant.columns))
        if key in merged:
            merged[key].privileges = sorted(set(merged[key].privileges) | set(grant.privileges))
        else:
            merged[key] = PrivilegeGrant(
                grant.schema_name,
                grant.table_name,
                sorted(set(grant.privileges)),
                grant.grantee,
                list(grant.columns),
            )
    return list(merged.values())


def _write_lines(facts: PrivilegeFacts, dialect: str) -> tuple[list[str], list[str], bool]:
    """The remediation lines of WRITE_PRIVILEGES, the table keys they cover (uncapped) and
    whether an ownership comment is among them."""
    mysql = _is_mysql(dialect)
    # PostgreSQL has no global scope: a grant without a schema would print ``ON *.*``.
    grants = [g for g in _merged_writes(facts.writes) if mysql or g.schema_name is not None]
    keys: list[str] = []
    for grant in grants:
        if grant.key is not None and grant.key not in keys:
            keys.append(grant.key)
    # PostgreSQL only: table-level grants per (schema, grantee) and owned tables per schema.
    per_pair: Counter[tuple[str | None, str | None]] = Counter()
    owned_per_schema: Counter[str | None] = Counter()
    if not mysql:
        for grant in grants:
            if grant.table_name is None or grant.columns:
                continue
            if grant.grantee is None:
                owned_per_schema[grant.schema_name] += 1
            else:
                per_pair[(grant.schema_name, grant.grantee)] += 1
    lines: list[str] = []
    owned = False
    collapsed: set[tuple[str | None, str | None]] = set()
    owned_named: Counter[str | None] = Counter()
    for grant in grants:
        scope = _scope(grant, dialect)
        if grant.grantee is None:
            owned = True
            total = owned_per_schema[grant.schema_name]
            if total <= _PER_SCHEMA_COLLAPSE:
                lines.append(_owner_comment(scope))
                continue
            owned_named[grant.schema_name] += 1
            if owned_named[grant.schema_name] <= _PER_SCHEMA_COLLAPSE:
                lines.append(_owner_comment(scope))
            elif owned_named[grant.schema_name] == _PER_SCHEMA_COLLAPSE + 1:
                rest = total - _PER_SCHEMA_COLLAPSE
                schema = quote_identifier(grant.schema_name or "", dialect)
                lines.append(_comment(f"and {rest} more tables in {schema} owned by the role."))
            continue
        pair = (grant.schema_name, grant.grantee)
        if not mysql and not grant.columns and per_pair[pair] > _PER_SCHEMA_COLLAPSE:
            if pair not in collapsed:
                if not collapsed:
                    lines.append(_ON_ALL_TABLES_NOTE)
                collapsed.add(pair)
                schema = quote_identifier(grant.schema_name or "", dialect)
                lines.append(
                    f"REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema}"
                    f" FROM {_grantee(grant.grantee, dialect)};"
                )
            continue
        lines.append(_revoke(grant.privileges, grant.columns, scope, grant.grantee, dialect))
    return lines, keys, owned


def _rls_effective(facts: PrivilegeFacts, fact: TablePrivilegeFacts | None) -> bool:
    if fact is None or not fact.can_select:
        return False
    return (
        fact.rls_enabled
        and fact.select_policy_applies
        and not facts.is_superuser
        and not facts.bypasses_rls
        and (not fact.is_owner or fact.rls_forced)
    )


def _least_privilege(
    facts: PrivilegeFacts,
    privacy: PrivacySettings,
    dialect: str,
    role: str,
    by_key: dict[str, TablePrivilegeFacts],
    by_name: dict[str, list[TablePrivilegeFacts]],
    database: str | None,
) -> list[AuditFinding]:
    """Tier 2: informational, the validator and the read-only transaction already stop a
    write. Not evaluated for a superuser."""
    findings: list[AuditFinding] = []
    script = dedicated_role_script(facts, privacy, dialect, database)

    if facts.has_super:
        # One REVOKE per grantee that holds SUPER (the account, an active role).
        revokes = {
            f"REVOKE SUPER ON *.* FROM {_grantee(grantee, dialect)};": None
            for grantee in facts.super_grantees or [facts.role]
        }
        findings.append(AuditFinding(Finding.SUPER_PRIVILEGE, remediation="\n".join(revokes)))

    lines, keys, owned = _write_lines(facts, dialect)
    write_members = _memberships(facts, _WRITE_ALL_DATA, dialect)
    if write_members:  # every table, so before the per-grant lines; adds no table key
        lines = [
            _WRITE_ALL_NOTE,
            *_membership_revokes(facts, _WRITE_ALL_DATA, write_members, dialect),
            *lines,
        ]
    script_in_writes = bool(lines) and owned
    if lines:
        if owned:
            lines.append(script)
        if facts.writes_truncated:
            lines.append(_TRUNCATED_COMMENT)  # last of all, after the script
        tables, more = _capped(keys)
        findings.append(
            AuditFinding(
                Finding.WRITE_PRIVILEGES,
                tables=tables,
                remediation="\n".join(lines),
                more_tables=more,
            )
        )

    readable: dict[str, TablePrivilegeFacts] = {}
    for fact in _excluded(privacy, by_key, by_name):
        if fact.can_select and fact.key is not None:
            readable.setdefault(fact.key, fact)
    if readable:
        # A line, or the (scope, grantee) of a schema- or global-scope grant: one comment
        # naming every excluded table it covers, then its REVOKE, where first needed.
        items: list[str | tuple[str, str]] = []
        wide: dict[tuple[str, str], list[str]] = {}
        owned = False
        read_members = _memberships(facts, _READ_ALL_DATA, dialect)
        for fact in readable.values():
            table = _qualified(fact, dialect)
            # Without a grant the role reads the table through pg_read_all_data when it is a
            # member (revoked below), else through a grant the catalog did not show.
            fallback = (
                []
                if read_members
                else [PrivilegeGrant(fact.schema_name, fact.table_name, ["SELECT"], facts.role)]
            )
            for grant in fact.select_grants or fallback:
                if grant.grantee is None:
                    owned = True
                    items.append(_owner_comment(table))
                elif grant.table_name is not None:
                    items.append(_revoke(["SELECT"], [], table, grant.grantee, dialect))
                else:
                    group = (_scope(grant, dialect), grant.grantee)
                    if group not in wide:
                        wide[group] = []
                        items.append(group)
                    if table not in wide[group]:
                        wide[group].append(table)
        lines = []
        for item in items:
            if isinstance(item, tuple):
                scope, grantee = item
                lines.append(_wide_grant_comment(wide[item], scope))
                item = _revoke(["SELECT"], [], scope, grantee, dialect)
            if item not in lines:  # the same REVOKE once
                lines.append(item)
        if read_members:
            lines.append(_read_all_comment([_qualified(f, dialect) for f in readable.values()]))
            lines.extend(_membership_revokes(facts, _READ_ALL_DATA, read_members, dialect))
        if owned:
            # Two copies would fail on the second CREATE ROLE: the write finding has it.
            lines.append(_SCRIPT_ABOVE if script_in_writes else script)
        tables, more = _capped(list(readable))
        findings.append(
            AuditFinding(
                Finding.READS_EXCLUDED_TABLES,
                tables=tables,
                remediation="\n".join(lines),
                more_tables=more,
            )
        )

    if facts.sql_from_string_functions:
        functions = list(facts.sql_from_string_functions)
        findings.append(
            AuditFinding(
                Finding.SQL_FROM_STRING,
                functions=functions,
                remediation="\n".join(
                    # Extension functions are granted to PUBLIC on install: revoking from
                    # the role alone would change nothing.
                    f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC, {role};"
                    for fn in functions
                ),
            )
        )
    return findings


def evaluate(
    facts: PrivilegeFacts,
    privacy: PrivacySettings,
    dialect: str,
    database: str | None = None,
) -> PrivilegeAudit:
    """The findings and ``rls_effective`` for *facts* under the connection's *privacy*.

    *dialect* is the connection's ``source_type`` (``postgresql`` / ``mysql``, any case);
    *database* is the connection's database, named by the dedicated-role script. A table
    absent from ``facts.tables`` or with ``exists False`` produces no finding. A superuser
    (PostgreSQL ``rolsuper``; MySQL global ``ALL PRIVILEGES``, or global ``SUPER`` plus every
    privilege of the write set, since MySQL 8 prints ``GRANT ALL ON *.*`` as the expanded
    list) skips the tier 2 findings (every privilege follows from it) but not the tier 1
    ones: ``SUPERUSER``, ``BYPASSRLS`` when set, and ``OWNER_WITHOUT_FORCE``. The latter
    needs ``rls_enabled``: the audit reruns on every Test and save and the 3b-2 recheck reads
    the facts live, so the moment RLS is enabled is covered.
    """
    mysql = _is_mysql(dialect)
    role = _account(facts.role, dialect)
    by_key, by_name = _table_index(facts)
    filtered = [(key, by_key.get(key)) for key in privacy.row_filters]

    findings: list[AuditFinding] = []
    if facts.is_superuser:
        findings.append(
            AuditFinding(
                Finding.SUPERUSER,
                remediation=dedicated_role_script(facts, privacy, dialect, database),
            )
        )
    if facts.bypasses_rls:
        findings.append(
            AuditFinding(Finding.BYPASSRLS, remediation=f"ALTER ROLE {role} NOBYPASSRLS;")
        )
    owned = [
        (key, fact)
        for key, fact in filtered
        if fact is not None and fact.rls_enabled and fact.is_owner and not fact.rls_forced
    ]
    if owned:
        findings.append(
            AuditFinding(
                Finding.OWNER_WITHOUT_FORCE,
                tables=[key for key, _ in owned],
                remediation="\n".join(
                    f"ALTER TABLE {_qualified(fact, dialect)} FORCE ROW LEVEL SECURITY;"
                    for _, fact in owned
                ),
            )
        )
    if not facts.is_superuser:
        findings.extend(_least_privilege(facts, privacy, dialect, role, by_key, by_name, database))

    return PrivilegeAudit(
        checked_at=datetime.now(UTC),
        role=facts.role,
        status="ok",
        error=None,
        findings=findings,
        rls_in_use=any(fact.rls_enabled for _, fact in filtered if fact is not None),
        rls_effective={
            key: (False if mysql else _rls_effective(facts, fact)) for key, fact in filtered
        },
    )


def has_findings(audit: PrivilegeAudit, tier: int) -> bool:
    """Tier 1: a ``TIER_1`` code while ``rls_in_use``. Tier 2: every other finding, the
    ``TIER_1`` codes included when RLS is not in use."""
    tier_1 = audit.rls_in_use and any(f.code in TIER_1 for f in audit.findings)
    if tier == 1:
        return tier_1
    return any(not (audit.rls_in_use and f.code in TIER_1) for f in audit.findings)


def unavailable(role: str, error: str) -> PrivilegeAudit:
    """The stored result when the catalog could not be read: no findings, nothing effective.
    *role* is ``""`` when the connection never answered the role query."""
    return PrivilegeAudit(
        checked_at=datetime.now(UTC),
        role=role,
        status="unavailable",
        error=error,
        findings=[],
        rls_in_use=False,
        rls_effective={},
    )


def to_dict(audit: PrivilegeAudit) -> dict:
    return {
        "checked_at": audit.checked_at.isoformat(),
        "role": audit.role,
        "status": audit.status,
        "error": audit.error,
        "findings": [
            {
                "code": finding.code.value,
                "tables": list(finding.tables),
                "functions": list(finding.functions),
                "remediation": finding.remediation,
                "more_tables": finding.more_tables,
            }
            for finding in audit.findings
        ],
        "rls_in_use": audit.rls_in_use,
        "rls_effective": dict(audit.rls_effective),
    }


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def from_dict(data: object) -> PrivilegeAudit | None:
    """The stored audit, or ``None`` for anything that is not one (a tampered or outdated
    column). In the style of ``PrivacySettings.from_dict``: unknown keys are ignored, missing
    lists read empty, and a finding whose code this version does not know is dropped.
    """
    if not isinstance(data, dict):
        return None
    try:
        checked_at = datetime.fromisoformat(str(data["checked_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    status = data.get("status")
    if status not in ("ok", "unavailable"):
        return None
    findings: list[AuditFinding] = []
    raw_findings = data.get("findings")
    for item in raw_findings if isinstance(raw_findings, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            code = Finding(item.get("code"))
        except ValueError:
            continue
        findings.append(
            AuditFinding(
                code=code,
                tables=_strings(item.get("tables")),
                functions=_strings(item.get("functions")),
                remediation=_text(item.get("remediation")),
                more_tables=_count(item.get("more_tables")),
            )
        )
    effective = data.get("rls_effective")
    error = data.get("error")
    return PrivilegeAudit(
        checked_at=checked_at,
        role=_text(data.get("role")),
        status=status,
        error=error if isinstance(error, str) else None,
        findings=findings,
        rls_in_use=data.get("rls_in_use") is True,
        rls_effective=(
            {k: v is True for k, v in effective.items() if isinstance(k, str)}
            if isinstance(effective, dict)
            else {}
        ),
    )
