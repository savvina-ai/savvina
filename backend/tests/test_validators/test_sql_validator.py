# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for SQL query validation logic (base + PostgreSQL-specific)."""

import pytest

from app.datasources.validators.base_sql_validator import BaseSQLValidator
from app.datasources.validators.postgresql_validator import PostgreSQLValidator

# ── Base validator ────────────────────────────────────────────────────────────


class TestBaseSQLValidatorAllowed:
    def setup_method(self):
        self.v = BaseSQLValidator()

    def test_simple_select_passes(self):
        assert self.v.validate("SELECT * FROM users").is_valid

    def test_select_with_where(self):
        assert self.v.validate("SELECT id, name FROM users WHERE active = true").is_valid

    def test_select_with_join(self):
        q = "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        assert self.v.validate(q).is_valid

    def test_cte_passes(self):
        q = "WITH cte AS (SELECT id FROM users) SELECT * FROM cte"
        assert self.v.validate(q).is_valid

    def test_complex_cte_passes(self):
        q = """
        WITH monthly AS (
            SELECT DATE_TRUNC('month', created_at) AS month, COUNT(*) AS cnt
            FROM orders
            GROUP BY 1
        )
        SELECT month, cnt FROM monthly ORDER BY month
        """
        assert self.v.validate(q).is_valid

    def test_select_with_subquery(self):
        q = "SELECT * FROM (SELECT id, name FROM users) sub WHERE name = 'x'"
        assert self.v.validate(q).is_valid

    def test_select_with_aggregation(self):
        q = "SELECT COUNT(*), SUM(amount) FROM orders GROUP BY status"
        assert self.v.validate(q).is_valid


class TestBaseSQLValidatorCompoundSelects:
    """sqlparse returns None (not 'SELECT') for compound and parenthesized SELECTs.
    These are valid read-only queries and must pass validation.
    """

    def setup_method(self):
        self.v = BaseSQLValidator()

    def test_union_passes(self):
        q = "SELECT id FROM users UNION SELECT id FROM admins"
        assert self.v.validate(q).is_valid

    def test_intersect_passes(self):
        q = "SELECT id FROM active_users INTERSECT SELECT id FROM paying_users"
        assert self.v.validate(q).is_valid

    def test_except_passes(self):
        q = "SELECT id FROM users EXCEPT SELECT id FROM banned_users"
        assert self.v.validate(q).is_valid

    def test_parenthesized_select_passes(self):
        q = "(SELECT id, name FROM users ORDER BY name)"
        assert self.v.validate(q).is_valid

    def test_union_with_dml_still_blocked(self):
        # DML embedded in a compound query must still be caught by keyword scan
        q = "SELECT 1 UNION DELETE FROM users"
        assert not self.v.validate(q).is_valid


class TestBaseSQLValidatorBlocked:
    def setup_method(self):
        self.v = BaseSQLValidator()

    @pytest.mark.parametrize(
        "query,label",
        [
            ("INSERT INTO users VALUES (1, 'a')", "INSERT"),
            ("UPDATE users SET name = 'x'", "UPDATE"),
            ("DELETE FROM users WHERE id = 1", "DELETE"),
            ("DROP TABLE users", "DROP"),
            ("ALTER TABLE users ADD COLUMN foo INT", "ALTER"),
            ("CREATE TABLE foo (id INT)", "CREATE"),
            ("TRUNCATE TABLE users", "TRUNCATE"),
            ("GRANT SELECT ON users TO role", "GRANT"),
            ("REVOKE SELECT ON users FROM role", "REVOKE"),
        ],
    )
    def test_dml_ddl_blocked(self, query, label):
        result = self.v.validate(query)
        assert not result.is_valid, f"{label} should be rejected"

    def test_empty_query_rejected(self):
        assert not self.v.validate("").is_valid

    def test_whitespace_only_rejected(self):
        assert not self.v.validate("   \n\t  ").is_valid

    def test_multi_statement_rejected(self):
        assert not self.v.validate("SELECT 1; SELECT 2").is_valid

    def test_into_outfile_rejected(self):
        assert not self.v.validate("SELECT * FROM users INTO OUTFILE '/tmp/x'").is_valid

    def test_into_dumpfile_rejected(self):
        assert not self.v.validate("SELECT * FROM users INTO DUMPFILE '/tmp/x'").is_valid

    def test_invalid_result_has_no_sanitized_query(self):
        r = self.v.validate("DROP TABLE users")
        assert not r.is_valid
        assert r.sanitized_query is None

    def test_invalid_result_has_error_message(self):
        r = self.v.validate("DROP TABLE users")
        assert r.error_message is not None
        assert len(r.error_message) > 0


