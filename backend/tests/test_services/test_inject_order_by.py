# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _inject_order_by (SEC-1 — SQL injection via sort column).

Covers:
  - Normal happy-path injection for both dialects
  - Quote-char escaping (core SEC-1 fix)
  - Rejection of invalid sort direction
  - Existing ORDER BY replacement
  - LIMIT hoisting
"""

import pytest

from app.services.execution import _inject_order_by

# ── helpers ───────────────────────────────────────────────────────────────────

BASE_PG = "SELECT id, name FROM users"
BASE_MY = "SELECT id, name FROM users"
BASE_LIMIT = "SELECT id, name FROM users LIMIT 100"
BASE_ORDER = "SELECT id, name FROM users ORDER BY id ASC"
BASE_ORDER_LIMIT = "SELECT id, name FROM users ORDER BY id ASC LIMIT 50"


# ── happy-path injection ──────────────────────────────────────────────────────


class TestHappyPath:
    def test_postgres_appends_order_by(self):
        result = _inject_order_by(BASE_PG, "name", "ASC", "PostgreSQL")
        assert 'ORDER BY "name" ASC' in result
        assert result.startswith("SELECT")

    def test_mysql_appends_order_by(self):
        result = _inject_order_by(BASE_MY, "name", "ASC", "MySQL")
        assert "ORDER BY `name` ASC" in result

    def test_desc_direction(self):
        result = _inject_order_by(BASE_PG, "created_at", "DESC", "PostgreSQL")
        assert 'ORDER BY "created_at" DESC' in result

    def test_direction_lowercased_input_normalised(self):
        """Callers may pass lowercase; the function should upper-case it."""
        result = _inject_order_by(BASE_PG, "id", "asc", "PostgreSQL")
        assert 'ORDER BY "id" ASC' in result

    def test_other_dialect_uses_double_quote(self):
        result = _inject_order_by(BASE_PG, "col", "ASC", "SQLite")
        assert 'ORDER BY "col" ASC' in result


# ── LIMIT hoisting ────────────────────────────────────────────────────────────


class TestLimitHoisting:
    def test_order_by_inserted_before_limit(self):
        result = _inject_order_by(BASE_LIMIT, "name", "ASC", "PostgreSQL")
        order_pos = result.upper().index("ORDER BY")
        limit_pos = result.upper().index("LIMIT")
        assert order_pos < limit_pos, "ORDER BY must come before LIMIT"

    def test_limit_preserved_verbatim(self):
        result = _inject_order_by(BASE_LIMIT, "name", "ASC", "PostgreSQL")
        assert "LIMIT 100" in result


# ── existing ORDER BY replacement ────────────────────────────────────────────


class TestReplacement:
    def test_replaces_existing_order_by(self):
        result = _inject_order_by(BASE_ORDER, "name", "DESC", "PostgreSQL")
        assert result.upper().count("ORDER BY") == 1
        assert 'ORDER BY "name" DESC' in result

    def test_replaces_existing_order_by_with_limit(self):
        result = _inject_order_by(BASE_ORDER_LIMIT, "name", "ASC", "PostgreSQL")
        assert result.upper().count("ORDER BY") == 1
        assert "LIMIT 50" in result
        order_pos = result.upper().index("ORDER BY")
        limit_pos = result.upper().index("LIMIT")
        assert order_pos < limit_pos


# ── SEC-1: quote-character escaping ──────────────────────────────────────────


class TestQuoteEscaping:
    """Core SEC-1 fix: embedded quote chars in column names must be escaped,
    not passed through raw into the SQL string."""

    def test_postgres_escapes_embedded_double_quote(self):
        """A column named   total"cost   must produce   "total""cost"   not break SQL."""
        result = _inject_order_by(BASE_PG, 'total"cost', "ASC", "PostgreSQL")
        # The doubled quote is the SQL-standard escape — it stays inside the identifier
        assert '"total""cost"' in result
        # Must NOT appear as a raw un-doubled quote mid-identifier (injection vector)
        assert 'total"cost"' not in result.replace('"total""cost"', "")

    def test_mysql_escapes_embedded_backtick(self):
        """A column named   col`name   must produce   `col``name`   not break SQL."""
        result = _inject_order_by(BASE_MY, "col`name", "ASC", "MySQL")
        assert "`col``name`" in result

    def test_postgres_double_quote_injection_attempt_neutralised(self):
        """Simulate an attacker-crafted column: id" UNION SELECT password--
        After escaping this must not produce runnable UNION SELECT."""
        malicious = 'id" UNION SELECT password FROM users--'
        result = _inject_order_by(BASE_PG, malicious, "ASC", "PostgreSQL")
        # Whole string must be wrapped inside a double-quoted identifier
        assert result.upper().count("UNION SELECT") == 0 or (
            # If somehow present it must be inside the quoted identifier context
            '"id"" UNION SELECT password FROM users--"' in result
        )
        # Simpler check: the raw un-escaped fragment must not appear outside quotes
        assert 'id" UNION' not in result

    def test_mysql_backtick_injection_attempt_neutralised(self):
        """Simulate: column = `id` UNION SELECT password--"""
        malicious = "`id` UNION SELECT password--"
        result = _inject_order_by(BASE_MY, malicious, "ASC", "MySQL")
        # After escaping the backticks, no raw UNION SELECT should be injected
        assert "UNION SELECT" not in result.upper() or ("``id`` UNION SELECT password--`" in result)

    def test_multiple_embedded_quotes_all_escaped(self):
        result = _inject_order_by(BASE_PG, 'a"b"c', "ASC", "PostgreSQL")
        assert '"a""b""c"' in result

    def test_column_with_no_quotes_unchanged(self):
        result = _inject_order_by(BASE_PG, "user_name", "ASC", "PostgreSQL")
        assert '"user_name"' in result


