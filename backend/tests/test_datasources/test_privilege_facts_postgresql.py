# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``PostgreSQLDataSource.audit_privileges``: the catalog facts about the connection role,
read with a mocked asyncpg."""

from __future__ import annotations

from collections.abc import Sequence
import re
from unittest.mock import AsyncMock

import pytest

from app.datasources.adapters.postgresql import (
    _SQL_FROM_STRING_FUNCTIONS,
    _WRITES_CAP,
    PostgreSQLDataSource,
)
from app.datasources.base import BaseDataSource
from app.datasources.models import (
    PrivilegeFacts,
    PrivilegeGrant,
    RoleMembership,
    TablePrivilegeFacts,
)
from app.services.row_filter_rewrite import _UNFILTERABLE_FUNCTION
from tests.conftest import FakeRecord, MockConnection, MockPool


def _adapter() -> tuple[PostgreSQLDataSource, MockConnection]:
    adapter = PostgreSQLDataSource()
    conn = MockConnection()
    adapter._pool = MockPool(conn)
    return adapter, conn


class TestContract:
    def test_audit_privileges_is_abstract_on_the_base(self) -> None:
        assert "audit_privileges" in BaseDataSource.__abstractmethods__

    def test_facts_default_to_nothing_granted(self) -> None:
        facts = PrivilegeFacts(role="r")
        assert facts.is_superuser is False
        assert facts.has_super is False
        assert facts.bypasses_rls is False
        assert facts.writes == [] and facts.writes_truncated is False
        assert facts.predefined_roles == []
        assert facts.schemas == []
        assert facts.sql_from_string_functions == []
        assert facts.tables == []
        missing = TablePrivilegeFacts(schema_name=None, table_name="t", exists=False)
        assert missing.can_select is False and missing.rls_enabled is False
        assert missing.select_grants == []
        assert missing.key is None
        assert TablePrivilegeFacts("s", "t", True).key == "s.t"

    def test_a_grant_knows_its_table_key(self) -> None:
        assert PrivilegeGrant("s", "t", ["INSERT"], "PUBLIC").key == "s.t"
        assert PrivilegeGrant("s", None, ["INSERT"], "u@%").key is None
        assert PrivilegeGrant(None, None, ["FILE"], "u@%").key is None
        assert PrivilegeGrant("s", "t", ["UPDATE"], None, ["a"]).columns == ["a"]


def _role_row(role: str = "savvina", superuser: bool = False, bypass: bool = False) -> FakeRecord:
    return FakeRecord({"rolname": role, "rolsuper": superuser, "rolbypassrls": bypass})


def _table_row(
    schema: str = "public",
    table: str = "orders",
    *,
    is_owner: bool = False,
    rls_enabled: bool = False,
    rls_forced: bool = False,
    can_select: bool = True,
    can_write: bool = False,
    policy: bool = False,
    select_grantees: list[str] | None = None,
) -> FakeRecord:
    return FakeRecord(
        {
            "schema_name": schema,
            "table_name": table,
            "is_owner": is_owner,
            "rls_enabled": rls_enabled,
            "rls_forced": rls_forced,
            "can_select": can_select,
            "can_write": can_write,
            "select_policy_applies": policy,
            "select_grantees": select_grantees,
        }
    )


def _write_row(
    schema: str = "public",
    table: str = "orders",
    grantee: str | None = "savvina",
    privileges: Sequence[str] = ("DELETE", "INSERT", "UPDATE"),
    columns: Sequence[str] | None = None,
) -> FakeRecord:
    return FakeRecord(
        {
            "schema_name": schema,
            "table_name": table,
            "grantee": grantee,
            "privileges": list(privileges),
            "columns": list(columns) if columns is not None else None,
        }
    )


class TestAuditPrivileges:
    async def test_runs_in_a_read_only_transaction_under_the_timeout(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[], [], [], [], []]
        await adapter.audit_privileges([("public", "orders")], timeout=7)
        assert conn.transaction_kwargs == {"readonly": True}
        assert [c.args[0] for c in conn.execute.call_args_list] == [
            "SET LOCAL statement_timeout = 7000"
        ]

    async def test_role_facts(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row("admin", superuser=True, bypass=True))
        conn.fetch.side_effect = [[], []]  # schemas, functions
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.role == "admin"
        assert facts.is_superuser is True
        assert facts.bypasses_rls is True
        assert facts.writes == [] and facts.writes_truncated is False
        # No table asked for: the table query is skipped; a superuser skips the write query;
        # the schema and function queries still run.
        assert conn.fetch.await_count == 2

    async def test_one_table_query_carries_every_pair_as_arrays(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [_table_row(), _table_row("archive", "orders")],
            [],
            [],
            [],
            [],
        ]
        await adapter.audit_privileges(
            [("public", "orders"), (None, "orders"), ("public", "orders"), ("s", "Orders")],
            timeout=7,
        )
        table_call = conn.fetch.await_args_list[0]
        sql = table_call.args[0]
        assert "pg_catalog.pg_class" in sql and "pg_catalog.pg_policies" in sql
        assert "CASE WHEN r.name = 'public' THEN true" in sql
        assert "pg_has_role(current_user, r.name, 'USAGE')" in sql
        # The owner by oid; else, for a non-superuser, a role whose privileges it inherits
        # (pg_has_role answers true for every role when asked about a superuser).
        assert (
            "CASE WHEN c.relowner = me.oid THEN true"
            " WHEN me.rolsuper THEN false"
            " ELSE pg_has_role(current_user, c.relowner, 'USAGE') END AS is_owner"
        ) in " ".join(sql.split())
        assert "pg_catalog.pg_roles me ON me.rolname = current_user" in " ".join(sql.split())
        assert "has_schema_privilege(current_user, c.relnamespace, 'USAGE')" in sql
        assert "has_table_privilege(current_user, c.oid, 'INSERT, UPDATE, DELETE')" in sql
        assert "NOT IN ('pg_catalog', 'information_schema')" in sql
        assert "NOT LIKE 'pg\\_toast%'" in sql and "NOT LIKE 'pg\\_temp%'" in sql
        # Duplicates read once; the qualified pairs as two parallel arrays, the bare names
        # as the third.
        assert table_call.args[1:] == (["public", "s"], ["orders", "Orders"], ["orders"])

    async def test_rows_map_to_the_dataclass_and_a_missing_table_reads_exists_false(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [
                _table_row(
                    is_owner=True,
                    rls_enabled=True,
                    rls_forced=True,
                    can_write=True,
                    policy=True,
                ),
                _table_row("archive", "audit_log", can_select=False),
            ],
            [],
            [],
            [],
            [],
        ]
        facts = await adapter.audit_privileges(
            [("public", "orders"), (None, "audit_log"), ("public", "nosuch"), (None, "ghost")],
            timeout=7,
        )
        assert facts.tables == [
            TablePrivilegeFacts(
                "public",
                "orders",
                True,
                can_select=True,
                can_write=True,
                is_owner=True,
                rls_enabled=True,
                rls_forced=True,
                select_policy_applies=True,
                select_grants=[PrivilegeGrant("public", "orders", ["SELECT"], None)],
            ),
            TablePrivilegeFacts("archive", "audit_log", True, can_select=False),
            TablePrivilegeFacts("public", "nosuch", False),
            TablePrivilegeFacts(None, "ghost", False),
        ]

    async def test_a_bare_name_found_by_a_qualified_pair_is_not_missing(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[_table_row()], [], [], [], []]
        facts = await adapter.audit_privileges([("public", "orders"), (None, "orders")], timeout=7)
        assert [(t.schema_name, t.table_name, t.exists) for t in facts.tables] == [
            ("public", "orders", True)
        ]

    async def test_a_bare_name_in_two_schemas_yields_two_rows_and_no_missing_entry(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [_table_row("archive", "audit_log"), _table_row("public", "audit_log")],
            [],
            [],
            [],
            [],
        ]
        facts = await adapter.audit_privileges([(None, "audit_log")], timeout=7)
        assert facts.tables == [
            TablePrivilegeFacts("archive", "audit_log", True, can_select=True),
            TablePrivilegeFacts("public", "audit_log", True, can_select=True),
        ]

    async def test_sql_from_string_functions_outside_pg_catalog(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [],
            [],
            [],
            [
                FakeRecord({"signature": "public.dblink(text)"}),
                FakeRecord({"signature": "public.crosstab(text)"}),
            ],
        ]
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.sql_from_string_functions == ["public.dblink(text)", "public.crosstab(text)"]
        call = conn.fetch.await_args_list[3]
        assert "n.nspname <> 'pg_catalog'" in call.args[0]
        assert "has_function_privilege(current_user, p.oid, 'EXECUTE')" in call.args[0]
        assert "format('%I.%I(%s)'" in call.args[0]
        pattern = call.args[1]
        for name in (
            "dblink",
            "dblink_exec",
            "crosstab",
            "crosstab4",
            "connectby",
            "query_to_xml",
            "ts_stat",
            "ts_rewrite",
        ):
            assert re.match(pattern, name, re.IGNORECASE), name
        assert not re.match(pattern, "dbl", re.IGNORECASE)

    async def test_a_role_row_that_is_missing_reads_an_empty_role(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch.side_effect = [[], [], [], []]  # writes, predefined, schemas, functions
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.role == "" and facts.is_superuser is False

    async def test_a_driver_exception_passes_through(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("permission denied for table pg_roles"))
        with pytest.raises(RuntimeError, match="pg_roles"):
            await adapter.audit_privileges([], timeout=7)

    async def test_not_connected_raises(self) -> None:
        with pytest.raises(RuntimeError):
            await PostgreSQLDataSource().audit_privileges([], timeout=7)


class TestWriteGrants:
    async def test_the_write_query_reads_every_non_system_table_through_aclexplode(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[], [], [], []]
        await adapter.audit_privileges([], timeout=7)
        sql = " ".join(conn.fetch.await_args_list[0].args[0].split())
        # Materialized views are left out: no INSERT/UPDATE/DELETE runs against one.
        assert "c.relkind IN ('r', 'p', 'v', 'f')" in sql
        assert "aclexplode(r.relacl)" in sql and "aclexplode(att.attacl)" in sql
        assert "a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')" in sql
        assert "a.privilege_type IN ('INSERT', 'UPDATE')" in sql
        assert "a.grantee = 0 OR pg_has_role(current_user, a.grantee, 'USAGE')" in sql
        assert "NOT IN ('pg_catalog', 'information_schema')" in sql
        assert "NOT LIKE 'pg\\_toast%'" in sql and "NOT LIKE 'pg\\_temp%'" in sql
        assert "NOT att.attisdropped" in sql
        assert "pg_has_role(current_user, r.relowner, 'USAGE')" in sql
        # The owner's own ACL entry is ownership (the NULL-grantee row), not a grantee: both
        # ACL branches leave it out.
        assert sql.count("a.grantee <> r.relowner") == 2
        assert "array_agg(column_name ORDER BY column_order)" in sql
        assert sql.endswith(f"LIMIT {_WRITES_CAP + 1}")

    async def test_a_superuser_skips_the_write_query(self) -> None:
        # pg_has_role answers true for every role when asked about a superuser, and evaluate
        # skips tier 2 for it anyway: the scan over every table would be wasted.
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row("admin", superuser=True))
        conn.fetch.side_effect = [[], []]  # schemas, functions
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.writes == [] and facts.writes_truncated is False
        assert conn.fetch.await_count == 2

    async def test_rows_fold_into_grants_and_mark_the_audited_tables_writable(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [_table_row(can_write=False)],
            [
                _write_row(grantee=None),
                _write_row(grantee="PUBLIC", privileges=["INSERT"]),
                _write_row(table="items", privileges=["UPDATE"], columns=["qty"]),
            ],
            [],
            [],
            [],
        ]
        facts = await adapter.audit_privileges([("public", "orders")], timeout=7)
        assert facts.writes == [
            PrivilegeGrant("public", "orders", ["DELETE", "INSERT", "UPDATE"], None),
            PrivilegeGrant("public", "orders", ["INSERT"], "PUBLIC"),
            PrivilegeGrant("public", "items", ["UPDATE"], "savvina", ["qty"]),
        ]
        assert facts.writes_truncated is False
        assert facts.tables[0].can_write is True  # a grant in writes makes the table writable

    async def test_five_hundred_and_one_rows_set_truncated_and_keep_five_hundred(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[_write_row(table=f"t{i}") for i in range(501)], [], [], []]
        facts = await adapter.audit_privileges([], timeout=7)
        assert len(facts.writes) == 500 and facts.writes_truncated is True


class TestPredefinedRoles:
    async def test_the_query_reads_the_inherited_memberships_of_both_roles(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[], [], [], []]  # writes, predefined, schemas, functions
        await adapter.audit_privileges([], timeout=7)
        sql = " ".join(conn.fetch.await_args_list[1].args[0].split())
        assert "pg_catalog.pg_auth_members" in sql
        assert "p.rolname IN ('pg_read_all_data', 'pg_write_all_data')" in sql
        # The member is the role or one it inherits; the membership edge itself is inherited
        # (PG16 per-edge inherit_option, read through to_jsonb so PG14/15 do not error).
        assert "pg_has_role(current_user, m.oid, 'USAGE')" in sql
        assert "COALESCE((to_jsonb(am) ->> 'inherit_option')::boolean, m.rolinherit)" in sql
        assert "pg_has_role(current_user, p.oid" not in sql
        # The grantor a REVOKE ... GRANTED BY must name; NULL for the bootstrap superuser.
        assert "g.oid = am.grantor" in sql
        assert "CASE WHEN am.grantor = 10 THEN NULL ELSE g.rolname END AS grantor" in sql
        # One row per (role, member, grantor): several grantors each need their REVOKE.
        assert sql.startswith("SELECT DISTINCT p.rolname AS role, m.rolname AS member,")
        assert sql.endswith("ORDER BY 1, 2, 3 NULLS FIRST")

    async def test_rows_fold_into_memberships_in_order(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [],
            [
                FakeRecord({"role": "pg_read_all_data", "member": "readers", "grantor": None}),
                FakeRecord({"role": "pg_write_all_data", "member": "savvina", "grantor": "adm"}),
            ],
            [],
            [],
        ]
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.predefined_roles == [
            RoleMembership("pg_read_all_data", "readers", None),
            RoleMembership("pg_write_all_data", "savvina", "adm"),
        ]

    async def test_a_superuser_skips_the_predefined_query(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row("admin", superuser=True))
        conn.fetch.side_effect = [[], []]  # schemas, functions
        facts = await adapter.audit_privileges([], timeout=7)
        assert facts.predefined_roles == []
        assert not any("pg_auth_members" in c.args[0] for c in conn.fetch.await_args_list)


class TestSchemasAndSelectGrants:
    async def test_schemas_are_read_without_the_system_ones(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [],
            [],
            [FakeRecord({"nspname": "hr"}), FakeRecord({"nspname": "public"})],
            [],
        ]
        facts = await adapter.audit_privileges([], timeout=7)
        sql = " ".join(conn.fetch.await_args_list[2].args[0].split())
        assert "pg_catalog.pg_namespace" in sql
        assert "NOT IN ('pg_catalog', 'information_schema')" in sql
        assert "NOT LIKE 'pg\\_toast%'" in sql and "NOT LIKE 'pg\\_temp%'" in sql
        assert facts.schemas == ["hr", "public"]

    async def test_select_grants_list_the_grantees_and_ownership_first(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [
            [
                _table_row(is_owner=True, select_grantees=["PUBLIC", "readers"]),
                _table_row(table="items", select_grantees=None),
                _table_row(table="none", can_select=False, select_grantees=["x"]),
            ],
            [],
            [],
            [],
            [],
        ]
        facts = await adapter.audit_privileges(
            [("public", "orders"), ("public", "items"), ("public", "none")], timeout=7
        )
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("public", "orders", ["SELECT"], None),
            PrivilegeGrant("public", "orders", ["SELECT"], "PUBLIC"),
            PrivilegeGrant("public", "orders", ["SELECT"], "readers"),
        ]
        assert facts.tables[1].select_grants == []
        assert facts.tables[2].select_grants == []  # not readable: no path

    async def test_a_superuser_owner_has_no_ownership_entry(self) -> None:
        # A superuser's bypass is its own finding: ownership is not listed as a SELECT path.
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row("admin", superuser=True))
        conn.fetch.side_effect = [
            [_table_row(is_owner=True, select_grantees=["PUBLIC"])],
            [],  # schemas
            [],  # functions
        ]
        facts = await adapter.audit_privileges([("public", "orders")], timeout=7)
        assert facts.tables[0].select_grants == [
            PrivilegeGrant("public", "orders", ["SELECT"], "PUBLIC")
        ]

    async def test_the_table_query_aggregates_the_select_grantees(self) -> None:
        adapter, conn = _adapter()
        conn.fetchrow = AsyncMock(return_value=_role_row())
        conn.fetch.side_effect = [[], [], [], [], []]
        await adapter.audit_privileges([("public", "orders")], timeout=7)
        sql = " ".join(conn.fetch.await_args_list[0].args[0].split())
        assert "aclexplode(c.relacl)" in sql
        assert "CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE g.rolname END" in sql
        assert "a.privilege_type = 'SELECT'" in sql
        assert "a.grantee <> c.relowner" in sql  # the owner's own entry is not a grantee
        assert "AS select_grantees" in sql


class TestDenyListParity:
    def test_the_adapter_and_the_rewriter_deny_the_same_functions(self) -> None:
        """Catch drift when one of the two function lists is edited and not the other."""
        assert _UNFILTERABLE_FUNCTION.pattern.replace("?:", "") == _SQL_FROM_STRING_FUNCTIONS