class TestBaseSQLValidatorLimitHandling:
    def setup_method(self):
        self.v = BaseSQLValidator()

    def test_adds_limit_when_missing(self):
        r = self.v.validate("SELECT * FROM users")
        assert r.is_valid
        assert "LIMIT" in r.sanitized_query.upper()

    def test_uses_default_limit_of_1000(self):
        r = self.v.validate("SELECT * FROM users")
        assert "LIMIT 1000" in r.sanitized_query

    def test_custom_default_limit(self):
        r = self.v.validate("SELECT * FROM users", default_limit=500)
        assert "LIMIT 500" in r.sanitized_query

    def test_existing_limit_preserved(self):
        q = "SELECT * FROM users LIMIT 10"
        r = self.v.validate(q)
        assert r.is_valid
        assert r.sanitized_query == q  # unchanged

    def test_trailing_semicolon_stripped_before_limit(self):
        r = self.v.validate("SELECT * FROM users;")
        assert r.is_valid
        assert ";" not in r.sanitized_query
        assert "LIMIT" in r.sanitized_query.upper()

    def test_valid_result_has_sanitized_query(self):
        r = self.v.validate("SELECT 1")
        assert r.is_valid
        assert r.sanitized_query is not None


# ── PostgreSQL-specific validator ─────────────────────────────────────────────


class TestPostgreSQLValidatorAllowed:
    def setup_method(self):
        self.v = PostgreSQLValidator()

    def test_simple_select_passes(self):
        assert self.v.validate("SELECT id FROM users").is_valid

    def test_ilike_passes(self):
        assert self.v.validate("SELECT * FROM users WHERE name ILIKE '%john%'").is_valid

    def test_window_function_passes(self):
        q = "SELECT id, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY name) FROM employees"
        assert self.v.validate(q).is_valid

    def test_cast_passes(self):
        assert self.v.validate("SELECT id::text, created_at::date FROM orders").is_valid

    def test_date_trunc_passes(self):
        q = "SELECT DATE_TRUNC('month', created_at), COUNT(*) FROM orders GROUP BY 1"
        assert self.v.validate(q).is_valid

    def test_array_agg_passes(self):
        assert self.v.validate("SELECT ARRAY_AGG(name) FROM users").is_valid

    def test_cte_passes(self):
        q = "WITH t AS (SELECT 1 AS n) SELECT n FROM t"
        assert self.v.validate(q).is_valid


class TestPostgreSQLValidatorBlockedFunctions:
    def setup_method(self):
        self.v = PostgreSQLValidator()

    @pytest.mark.parametrize(
        "func,query",
        [
            ("pg_sleep", "SELECT pg_sleep(10)"),
            ("pg_terminate_backend", "SELECT pg_terminate_backend(12345)"),
            ("pg_cancel_backend", "SELECT pg_cancel_backend(12345)"),
            ("pg_read_file", "SELECT pg_read_file('/etc/passwd')"),
            ("pg_read_binary_file", "SELECT pg_read_binary_file('/etc/passwd', 0, 100)"),
            ("pg_ls_dir", "SELECT * FROM pg_ls_dir('/tmp')"),
            ("lo_export", "SELECT lo_export(1234, '/tmp/out')"),
            ("lo_import", "SELECT lo_import('/tmp/file')"),
            ("dblink", "SELECT * FROM dblink('host=x', 'SELECT 1') AS t(id INT)"),
            ("dblink_exec", "SELECT dblink_exec('host=x', 'DROP TABLE users')"),
        ],
    )
    def test_dangerous_function_blocked(self, func, query):
        result = self.v.validate(query)
        assert not result.is_valid, f"{func}() should be blocked"

    def test_blocked_function_error_message_names_function(self):
        r = self.v.validate("SELECT pg_sleep(5)")
        assert "pg_sleep" in r.error_message


class TestPostgreSQLValidatorBlockedPatterns:
    def setup_method(self):
        self.v = PostgreSQLValidator()

    def test_copy_to_blocked(self):
        # COPY is a top-level command — caught by base (not SELECT)
        assert not self.v.validate("COPY users TO '/tmp/dump.csv'").is_valid

    def test_set_role_blocked(self):
        # Not a SELECT — caught by base
        assert not self.v.validate("SET ROLE admin").is_valid

    def test_set_session_blocked(self):
        # Not a SELECT — caught by base
        assert not self.v.validate("SET SESSION AUTHORIZATION admin").is_valid

    def test_dml_still_blocked(self):
        """Ensure base validator rules still apply in the PG subclass."""
        assert not self.v.validate("DELETE FROM users").is_valid
        assert not self.v.validate("INSERT INTO users VALUES (1)").is_valid

    def test_multi_statement_still_blocked(self):
        assert not self.v.validate("SELECT 1; SELECT 2").is_valid
