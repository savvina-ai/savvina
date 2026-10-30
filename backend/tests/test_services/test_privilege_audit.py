# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``services/privilege_audit``: findings and ``rls_effective`` from hand-built facts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import re

import pytest

from app.datasources.models import (
    PrivacySettings,
    PrivilegeFacts,
    PrivilegeGrant,
    RoleMembership,
    TablePrivilegeFacts,
)
from app.services.privilege_audit import (
    TIER_1,
    AuditFinding,
    Finding,
    PrivilegeAudit,
    dedicated_role_script,
    evaluate,
    from_dict,
    has_findings,
    to_dict,
    unavailable,
)
from app.services.row_filter import quote_identifier


class TestQuoteIdentifier:
    @pytest.mark.parametrize(
        ("part", "dialect", "expected"),
        [
            ("Orders", "postgresql", '"Orders"'),
            ("Orders", "PostgreSQL", '"Orders"'),
            ("orders", "mysql", "`orders`"),
            ("orders", "MySQL", "`orders`"),
            ('a"b', "postgresql", '"a""b"'),
            ("a`b", "mysql", "`a``b`"),
            ("x", "", '"x"'),
            ('a"b', "mysql", '`a"b`'),
            ("a`b", "postgresql", '"a`b"'),
            ("x", None, '"x"'),
        ],
    )
    def test_quotes_with_the_dialects_quote_and_doubles_it_inside(
        self, part: str, dialect: str | None, expected: str
    ) -> None:
        assert quote_identifier(part, dialect) == expected


def _table(
    schema: str | None = "public",
    table: str = "orders",
    *,
    exists: bool = True,
    can_select: bool = True,
    is_owner: bool = False,
    rls_enabled: bool = False,
    rls_forced: bool = False,
    policy: bool = False,
    select_grantees: Sequence[str | None] = (),
    select_grants: Sequence[PrivilegeGrant] = (),
) -> TablePrivilegeFacts:
    """*select_grantees*: the grantees of table-scope SELECT grants (None for ownership), the
    short form; *select_grants*: grants at any scope, appended after them."""
    return TablePrivilegeFacts(
        schema_name=schema,
        table_name=table,
        exists=exists,
        can_select=can_select,
        is_owner=is_owner,
        rls_enabled=rls_enabled,
        rls_forced=rls_forced,
        select_policy_applies=policy,
        select_grants=[PrivilegeGrant(schema, table, ["SELECT"], g) for g in select_grantees]
        + list(select_grants),
    )


def _grant(
    schema: str | None = "public",
    table: str | None = "orders",
    privileges: Sequence[str] = ("INSERT", "UPDATE", "DELETE"),
    grantee: str | None = "savvina",
    columns: Sequence[str] = (),
) -> PrivilegeGrant:
    return PrivilegeGrant(schema, table, list(privileges), grantee, list(columns))


def _owner(table: str, schema: str = "public") -> str:
    return (
        f'-- "{schema}"."{table}": the role owns it or inherits the owner;'
        " ownership cannot be revoked."
    )


_ON_ALL_TABLES_NOTE = (
    "-- Run the ON ALL TABLES lines as the tables' owner or a superuser:"
    " a table you hold no privilege on aborts the statement."
)


def _wide(tables: str, scope: str) -> str:
    """The comment before a schema- or global-scope SELECT revoke."""
    return (
        f"-- {tables}: SELECT comes from the grant on {scope};"  # noqa: S608
        " revoking it removes SELECT on every table that grant covers. To keep Savvina's"
        " access, grant SELECT per database or per table instead."
    )


def _facts(role: str = "savvina", **kwargs) -> PrivilegeFacts:
    return PrivilegeFacts(role=role, **kwargs)


def _privacy(filters: Sequence[str] = (), excluded: Sequence[str] = ()) -> PrivacySettings:
    return PrivacySettings(
        row_filters={k: "tenant_id = 1" for k in filters}, excluded_tables=list(excluded)
    )


def _codes(audit: PrivilegeAudit) -> list[Finding]:
    return [f.code for f in audit.findings]


def _finding(audit: PrivilegeAudit, code: Finding) -> AuditFinding:
    return next(f for f in audit.findings if f.code == code)


class TestRoleFindings:
    def test_a_clean_role_has_no_findings(self) -> None:
        audit = evaluate(_facts(tables=[_table()]), _privacy(["public.orders"]), "postgresql")
        assert audit.status == "ok"
        assert audit.error is None
        assert audit.role == "savvina"
        assert audit.findings == []
        assert audit.rls_in_use is False
        assert audit.checked_at.tzinfo is UTC

    def test_superuser_postgresql(self) -> None:
        audit = evaluate(_facts(is_superuser=True), _privacy(), "postgresql")
        assert _codes(audit) == [Finding.SUPERUSER]

    def test_superuser_hides_every_least_privilege_finding(self) -> None:
        """Every privilege function answers true for a superuser; the REVOKEs would change
        nothing. BYPASSRLS keeps its own remediation."""
        facts = _facts(
            is_superuser=True,
            bypasses_rls=True,
            writes=[_grant(), _grant(table="audit_log")],
            sql_from_string_functions=["public.dblink(text)"],
            tables=[_table(), _table(table="audit_log")],
        )
        audit = evaluate(facts, _privacy(["public.orders"], ["public.audit_log"]), "postgresql")
        assert _codes(audit) == [Finding.SUPERUSER, Finding.BYPASSRLS]

    def test_bypassrls(self) -> None:
        audit = evaluate(_facts(bypasses_rls=True), _privacy(), "postgresql")
        assert _codes(audit) == [Finding.BYPASSRLS]
        assert _finding(audit, Finding.BYPASSRLS).remediation == 'ALTER ROLE "savvina" NOBYPASSRLS;'


class TestOwnerWithoutForce:
    def test_owner_of_a_filtered_table_with_rls_and_no_force(self) -> None:
        facts = _facts(tables=[_table(is_owner=True, rls_enabled=True, policy=True)])
        audit = evaluate(facts, _privacy(["public.orders"]), "postgresql")
        assert _codes(audit) == [Finding.OWNER_WITHOUT_FORCE]
        finding = _finding(audit, Finding.OWNER_WITHOUT_FORCE)
        assert finding.tables == ["public.orders"]
        assert finding.remediation == 'ALTER TABLE "public"."orders" FORCE ROW LEVEL SECURITY;'
        assert audit.rls_in_use is True
        assert audit.rls_effective == {"public.orders": False}

    def test_owner_without_rls_is_not_reported(self) -> None:
        facts = _facts(tables=[_table(is_owner=True)])
        audit = evaluate(facts, _privacy(["public.orders"]), "postgresql")
        assert audit.findings == []

    def test_owner_with_force_is_not_reported(self) -> None:
        facts = _facts(tables=[_table(is_owner=True, rls_enabled=True, rls_forced=True)])
        audit = evaluate(facts, _privacy(["public.orders"]), "postgresql")
        assert audit.findings == []

    def test_an_owned_excluded_table_is_not_an_owner_finding(self) -> None:
        facts = _facts(tables=[_table(table="audit_log", is_owner=True, rls_enabled=True)])
        audit = evaluate(facts, _privacy([], ["public.audit_log"]), "postgresql")
        assert Finding.OWNER_WITHOUT_FORCE not in _codes(audit)