# ── SortRequest schema validator (SEC-1 defence-in-depth) ────────────────────


class TestSortRequestSchema:
    """The Pydantic schema is the first gate; _inject_order_by is the second."""

    def test_valid_column_accepted(self):
        from app.schemas.chat import SortRequest

        req = SortRequest(sort_column="user_name", sort_order="ASC")
        assert req.sort_column == "user_name"

    def test_column_with_spaces_accepted(self):
        from app.schemas.chat import SortRequest

        req = SortRequest(sort_column="total amount", sort_order="DESC")
        assert req.sort_column == "total amount"

    def test_double_quote_rejected(self):
        from pydantic import ValidationError

        from app.schemas.chat import SortRequest

        with pytest.raises(ValidationError, match="quote characters"):
            SortRequest(sort_column='id" UNION SELECT 1--', sort_order="ASC")

    def test_backtick_rejected(self):
        from pydantic import ValidationError

        from app.schemas.chat import SortRequest

        with pytest.raises(ValidationError, match="quote characters"):
            SortRequest(sort_column="id` UNION SELECT 1--", sort_order="ASC")

    def test_empty_column_rejected(self):
        from pydantic import ValidationError

        from app.schemas.chat import SortRequest

        with pytest.raises(ValidationError):
            SortRequest(sort_column="", sort_order="ASC")


# ── invalid direction rejection ───────────────────────────────────────────────


class TestDirectionValidation:
    def test_rejects_drop_table_in_direction(self):
        with pytest.raises(ValueError, match="Invalid sort direction"):
            _inject_order_by(BASE_PG, "id", "ASC; DROP TABLE users--", "PostgreSQL")

    def test_rejects_empty_direction(self):
        with pytest.raises(ValueError, match="Invalid sort direction"):
            _inject_order_by(BASE_PG, "id", "", "PostgreSQL")

    def test_rejects_arbitrary_string_direction(self):
        with pytest.raises(ValueError, match="Invalid sort direction"):
            _inject_order_by(BASE_PG, "id", "NULLS FIRST", "PostgreSQL")