class TestWritePrivileges:
    def test_no_write_grants_no_finding(self) -> None:
        audit = evaluate(_facts(tables=[_table()]), _privacy(["public.orders"]), "postgresql")
        assert Finding.WRITE_PRIVILEGES not in _codes(audit)

    def test_a_direct_table_grant_is_revoked_from_the_role(self) -> None:
        facts = _facts(writes=[_grant()])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.tables == ["public.orders"] and f.more_tables == 0
        assert f.remediation == 'REVOKE DELETE, INSERT, UPDATE ON "public"."orders" FROM "savvina";'

    def test_privileges_are_sorted_and_identical_grants_merge(self) -> None:
        facts = _facts(writes=[_grant(privileges=["UPDATE"]), _grant(privileges=["INSERT"])])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation == 'REVOKE INSERT, UPDATE ON "public"."orders" FROM "savvina";'

    def test_the_grantee_is_the_group_role_or_public(self) -> None:
        facts = _facts(
            writes=[
                _grant(grantee="PUBLIC", privileges=["INSERT"]),
                _grant(grantee="writers", privileges=["DELETE"]),
            ]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            'REVOKE INSERT ON "public"."orders" FROM PUBLIC;',
            'REVOKE DELETE ON "public"."orders" FROM "writers";',
        ]

    def test_column_level_grants_name_the_columns_per_privilege(self) -> None:
        facts = _facts(writes=[_grant(privileges=["INSERT", "UPDATE"], columns=["status", "note"])])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation == (
            'REVOKE INSERT ("status", "note"), UPDATE ("status", "note")'
            ' ON "public"."orders" FROM "savvina";'
        )

    def test_ownership_is_a_comment_and_the_dedicated_role_script(self) -> None:
        facts = _facts(writes=[_grant(grantee=None)], schemas=["public"])
        audit = evaluate(facts, _privacy(), "postgresql", database="app")
        f = _finding(audit, Finding.WRITE_PRIVILEGES)
        lines = f.remediation.splitlines()
        assert lines[0] == (
            '-- "public"."orders": the role owns it or inherits the owner;'
            " ownership cannot be revoked."
        )
        script = dedicated_role_script(facts, _privacy(), "postgresql", "app")
        assert lines[1:] == script.splitlines()

    def test_more_than_ten_owned_tables_in_a_schema_collapse(self) -> None:
        facts = _facts(writes=[_grant(table=f"t{i:02d}", grantee=None) for i in range(12)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        lines = f.remediation.splitlines()
        script = dedicated_role_script(facts, _privacy(), "postgresql", None).splitlines()
        assert lines[-len(script) :] == script
        assert lines[: -len(script)] == [_owner(f"t{i:02d}") for i in range(10)] + [
            '-- and 2 more tables in "public" owned by the role.'
        ]

    def test_exactly_ten_owned_tables_in_a_schema_do_not_collapse(self) -> None:
        facts = _facts(writes=[_grant(table=f"t{i:02d}", grantee=None) for i in range(10)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        lines = f.remediation.splitlines()
        script = dedicated_role_script(facts, _privacy(), "postgresql", None).splitlines()
        assert lines[: -len(script)] == [_owner(f"t{i:02d}") for i in range(10)]
        assert not any("more tables" in line for line in lines)

    def test_owned_tables_collapse_once_per_schema(self) -> None:
        facts = _facts(
            writes=[_grant(table=f"t{i:02d}", grantee=None) for i in range(12)]
            + [_grant("s2", f"t{i:02d}", grantee=None) for i in range(13)]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        tails = [line for line in f.remediation.splitlines() if "more tables" in line]
        assert tails == [
            '-- and 2 more tables in "public" owned by the role.',
            '-- and 3 more tables in "s2" owned by the role.',
        ]

    def test_a_finding_of_ownership_only_is_comments_and_the_script(self) -> None:
        facts = _facts(writes=[_grant(grantee=None)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        script = dedicated_role_script(facts, _privacy(), "postgresql", None).splitlines()
        assert f.remediation.splitlines() == [_owner("orders"), *script]
        assert f.tables == ["public.orders"]
        assert not any(line.startswith("REVOKE") for line in f.remediation.splitlines())

    def test_exactly_ten_table_grants_do_not_collapse(self) -> None:
        facts = _facts(writes=[_grant(table=f"t{i:02d}") for i in range(10)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            f'REVOKE DELETE, INSERT, UPDATE ON "public"."t{i:02d}" FROM "savvina";'
            for i in range(10)
        ]

    def test_public_and_the_role_collapse_independently(self) -> None:
        facts = _facts(
            writes=[_grant(table=f"t{i:02d}", grantee="PUBLIC") for i in range(11)]
            + [_grant(table=f"t{i:02d}") for i in range(11)]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            _ON_ALL_TABLES_NOTE,
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" FROM PUBLIC;',
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" FROM "savvina";',
        ]
        assert len(f.tables) == 11

    def test_the_on_all_tables_note_comes_once_before_the_first_collapsed_line(self) -> None:
        facts = _facts(
            writes=[_grant(schema="a", table="x", privileges=["UPDATE"], columns=["c"])]
            + [_grant(table=f"t{i:02d}") for i in range(11)]
            + [_grant(schema="s2", table=f"t{i:02d}") for i in range(11)]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            'REVOKE UPDATE ("c") ON "a"."x" FROM "savvina";',
            _ON_ALL_TABLES_NOTE,
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" FROM "savvina";',
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "s2" FROM "savvina";',
        ]

    def test_two_collapsed_pairs_share_one_note_first(self) -> None:
        facts = _facts(
            writes=[_grant(table=f"t{i:02d}") for i in range(11)]
            + [_grant(schema="s2", table=f"t{i:02d}") for i in range(11)]
        )
        lines = _finding(
            evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES
        ).remediation.splitlines()
        assert lines[0] == _ON_ALL_TABLES_NOTE and lines.count(_ON_ALL_TABLES_NOTE) == 1
        assert len(lines) == 3

    def test_no_note_without_a_collapsed_line(self) -> None:
        facts = _facts(writes=[_grant(table=f"t{i:02d}") for i in range(10)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert _ON_ALL_TABLES_NOTE not in f.remediation

    def test_a_postgresql_grant_without_a_schema_is_skipped(self) -> None:
        facts = _facts(writes=[_grant(None, None, ["INSERT"]), _grant()])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation == 'REVOKE DELETE, INSERT, UPDATE ON "public"."orders" FROM "savvina";'
        only = _facts(writes=[_grant(None, None, ["INSERT"])])
        assert Finding.WRITE_PRIVILEGES not in _codes(evaluate(only, _privacy(), "postgresql"))

    def test_more_than_ten_table_grants_per_schema_and_grantee_collapse_on_postgresql(
        self,
    ) -> None:
        facts = _facts(
            writes=[_grant(table=f"t{i:02d}") for i in range(11)] + [_grant(schema="s2")]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            _ON_ALL_TABLES_NOTE,
            'REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "public" FROM "savvina";',
            'REVOKE DELETE, INSERT, UPDATE ON "s2"."orders" FROM "savvina";',
        ]
        assert len(f.tables) == 12

    def test_column_grants_never_collapse(self) -> None:
        facts = _facts(
            writes=[_grant(table=f"t{i:02d}") for i in range(11)]
            + [_grant(table="t00", privileges=["UPDATE"], columns=["a"])]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines()[-1] == (
            'REVOKE UPDATE ("a") ON "public"."t00" FROM "savvina";'
        )

    def test_the_table_list_is_capped_at_fifty(self) -> None:
        facts = _facts(writes=[_grant(table=f"t{i:03d}", grantee="PUBLIC") for i in range(60)])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert len(f.tables) == 50 and f.more_tables == 10
        assert f.tables[0] == "public.t000"

    def test_a_truncated_fact_list_ends_with_a_comment_after_the_script(self) -> None:
        facts = _facts(writes=[_grant(), _grant(table="t2", grantee=None)], writes_truncated=True)
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        lines = f.remediation.splitlines()
        assert lines[-1] == "-- The list was cut at 500 grants; check again after revoking."
        assert lines[-2].startswith("CREATE ROLE savvina_reader")  # the script precedes it

    def test_a_truncated_fact_list_without_ownership_ends_with_the_comment(self) -> None:
        facts = _facts(writes=[_grant()], writes_truncated=True)
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            'REVOKE DELETE, INSERT, UPDATE ON "public"."orders" FROM "savvina";',
            "-- The list was cut at 500 grants; check again after revoking.",
        ]

    def test_mysql_levels_and_the_backtick_account_form(self) -> None:
        facts = _facts(
            role="savvina@%",
            writes=[
                _grant(None, None, ["FILE", "INSERT"], "savvina@%"),
                _grant("sample\\_delivery", None, ["ALL PRIVILEGES"], "savvina@%"),
                _grant("shop", "orders", ["DELETE"], "writer@%"),
                _grant("shop", "orders", ["UPDATE"], "savvina@%", ["status"]),
            ],
        )
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.WRITE_PRIVILEGES)
        assert f.tables == ["shop.orders"]
        assert f.remediation.splitlines() == [
            "REVOKE FILE, INSERT ON *.* FROM `savvina`@`%`;",
            "REVOKE ALL PRIVILEGES ON `sample\\_delivery`.* FROM `savvina`@`%`;",
            "REVOKE DELETE ON `shop`.`orders` FROM `writer`@`%`;",
            "REVOKE UPDATE (`status`) ON `shop`.`orders` FROM `savvina`@`%`;",
        ]

    def test_mysql_column_grants_with_different_columns_stay_apart(self) -> None:
        facts = _facts(
            role="u@h",
            writes=[
                _grant("shop", "orders", ["UPDATE"], "u@h", ["a"]),
                _grant("shop", "orders", ["UPDATE"], "u@h", ["b"]),
            ],
        )
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            "REVOKE UPDATE (`a`) ON `shop`.`orders` FROM `u`@`h`;",
            "REVOKE UPDATE (`b`) ON `shop`.`orders` FROM `u`@`h`;",
        ]
        assert f.tables == ["shop.orders"]

    def test_mysql_never_collapses_per_schema(self) -> None:
        facts = _facts(
            role="u@h", writes=[_grant("shop", f"t{i:02d}", ["INSERT"], "u@h") for i in range(11)]
        )
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.WRITE_PRIVILEGES)
        assert len(f.remediation.splitlines()) == 11

    def test_a_mysql_global_only_grant_reports_no_table(self) -> None:
        facts = _facts(role="u@h", writes=[_grant(None, None, ["INSERT"], "u@h")])
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.WRITE_PRIVILEGES)
        assert f.tables == [] and f.more_tables == 0
        assert f.remediation == "REVOKE INSERT ON *.* FROM `u`@`h`;"

    def test_a_mysql_host_with_a_backtick_is_doubled(self) -> None:
        facts = _facts(role="u@a`b", writes=[_grant(None, None, ["INSERT"], "u@a`b")])
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation == "REVOKE INSERT ON *.* FROM `u`@`a``b`;"


class TestReadsExcludedTables:
    def test_an_excluded_table_the_role_reads_is_revoked_from_each_grantee(self) -> None:
        facts = _facts(tables=[_table("hr", "salaries", select_grantees=["PUBLIC", "savvina"])])
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        f = _finding(audit, Finding.READS_EXCLUDED_TABLES)
        assert f.tables == ["hr.salaries"]
        assert f.remediation.splitlines() == [
            'REVOKE SELECT ON "hr"."salaries" FROM PUBLIC;',
            'REVOKE SELECT ON "hr"."salaries" FROM "savvina";',
        ]

    def test_no_select_grants_falls_back_to_the_role(self) -> None:
        facts = _facts(tables=[_table("hr", "salaries")])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation == 'REVOKE SELECT ON "hr"."salaries" FROM "savvina";'

    def test_ownership_gets_the_comment_and_the_script(self) -> None:
        facts = _facts(
            tables=[_table("hr", "salaries", select_grantees=[None])], schemas=["hr", "public"]
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        lines = f.remediation.splitlines()
        assert lines[0].startswith('-- "hr"."salaries": the role owns it')
        assert "CREATE ROLE savvina_reader" in lines[2]

    def test_a_bare_entry_matches_every_schema(self) -> None:
        facts = _facts(
            tables=[
                _table("a", "secrets", select_grantees=["savvina"]),
                _table("b", "secrets", select_grantees=["savvina"]),
            ]
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["secrets"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.tables == ["a.secrets", "b.secrets"]

    def test_an_excluded_table_the_role_cannot_read_is_fine(self) -> None:
        facts = _facts(tables=[_table("hr", "salaries", can_select=False)])
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        assert Finding.READS_EXCLUDED_TABLES not in _codes(audit)

    def test_a_missing_table_produces_nothing(self) -> None:
        facts = _facts(
            tables=[
                _table("hr", "salaries", exists=False, select_grantees=["savvina"]),
                _table(
                    "public",
                    "orders",
                    exists=False,
                    is_owner=True,
                    rls_enabled=True,
                    select_grantees=[None],
                ),
            ]
        )
        audit = evaluate(facts, _privacy(["public.orders"], excluded=["hr.salaries"]), "postgresql")
        assert audit.findings == []

    def test_a_key_both_filtered_and_excluded_appears_under_both_codes(self) -> None:
        facts = _facts(
            tables=[_table(is_owner=True, rls_enabled=True, select_grantees=["savvina"])]
        )
        audit = evaluate(
            facts, _privacy(["public.orders"], excluded=["public.orders"]), "postgresql"
        )
        codes = _codes(audit)
        assert Finding.OWNER_WITHOUT_FORCE in codes and Finding.READS_EXCLUDED_TABLES in codes
        assert _finding(audit, Finding.OWNER_WITHOUT_FORCE).tables == ["public.orders"]
        assert _finding(audit, Finding.READS_EXCLUDED_TABLES).tables == ["public.orders"]

    def test_mysql_role_grantee(self) -> None:
        facts = _facts(role="u@%", tables=[_table("hr", "salaries", select_grantees=["reader@%"])])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation == "REVOKE SELECT ON `hr`.`salaries` FROM `reader`@`%`;"

    def test_a_mysql_schema_grant_is_revoked_at_the_schema_with_a_warning(self) -> None:
        grant = PrivilegeGrant("hr", None, ["SELECT"], "u@%")
        facts = _facts(role="u@%", tables=[_table("hr", "salaries", select_grants=[grant])])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines() == [
            _wide("`hr`.`salaries`", "`hr`.*"),
            "REVOKE SELECT ON `hr`.* FROM `u`@`%`;",
        ]

    def test_a_mysql_global_grant_is_revoked_at_star_star_with_a_warning(self) -> None:
        grant = PrivilegeGrant(None, None, ["SELECT"], "u@%")
        facts = _facts(role="u@%", tables=[_table("hr", "salaries", select_grants=[grant])])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines() == [
            _wide("`hr`.`salaries`", "*.*"),
            "REVOKE SELECT ON *.* FROM `u`@`%`;",
        ]

    def test_two_tables_under_one_schema_grant_share_one_revoke(self) -> None:
        grant = PrivilegeGrant("hr", None, ["SELECT"], "u@%")
        facts = _facts(
            role="u@%",
            tables=[
                _table("hr", "salaries", select_grants=[grant]),
                _table("hr", "reviews", select_grants=[grant]),
            ],
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries", "hr.reviews"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.tables == ["hr.salaries", "hr.reviews"]
        assert f.remediation.splitlines() == [
            _wide("`hr`.`salaries`, `hr`.`reviews`", "`hr`.*"),
            "REVOKE SELECT ON `hr`.* FROM `u`@`%`;",
        ]

    def test_a_wide_grant_group_keeps_its_place_of_first_use(self) -> None:
        grant = PrivilegeGrant("hr", None, ["SELECT"], "u@%")
        facts = _facts(
            role="u@%",
            tables=[
                _table("hr", "salaries", select_grants=[grant]),
                _table("hr", "audit", select_grantees=["u@%"]),
                _table("hr", "reviews", select_grants=[grant]),
            ],
        )
        privacy = _privacy(excluded=["hr.salaries", "hr.audit", "hr.reviews"])
        f = _finding(evaluate(facts, privacy, "mysql"), Finding.READS_EXCLUDED_TABLES)
        assert f.remediation.splitlines() == [
            _wide("`hr`.`salaries`, `hr`.`reviews`", "`hr`.*"),
            "REVOKE SELECT ON `hr`.* FROM `u`@`%`;",
            "REVOKE SELECT ON `hr`.`audit` FROM `u`@`%`;",
        ]

    def test_an_escaped_schema_literal_is_revoked_as_written(self) -> None:
        grant = PrivilegeGrant("sample\\_delivery", None, ["SELECT"], "u@%")
        facts = _facts(
            role="u@%", tables=[_table("sample_delivery", "orders", select_grants=[grant])]
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["sample_delivery.orders"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines() == [
            _wide("`sample_delivery`.`orders`", "`sample\\_delivery`.*"),
            "REVOKE SELECT ON `sample\\_delivery`.* FROM `u`@`%`;",
        ]

    def test_a_mariadb_role_at_schema_scope_is_written_without_a_host(self) -> None:
        grant = PrivilegeGrant("hr", None, ["SELECT"], "reader")
        facts = _facts(role="u@%", tables=[_table("hr", "salaries", select_grants=[grant])])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines() == [
            _wide("`hr`.`salaries`", "`hr`.*"),
            "REVOKE SELECT ON `hr`.* FROM `reader`;",
        ]

    def test_a_mariadb_role_grantee_is_written_without_a_host(self) -> None:
        # MariaDB's CURRENT_ROLE() prints the bare role name; `reader`@`%` would be an account.
        facts = _facts(role="u@%", tables=[_table("hr", "salaries", select_grantees=["reader"])])
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation == "REVOKE SELECT ON `hr`.`salaries` FROM `reader`;"


class TestDottedSchemaNames:
    """A bare excluded entry matches a table in a schema whose name holds a dot: the quoted
    name comes from the fact's schema and table, not from splitting its key at a dot."""

    def test_reads_excluded_names_the_schema_and_table_of_the_fact(self) -> None:
        facts = _facts(tables=[_table("a.b", "t")])
        audit = evaluate(facts, _privacy([], ["t"]), "postgresql")
        finding = _finding(audit, Finding.READS_EXCLUDED_TABLES)
        assert finding.tables == ["a.b.t"]
        assert finding.remediation == 'REVOKE SELECT ON "a.b"."t" FROM "savvina";'

    def test_write_privileges_names_the_schema_and_table_of_the_grant(self) -> None:
        facts = _facts(writes=[_grant("a.b", "t")])
        audit = evaluate(facts, _privacy(), "postgresql")
        finding = _finding(audit, Finding.WRITE_PRIVILEGES)
        assert finding.tables == ["a.b.t"]
        assert finding.remediation == 'REVOKE DELETE, INSERT, UPDATE ON "a.b"."t" FROM "savvina";'

    def test_mysql_schema_grant_of_a_dotted_schema(self) -> None:
        facts = _facts(role="u@h", writes=[_grant("a.b", None, ["DELETE", "INSERT"], "u@h")])
        audit = evaluate(facts, _privacy(), "mysql")
        finding = _finding(audit, Finding.WRITE_PRIVILEGES)
        assert finding.tables == []
        assert finding.remediation == "REVOKE DELETE, INSERT ON `a.b`.* FROM `u`@`h`;"

    def test_mysql_table_revoke_names_the_schema_and_table_of_the_grant(self) -> None:
        facts = _facts(role="u@h", writes=[_grant("a.b", "t", ["DELETE", "INSERT"], "u@h")])
        audit = evaluate(facts, _privacy(), "mysql")
        assert _finding(audit, Finding.WRITE_PRIVILEGES).remediation == (
            "REVOKE DELETE, INSERT ON `a.b`.`t` FROM `u`@`h`;"
        )


class TestSuperuser:
    def test_postgresql_remediation_is_the_dedicated_role_script(self) -> None:
        facts = _facts(is_superuser=True, schemas=["public", "hr"])
        audit = evaluate(facts, _privacy(), "postgresql", database="app")
        f = _finding(audit, Finding.SUPERUSER)
        assert f.remediation == dedicated_role_script(facts, _privacy(), "postgresql", "app")
        assert f.remediation.splitlines() == [
            "-- Create a read-only role for Savvina and point the connection at it.",
            "CREATE ROLE savvina_reader LOGIN PASSWORD '<choose a password>';",
            'GRANT CONNECT ON DATABASE "app" TO savvina_reader;',
            'GRANT USAGE ON SCHEMA "hr" TO savvina_reader;',
            'GRANT SELECT ON ALL TABLES IN SCHEMA "hr" TO savvina_reader;',
            'ALTER DEFAULT PRIVILEGES IN SCHEMA "hr" GRANT SELECT ON TABLES TO savvina_reader;',
            'GRANT USAGE ON SCHEMA "public" TO savvina_reader;',
            'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO savvina_reader;',
            'ALTER DEFAULT PRIVILEGES IN SCHEMA "public" GRANT SELECT ON TABLES TO savvina_reader;',
        ]

    def test_no_database_drops_the_connect_line_and_excluded_schemas_are_skipped(self) -> None:
        facts = _facts(is_superuser=True, schemas=["public", "hr"])
        privacy = PrivacySettings(excluded_schemas=["hr"])
        script = dedicated_role_script(facts, privacy, "postgresql", None)
        assert "CONNECT" not in script and '"hr"' not in script and '"public"' in script

    def test_mysql_script_grants_the_connection_database(self) -> None:
        facts = _facts(role="root@localhost", is_superuser=True, schemas=["shop", "other"])
        f = _finding(evaluate(facts, _privacy(), "mysql", database="shop"), Finding.SUPERUSER)
        assert f.remediation.splitlines() == [
            "-- Create a read-only account for Savvina and point the connection at it.",
            "CREATE USER `savvina_reader`@`%` IDENTIFIED BY '<choose a password>';",
            "GRANT SELECT ON `shop`.* TO `savvina_reader`@`%`;",
        ]

    def test_mysql_script_without_a_database_grants_every_schema(self) -> None:
        facts = _facts(role="root@localhost", is_superuser=True, schemas=["shop", "other"])
        script = dedicated_role_script(facts, _privacy(), "mysql", None)
        assert script.splitlines()[2:] == [
            "GRANT SELECT ON `other`.* TO `savvina_reader`@`%`;",
            "GRANT SELECT ON `shop`.* TO `savvina_reader`@`%`;",
        ]

    def test_a_superuser_skips_tier_2_but_not_tier_1(self) -> None:
        facts = _facts(
            is_superuser=True,
            bypasses_rls=True,
            writes=[_grant()],
            tables=[_table(is_owner=True, rls_enabled=True, select_grantees=[None])],
        )
        privacy = _privacy(["public.orders"], excluded=["public.orders"])
        audit = evaluate(facts, privacy, "postgresql")
        assert _codes(audit) == [Finding.SUPERUSER, Finding.BYPASSRLS, Finding.OWNER_WITHOUT_FORCE]


class TestScriptOnce:
    _ABOVE = "-- The read-only role script is in the write finding above."

    def test_reads_point_at_the_script_of_the_write_finding(self) -> None:
        facts = _facts(
            writes=[_grant(grantee=None)],
            tables=[_table("hr", "salaries", select_grantees=[None])],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        writes = _finding(audit, Finding.WRITE_PRIVILEGES).remediation
        reads = _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation
        assert "CREATE ROLE savvina_reader" in writes
        assert "CREATE ROLE" not in reads
        assert reads.splitlines() == [_owner("salaries", "hr"), self._ABOVE]

    def test_reads_carry_the_script_when_the_write_finding_does_not(self) -> None:
        facts = _facts(
            writes=[_grant()],
            tables=[_table("hr", "salaries", select_grantees=[None])],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        assert "CREATE ROLE" not in _finding(audit, Finding.WRITE_PRIVILEGES).remediation
        reads = _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation
        assert "CREATE ROLE savvina_reader" in reads and self._ABOVE not in reads


_EVIL = "ALTER ROLE mallory SUPERUSER; --"
_QUOTED = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`')
_KEYWORDS = {"REVOKE", "GRANT", "CREATE", "ALTER"}


def _assert_no_breakout(remediation: str) -> None:
    """Outside quoted identifiers (which may hold a newline, harmlessly), every line is a
    comment or a statement Savvina writes; none is the injected text."""
    for line in _QUOTED.sub("Q", remediation).splitlines():
        assert line.startswith("--") or line.split()[0] in _KEYWORDS, line
        assert not line.startswith(_EVIL), line


class TestCommentInjection:
    def test_postgresql_names_cannot_break_out_of_a_comment(self) -> None:
        schema, table = f"x\n{_EVIL}", f"y\r{_EVIL}"
        member, grantor = f"g\n{_EVIL}", f"h\r\n{_EVIL}"
        facts = _facts(
            schemas=[schema],
            writes=[_grant(schema, table, grantee=None)]
            + [_grant(f"z\n{_EVIL}", f"t{i:02d}", grantee=None) for i in range(11)],
            predefined_roles=[
                RoleMembership("pg_write_all_data", member, grantor=grantor),
                RoleMembership("pg_read_all_data", member, grantor=grantor),
            ],
            tables=[_table(schema, table, select_grantees=[None])],
        )
        audit = evaluate(
            facts, _privacy(excluded=[f"{schema}.{table}"]), "postgresql", database=f"d\n{_EVIL}"
        )
        assert _codes(audit) == [Finding.WRITE_PRIVILEGES, Finding.READS_EXCLUDED_TABLES]
        for finding in audit.findings:
            _assert_no_breakout(finding.remediation)
        writes = _finding(audit, Finding.WRITE_PRIVILEGES).remediation
        assert f'-- "x?{_EVIL}"."y?{_EVIL}": the role owns it' in writes
        assert f'-- and 1 more tables in "z?{_EVIL}" owned by the role.' in writes
        assert f'every other member of "g?{_EVIL}".' in writes
        # The statements keep the exact quoted names.
        assert f"GRANTED BY {quote_identifier(grantor, 'postgresql')};" in writes
        assert f"GRANT USAGE ON SCHEMA {quote_identifier(schema, 'postgresql')}" in writes
        reads = _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation
        assert f'-- "x?{_EVIL}"."y?{_EVIL}": SELECT comes from membership' in reads  # noqa: S608
        script = dedicated_role_script(
            facts, _privacy(excluded=[f"{schema}.{table}"]), "postgresql", f"d\n{_EVIL}"
        )
        _assert_no_breakout(script)

    def test_mysql_names_cannot_break_out_of_a_comment(self) -> None:
        schema, table = f"x\n{_EVIL}", f"y\r{_EVIL}"
        grant = PrivilegeGrant(schema, None, ["SELECT"], f"r\n{_EVIL}")
        facts = _facts(
            role="u@%",
            has_super=True,
            super_grantees=[f"s\n{_EVIL}"],
            schemas=[schema],
            tables=[_table(schema, table, select_grants=[grant])],
        )
        privacy = _privacy(excluded=[f"{schema}.{table}"])
        audit = evaluate(facts, privacy, "mysql")
        reads = _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation
        assert reads.startswith(f"-- `x?{_EVIL}`.`y?{_EVIL}`: SELECT comes from the grant on")  # noqa: S608
        for finding in audit.findings:
            _assert_no_breakout(finding.remediation)
        script = dedicated_role_script(facts, privacy, "mysql", None)
        assert script.splitlines()[-1] == (
            "-- MySQL cannot revoke one table from a database grant: the excluded tables"
            f" `x?{_EVIL}`.`y?{_EVIL}` stay readable by this account."
        )
        _assert_no_breakout(script)

    def test_every_control_character_reads_as_a_question_mark(self) -> None:
        facts = _facts(writes=[_grant(table="a\tb\x7fc\x00d\x1be")])
        facts.writes[0].grantee = None
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines()[0] == _owner("a?b?c?d?e")


class TestDedicatedRoleScriptExcludedTables:
    def test_postgresql_revokes_an_excluded_table_after_the_grants(self) -> None:
        facts = _facts(schemas=["public", "hr"], tables=[_table("hr", "salaries")])
        privacy = _privacy(excluded=["hr.salaries"])
        lines = dedicated_role_script(facts, privacy, "postgresql", "app").splitlines()
        assert lines[-2] == (
            'ALTER DEFAULT PRIVILEGES IN SCHEMA "public" GRANT SELECT ON TABLES TO savvina_reader;'
        )
        assert lines[-1] == 'REVOKE SELECT ON "hr"."salaries" FROM savvina_reader;'

    def test_an_excluded_table_under_an_excluded_schema_adds_nothing(self) -> None:
        facts = _facts(schemas=["public", "hr"], tables=[_table("hr", "salaries")])
        privacy = PrivacySettings(excluded_schemas=["hr"], excluded_tables=["hr.salaries"])
        assert "REVOKE" not in dedicated_role_script(facts, privacy, "postgresql", "app")

    def test_a_missing_excluded_table_adds_nothing(self) -> None:
        facts = _facts(schemas=["hr"], tables=[_table("hr", "salaries", exists=False)])
        privacy = _privacy(excluded=["hr.salaries"])
        assert "REVOKE" not in dedicated_role_script(facts, privacy, "postgresql", "app")

    def test_a_bare_entry_matching_two_schemas_gives_two_sorted_lines(self) -> None:
        facts = _facts(schemas=["a", "b"], tables=[_table("b", "secrets"), _table("a", "secrets")])
        lines = dedicated_role_script(
            facts, _privacy(excluded=["secrets"]), "postgresql", None
        ).splitlines()
        assert lines[-2:] == [
            'REVOKE SELECT ON "a"."secrets" FROM savvina_reader;',
            'REVOKE SELECT ON "b"."secrets" FROM savvina_reader;',
        ]

    def test_mysql_with_a_database_names_the_excluded_tables_in_it(self) -> None:
        facts = _facts(
            role="u@%",
            schemas=["shop", "other"],
            tables=[_table("shop", "salaries"), _table("other", "x")],
        )
        privacy = _privacy(excluded=["shop.salaries", "other.x"])
        lines = dedicated_role_script(facts, privacy, "mysql", "shop").splitlines()
        assert lines[-2:] == [
            "GRANT SELECT ON `shop`.* TO `savvina_reader`@`%`;",
            "-- MySQL cannot revoke one table from a database grant: the excluded tables"
            " `shop`.`salaries` stay readable by this account.",
        ]

    def test_mysql_without_a_database_names_every_excluded_table(self) -> None:
        facts = _facts(
            role="u@%",
            schemas=["shop", "other"],
            tables=[_table("shop", "salaries"), _table("other", "x")],
        )
        privacy = _privacy(excluded=["shop.salaries", "other.x"])
        lines = dedicated_role_script(facts, privacy, "mysql", None).splitlines()
        assert lines[-1] == (
            "-- MySQL cannot revoke one table from a database grant: the excluded tables"
            " `other`.`x`, `shop`.`salaries` stay readable by this account."
        )

    def test_mysql_adds_nothing_when_no_excluded_table_is_in_the_database(self) -> None:
        facts = _facts(role="u@%", schemas=["shop", "other"], tables=[_table("other", "x")])
        script = dedicated_role_script(facts, _privacy(excluded=["other.x"]), "mysql", "shop")
        assert "MySQL cannot revoke" not in script

    def test_the_superuser_remediation_revokes_the_excluded_table(self) -> None:
        facts = _facts(is_superuser=True, schemas=["hr"], tables=[_table("hr", "salaries")])
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql", database="app")
        remediation = _finding(audit, Finding.SUPERUSER).remediation
        assert remediation.splitlines()[-1] == (
            'REVOKE SELECT ON "hr"."salaries" FROM savvina_reader;'
        )


_WRITE_ALL_NOTE = "-- Membership in pg_write_all_data allows writing every table."


def _read_all_note(tables: str) -> str:
    return (
        f"-- {tables}: SELECT comes from membership in pg_read_all_data;"  # noqa: S608
        " revoking it removes SELECT on every table. To keep Savvina's access, grant SELECT"
        " per schema or per table instead."
    )


def _nested(member: str) -> str:
    return f"-- Revoking it from {member} also removes it from every other member of {member}."


class TestPredefinedRoles:
    def test_pg_write_all_data_alone_is_a_write_finding(self) -> None:
        facts = _facts(predefined_roles=[RoleMembership("pg_write_all_data", "savvina")])
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.tables == [] and f.more_tables == 0
        assert f.remediation.splitlines() == [
            _WRITE_ALL_NOTE,
            'REVOKE pg_write_all_data FROM "savvina";',
        ]

    def test_the_membership_lines_come_before_the_acl_grants(self) -> None:
        facts = _facts(
            predefined_roles=[RoleMembership("pg_write_all_data", "savvina")],
            writes=[_grant()],
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.tables == ["public.orders"]
        assert f.remediation.splitlines() == [
            _WRITE_ALL_NOTE,
            'REVOKE pg_write_all_data FROM "savvina";',
            'REVOKE DELETE, INSERT, UPDATE ON "public"."orders" FROM "savvina";',
        ]

    def test_two_members_get_one_note_and_two_revokes(self) -> None:
        facts = _facts(
            predefined_roles=[
                RoleMembership("pg_write_all_data", "savvina"),
                RoleMembership("pg_write_all_data", "writers"),
                RoleMembership("pg_read_all_data", "readers"),
            ]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            _WRITE_ALL_NOTE,
            'REVOKE pg_write_all_data FROM "savvina";',
            _nested('"writers"'),
            'REVOKE pg_write_all_data FROM "writers";',
        ]

    def test_pg_read_all_data_alone_gives_no_finding_without_a_readable_excluded_table(
        self,
    ) -> None:
        facts = _facts(predefined_roles=[RoleMembership("pg_read_all_data", "savvina")])
        assert evaluate(facts, _privacy(), "postgresql").findings == []

    def test_an_excluded_table_readable_only_through_the_membership(self) -> None:
        facts = _facts(
            predefined_roles=[
                RoleMembership("pg_read_all_data", "savvina"),
                RoleMembership("pg_read_all_data", "readers"),
            ],
            tables=[_table("hr", "salaries"), _table("hr", "reviews")],
        )
        privacy = _privacy(excluded=["hr.salaries", "hr.reviews"])
        f = _finding(evaluate(facts, privacy, "postgresql"), Finding.READS_EXCLUDED_TABLES)
        assert f.tables == ["hr.salaries", "hr.reviews"]
        assert f.remediation.splitlines() == [
            _read_all_note('"hr"."salaries", "hr"."reviews"'),
            'REVOKE pg_read_all_data FROM "savvina";',
            _nested('"readers"'),
            'REVOKE pg_read_all_data FROM "readers";',
        ]

    def test_a_nested_member_gets_the_comment_and_the_role_itself_does_not(self) -> None:
        facts = _facts(
            predefined_roles=[
                RoleMembership("pg_write_all_data", "Writers"),
                RoleMembership("pg_read_all_data", "savvina"),
            ],
            tables=[_table("hr", "salaries")],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        assert _finding(audit, Finding.WRITE_PRIVILEGES).remediation.splitlines() == [
            _WRITE_ALL_NOTE,
            '-- Revoking it from "Writers" also removes it from every other member of "Writers".',
            'REVOKE pg_write_all_data FROM "Writers";',
        ]
        reads = _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation
        assert "also removes it from" not in reads
        assert reads.splitlines()[-1] == 'REVOKE pg_read_all_data FROM "savvina";'

    def test_a_nested_member_of_pg_read_all_data_gets_the_comment(self) -> None:
        facts = _facts(
            predefined_roles=[RoleMembership("pg_read_all_data", "readers")],
            tables=[_table("hr", "salaries")],
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines()[-2:] == [
            _nested('"readers"'),
            'REVOKE pg_read_all_data FROM "readers";',
        ]

    def test_an_acl_grant_and_the_membership_are_both_revoked(self) -> None:
        facts = _facts(
            predefined_roles=[RoleMembership("pg_read_all_data", "savvina")],
            tables=[_table("hr", "salaries", select_grantees=["PUBLIC"])],
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines() == [
            'REVOKE SELECT ON "hr"."salaries" FROM PUBLIC;',
            _read_all_note('"hr"."salaries"'),
            'REVOKE pg_read_all_data FROM "savvina";',
        ]

    def test_the_membership_group_comes_before_the_script(self) -> None:
        facts = _facts(
            predefined_roles=[RoleMembership("pg_read_all_data", "savvina")],
            tables=[_table("hr", "salaries", select_grantees=[None])],
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        lines = f.remediation.splitlines()
        assert lines[:3] == [
            _owner("salaries", "hr"),
            _read_all_note('"hr"."salaries"'),
            'REVOKE pg_read_all_data FROM "savvina";',
        ]
        assert lines[3].startswith("-- Create a read-only role")

    def test_a_grantor_adds_granted_by(self) -> None:
        facts = _facts(
            predefined_roles=[RoleMembership("pg_write_all_data", "savvina", grantor="admin")]
        )
        f = _finding(evaluate(facts, _privacy(), "postgresql"), Finding.WRITE_PRIVILEGES)
        assert f.remediation.splitlines() == [
            _WRITE_ALL_NOTE,
            'REVOKE pg_write_all_data FROM "savvina" GRANTED BY "admin";',
        ]

    def test_one_revoke_per_member_and_grantor(self) -> None:
        facts = _facts(
            predefined_roles=[
                RoleMembership("pg_read_all_data", "readers", grantor="a"),
                RoleMembership("pg_read_all_data", "readers", grantor="b"),
                RoleMembership("pg_read_all_data", "readers", grantor="a"),
                RoleMembership("pg_read_all_data", "readers"),
                RoleMembership("pg_read_all_data", "readers"),
            ],
            tables=[_table("hr", "salaries")],
        )
        f = _finding(
            evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql"),
            Finding.READS_EXCLUDED_TABLES,
        )
        assert f.remediation.splitlines()[1:] == [
            _nested('"readers"'),
            'REVOKE pg_read_all_data FROM "readers" GRANTED BY "a";',
            'REVOKE pg_read_all_data FROM "readers" GRANTED BY "b";',
            'REVOKE pg_read_all_data FROM "readers";',
        ]

    def test_memberships_are_ignored_on_mysql(self) -> None:
        facts = _facts(
            role="u@%",
            predefined_roles=[
                RoleMembership("pg_write_all_data", "u@%"),
                RoleMembership("pg_read_all_data", "u@%"),
            ],
            tables=[_table("hr", "salaries")],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql")
        assert _codes(audit) == [Finding.READS_EXCLUDED_TABLES]
        assert _finding(audit, Finding.READS_EXCLUDED_TABLES).remediation == (
            "REVOKE SELECT ON `hr`.`salaries` FROM `u`@`%`;"
        )

    def test_a_superuser_skips_the_memberships(self) -> None:
        facts = _facts(
            is_superuser=True,
            predefined_roles=[
                RoleMembership("pg_write_all_data", "savvina"),
                RoleMembership("pg_read_all_data", "savvina"),
            ],
            tables=[_table("hr", "salaries")],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "postgresql")
        assert _codes(audit) == [Finding.SUPERUSER]


class TestSuperPrivilege:
    def test_super_alone_is_a_tier_2_finding_that_hides_nothing(self) -> None:
        facts = _facts(
            role="u@%",
            has_super=True,
            writes=[_grant("shop", "orders", ["INSERT"], "u@%")],
            tables=[_table("hr", "salaries", select_grantees=["u@%"])],
        )
        audit = evaluate(facts, _privacy(excluded=["hr.salaries"]), "mysql")
        assert _codes(audit) == [
            Finding.SUPER_PRIVILEGE,
            Finding.WRITE_PRIVILEGES,
            Finding.READS_EXCLUDED_TABLES,
        ]
        f = _finding(audit, Finding.SUPER_PRIVILEGE)
        assert f.remediation == "REVOKE SUPER ON *.* FROM `u`@`%`;"
        assert Finding.SUPER_PRIVILEGE not in TIER_1
        assert has_findings(audit, 2) and not has_findings(audit, 1)

    def test_super_is_revoked_from_every_grantee_that_holds_it(self) -> None:
        facts = _facts(
            role="u@%", has_super=True, super_grantees=["u@%", "admin@%", "dba", "admin@%"]
        )
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.SUPER_PRIVILEGE)
        assert f.remediation.splitlines() == [
            "REVOKE SUPER ON *.* FROM `u`@`%`;",
            "REVOKE SUPER ON *.* FROM `admin`@`%`;",
            "REVOKE SUPER ON *.* FROM `dba`;",
        ]

    def test_super_through_a_role_only_names_the_role(self) -> None:
        facts = _facts(role="u@%", has_super=True, super_grantees=["admin@%"])
        f = _finding(evaluate(facts, _privacy(), "mysql"), Finding.SUPER_PRIVILEGE)
        assert f.remediation == "REVOKE SUPER ON *.* FROM `admin`@`%`;"


class TestSqlFromString:
    def test_lists_the_functions_and_revokes_from_public_and_the_role(self) -> None:
        facts = _facts(sql_from_string_functions=["public.dblink(text)", "public.crosstab(text)"])
        audit = evaluate(facts, _privacy(), "postgresql")
        finding = _finding(audit, Finding.SQL_FROM_STRING)
        assert finding.functions == ["public.dblink(text)", "public.crosstab(text)"]
        assert finding.tables == []
        assert finding.remediation == (
            'REVOKE EXECUTE ON FUNCTION public.dblink(text) FROM PUBLIC, "savvina";\n'
            'REVOKE EXECUTE ON FUNCTION public.crosstab(text) FROM PUBLIC, "savvina";'
        )


class TestRlsEffective:
    @pytest.mark.parametrize(
        ("facts_kwargs", "table_kwargs", "expected"),
        [
            ({"is_superuser": True}, {"rls_enabled": True, "policy": True}, False),
            ({"bypasses_rls": True}, {"rls_enabled": True, "policy": True}, False),
            ({}, {"rls_enabled": True, "policy": True, "is_owner": True}, False),
            ({}, {"rls_enabled": True, "policy": True, "is_owner": True, "rls_forced": True}, True),
            ({}, {"rls_enabled": True, "policy": True}, True),
            ({}, {"rls_enabled": True, "policy": False}, False),
            ({}, {"rls_enabled": False, "policy": True}, False),
            ({}, {"rls_enabled": True, "policy": True, "can_select": False}, False),
            ({}, {"rls_enabled": True, "policy": True, "exists": False}, False),
        ],
    )
    def test_per_filtered_table(self, facts_kwargs, table_kwargs, expected) -> None:
        facts = _facts(tables=[_table(**table_kwargs)], **facts_kwargs)
        audit = evaluate(facts, _privacy(["public.orders"]), "postgresql")
        assert audit.rls_effective == {"public.orders": expected}

    def test_a_filtered_table_without_facts_is_false(self) -> None:
        audit = evaluate(_facts(), _privacy(["public.orders"]), "postgresql")
        assert audit.rls_effective == {"public.orders": False}

    def test_mysql_is_false_for_every_key(self) -> None:
        # RLS flags are PostgreSQL facts; even if set, MySQL never reports RLS as effective.
        facts = _facts(role="u@h", tables=[_table("shop", rls_enabled=True, policy=True)])
        audit = evaluate(facts, _privacy(["shop.orders", "shop.items"]), "mysql")
        assert audit.rls_effective == {"shop.orders": False, "shop.items": False}
        assert audit.rls_in_use is True


class TestHasFindings:
    def test_tier_1_counts_only_while_rls_is_in_use(self) -> None:
        without = evaluate(_facts(is_superuser=True), _privacy(), "postgresql")
        assert has_findings(without, 1) is False
        assert has_findings(without, 2) is True
        with_rls = evaluate(
            _facts(is_superuser=True, tables=[_table(rls_enabled=True)]),
            _privacy(["public.orders"]),
            "postgresql",
        )
        assert has_findings(with_rls, 1) is True
        assert has_findings(with_rls, 2) is False

    def test_no_findings(self) -> None:
        audit = evaluate(_facts(), _privacy(), "postgresql")
        assert has_findings(audit, 1) is False and has_findings(audit, 2) is False

    def test_tier_1_set(self) -> None:
        assert {Finding.SUPERUSER, Finding.BYPASSRLS, Finding.OWNER_WITHOUT_FORCE} == TIER_1


class TestSerialisation:
    def test_round_trip(self) -> None:
        facts = _facts(
            bypasses_rls=True,
            sql_from_string_functions=["public.dblink(text)"],
            tables=[_table(rls_enabled=True, policy=True)],
        )
        audit = evaluate(facts, _privacy(["public.orders"]), "postgresql")
        data = to_dict(audit)
        assert data["status"] == "ok"
        assert data["checked_at"] == audit.checked_at.isoformat()
        assert data["findings"][0]["code"] == "bypassrls"
        assert from_dict(data) == audit

    def test_more_tables_round_trips_and_defaults_to_zero(self) -> None:
        audit = evaluate(
            _facts(writes=[_grant(table=f"t{i:03d}", grantee="PUBLIC") for i in range(51)]),
            _privacy(),
            "postgresql",
        )
        data = to_dict(audit)
        assert data["findings"][0]["more_tables"] == 1
        back = from_dict(data)
        assert back is not None and back.findings[0].more_tables == 1
        del data["findings"][0]["more_tables"]
        back = from_dict(data)
        assert back is not None and back.findings[0].more_tables == 0

    @pytest.mark.parametrize("value", [-1, True, "3"])
    def test_a_more_tables_that_is_not_a_count_reads_zero(self, value) -> None:
        item = {"code": "write_privileges", "more_tables": value}
        data = {"checked_at": "2026-10-29T10:00:00+00:00", "status": "ok", "findings": [item]}
        audit = from_dict(data)
        assert audit is not None
        assert audit.findings[0].more_tables == 0

    def test_unknown_code_is_dropped_and_missing_lists_read_empty(self) -> None:
        data = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "role": "r",
            "status": "ok",
            "findings": [
                {"code": "superuser", "remediation": "x"},
                {"code": "from_the_future", "remediation": "y"},
                "not a finding",
            ],
        }
        audit = from_dict(data)
        assert audit is not None
        assert audit.error is None
        assert audit.rls_in_use is False
        assert audit.rls_effective == {}
        assert audit.findings == [AuditFinding(Finding.SUPERUSER, remediation="x")]
        assert audit.checked_at == datetime(2026, 10, 29, 10, tzinfo=UTC)

    @pytest.mark.parametrize("findings", [5, True, "x", {"code": "superuser"}])
    def test_findings_that_is_not_a_list_reads_empty(self, findings) -> None:
        data = {"checked_at": "2026-10-29T10:00:00+00:00", "status": "ok", "findings": findings}
        audit = from_dict(data)
        assert audit is not None
        assert audit.findings == []

    def test_rls_effective_is_fail_closed_and_string_keyed(self) -> None:
        data = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "status": "ok",
            "rls_effective": {"a.t": True, "b.t": "false", "c.t": 1, "d.t": "true", 5: True},
        }
        audit = from_dict(data)
        assert audit is not None
        assert audit.rls_effective == {"a.t": True, "b.t": False, "c.t": False, "d.t": False}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("false", False),
            ("true", False),
            (1, False),
            (None, False),
        ],
    )
    def test_rls_in_use_is_true_only_for_a_json_true(self, value, expected) -> None:
        data = {"checked_at": "2026-10-29T10:00:00+00:00", "status": "ok", "rls_in_use": value}
        audit = from_dict(data)
        assert audit is not None
        assert audit.rls_in_use is expected

    @pytest.mark.parametrize("code_field", ["tables", "functions"])
    def test_non_string_list_items_are_dropped(self, code_field) -> None:
        item = {"code": "sql_from_string", code_field: ["a", None, 3, {"x": 1}, "b"]}
        data = {"checked_at": "2026-10-29T10:00:00+00:00", "status": "ok", "findings": [item]}
        audit = from_dict(data)
        assert audit is not None
        assert getattr(audit.findings[0], code_field) == ["a", "b"]

    def test_naive_checked_at_reads_as_utc(self) -> None:
        audit = from_dict({"checked_at": "2026-10-29T10:00:00", "status": "ok"})
        assert audit is not None
        assert audit.checked_at == datetime(2026, 10, 29, 10, tzinfo=UTC)

    def test_non_string_text_fields_read_empty(self) -> None:
        data = {
            "checked_at": "2026-10-29T10:00:00+00:00",
            "status": "ok",
            "role": 5,
            "error": ["x"],
            "findings": [{"code": "superuser", "remediation": {"x": 1}}],
        }
        audit = from_dict(data)
        assert audit is not None
        assert audit.role == ""
        assert audit.error is None
        assert audit.findings[0].remediation == ""
        assert from_dict({**data, "error": "boom", "role": "r"}).error == "boom"

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "text",
            [],
            {},
            {"checked_at": "nope", "status": "ok"},
            {"checked_at": "2026-10-29T10:00:00+00:00", "status": "weird"},
        ],
    )
    def test_malformed_reads_none(self, data) -> None:
        assert from_dict(data) is None

    def test_unavailable(self) -> None:
        audit = unavailable("", "connection refused")
        assert audit.status == "unavailable"
        assert audit.error == "connection refused"
        assert audit.role == ""
        assert audit.findings == [] and audit.rls_effective == {} and audit.rls_in_use is False
        assert from_dict(to_dict(audit)) == audit
