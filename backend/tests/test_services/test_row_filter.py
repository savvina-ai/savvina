# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""_inject_row_filter: the wrap holds, or the statement is rejected before it runs."""

# ruff: noqa: S608 — every string here is a test query, never executed.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.datasources.errors import ErrorCategory
from app.datasources.models import DataSourceSchema, PrivacySettings, QueryResult, ValidationResult
from app.providers.base import LLMResponse
from app.services.execution import (
    RowFilterRejectedError,
    _classify,
    _execute_auto_query,
    _inject_row_filter,
)

_F = "tenant_id = 7"


class TestWrapShape:
    def test_wrap_puts_the_query_on_its_own_lines(self) -> None:
        assert _inject_row_filter("SELECT id, tenant_id FROM t", _F, "PostgreSQL") == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n) AS _sift_q WHERE tenant_id = 7"
        )

    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL"])
    def test_trailing_line_comment_ends_before_the_closing_paren(self, dialect: str) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t -- every row", _F, dialect)
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t -- every row\n)"
            " AS _sift_q WHERE tenant_id = 7"
        )

    def test_trailing_semicolon_is_dropped(self) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t;", _F, "PostgreSQL")
        assert (
            out == "SELECT * FROM (\nSELECT id, tenant_id FROM t\n) AS _sift_q WHERE tenant_id = 7"
        )


class TestLimitHoisting:
    @pytest.mark.parametrize(
        "sql",
        ["SELECT id, tenant_id FROM t LIMIT 10", "SELECT id, tenant_id FROM t LIMIT 10;"],
    )
    def test_trailing_limit_moves_outside_the_filter(self, sql: str) -> None:
        assert _inject_row_filter(sql, _F, "PostgreSQL") == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 10"
        )

    def test_order_by_stays_inside_and_limit_moves_out(self) -> None:
        out = _inject_row_filter(
            "SELECT id, tenant_id FROM t ORDER BY id DESC LIMIT 3", _F, "MySQL"
        )
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t ORDER BY id DESC\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 3"
        )

    def test_limit_inside_a_subquery_is_not_hoisted(self) -> None:
        sql = "SELECT * FROM (SELECT id, tenant_id FROM t LIMIT 5) s"
        assert _inject_row_filter(sql, _F, "PostgreSQL") == (
            f"SELECT * FROM (\n{sql}\n) AS _sift_q WHERE tenant_id = 7"
        )

    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL"])
    def test_limit_offset_moves_outside_the_filter(self, dialect: str) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t LIMIT 10 OFFSET 20", _F, dialect)
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 10 OFFSET 20"
        )

    def test_mysql_comma_limit_moves_outside_the_filter(self) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t LIMIT 20, 10", _F, "MySQL")
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 20, 10"
        )

    def test_postgres_offset_before_limit_moves_outside_the_filter(self) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t OFFSET 20 LIMIT 10", _F, "PostgreSQL")
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7 OFFSET 20 LIMIT 10"
        )

    def test_the_hoisted_clause_keeps_its_whitespace_and_case(self) -> None:
        out = _inject_row_filter(
            "SELECT id, tenant_id FROM t\nlimit 10  offset 20", _F, "PostgreSQL"
        )
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7\nlimit 10  offset 20"
        )

    def test_mysql_comma_limit_stays_inside_the_wrap_on_postgresql(self) -> None:
        sql = "SELECT id, tenant_id FROM t LIMIT 20, 10"
        assert _inject_row_filter(sql, _F, "PostgreSQL") == (
            f"SELECT * FROM (\n{sql}\n) AS _sift_q WHERE tenant_id = 7"
        )

    def test_unknown_dialect_hoists_every_shape(self) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t LIMIT 20, 10", _F, "")
        assert out.endswith(" AS _sift_q WHERE tenant_id = 7 LIMIT 20, 10")
        out = _inject_row_filter("SELECT id, tenant_id FROM t OFFSET 20 LIMIT 10", _F, "")
        assert out.endswith(" AS _sift_q WHERE tenant_id = 7 OFFSET 20 LIMIT 10")

    def test_mysql_comma_limit_without_a_space_moves_outside_the_filter(self) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t LIMIT 20,10", _F, "MySQL")
        assert out.endswith("\n) AS _sift_q WHERE tenant_id = 7 LIMIT 20,10")

    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL"])
    def test_limit_offset_with_a_semicolon_and_a_newline(self, dialect: str) -> None:
        out = _inject_row_filter("SELECT id, tenant_id FROM t LIMIT 10\nOFFSET 20;", _F, dialect)
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 10\nOFFSET 20"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, tenant_id FROM t LIMIT ALL",
            "SELECT id, tenant_id FROM t FETCH FIRST 5 ROWS ONLY",
            "SELECT id, tenant_id FROM t OFFSET 5",
        ],
    )
    def test_other_row_limiting_clauses_stay_inside_the_wrap(self, sql: str) -> None:
        assert _inject_row_filter(sql, _F, "PostgreSQL") == (
            f"SELECT * FROM (\n{sql}\n) AS _sift_q WHERE tenant_id = 7"
        )

    def test_only_the_trailing_limit_is_hoisted_after_an_unrecognised_offset(self) -> None:
        out = _inject_row_filter(
            "SELECT id, tenant_id FROM t ORDER BY id OFFSET 20 ROWS LIMIT 10", _F, "PostgreSQL"
        )
        assert out == (
            "SELECT * FROM (\nSELECT id, tenant_id FROM t ORDER BY id OFFSET 20 ROWS\n)"
            " AS _sift_q WHERE tenant_id = 7 LIMIT 10"
        )


class TestUnbalancedParenthesesAreRejected:
    """A closing parenthesis the query did not open would end the wrap early and leave the
    rest of the statement outside the filter. Such a statement is rejected, never run."""

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT * FROM users) AS a --", "PostgreSQL"),
            ("SELECT * FROM users) AS a -- ", "MySQL"),
            ("SELECT * FROM users) AS a #", "MySQL"),
            ("SELECT * FROM users) AS a /* rest */", "PostgreSQL"),
            ("SELECT * FROM users) AS a /* rest */", "MySQL"),
            (
                "SELECT * FROM users) AS a UNION ALL SELECT * FROM (SELECT * FROM users WHERE 1=0",
                "PostgreSQL",
            ),
            (
                "SELECT * FROM users) AS a UNION ALL SELECT * FROM (SELECT * FROM users WHERE 1=0",
                "MySQL",
            ),
            ("SELECT (1 FROM t", "PostgreSQL"),
        ],
    )
    def test_rejected(self, sql: str, dialect: str) -> None:
        with pytest.raises(RowFilterRejectedError):
            _inject_row_filter(sql, _F, dialect)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, tenant_id FROM t WHERE note = ')'",
            "SELECT id, tenant_id FROM t WHERE note = '('",
            'SELECT id, tenant_id, ")" FROM t',
            "SELECT id, tenant_id FROM t -- a ) in a comment",
            "SELECT id, tenant_id FROM t /* ( */",
            "SELECT id, tenant_id FROM t WHERE (a = 1 AND (b = 2))",
        ],
    )
    def test_parentheses_in_strings_comments_and_identifiers_do_not_count(self, sql: str) -> None:
        assert _inject_row_filter(sql, _F, "PostgreSQL").startswith("SELECT * FROM (\n")

    def test_rejection_is_a_syntax_error(self) -> None:
        exc = RowFilterRejectedError("x")
        adapter = MagicMock()
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
        assert _classify(adapter, exc) is ErrorCategory.SYNTAX


class TestRejectionGoesThroughCorrection:
    async def test_rejected_statement_never_runs_and_the_correction_runs_wrapped(self) -> None:
        good = "SELECT id, tenant_id FROM users"
        adapter = MagicMock()
        adapter.query_dialect = "PostgreSQL"
        adapter.execute_query = AsyncMock(
            return_value=QueryResult(
                columns=["id", "tenant_id"],
                column_types=["int", "int"],
                rows=[[1, 7]],
                row_count=1,
                truncated=False,
                execution_time_ms=1.0,
            )
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = MagicMock()
        provider.max_output_tokens = 4096
        provider.generate_response = AsyncMock(
            return_value=LLMResponse(
                query=good,
                explanation="fixed",
                raw_response=f"QUERY:\n```sql\n{good}\n```\nEXPLANATION: fixed",
                model="m",
            )
        )
        settings = MagicMock(default_row_limit=100, default_query_timeout=30)

        exe = await _execute_auto_query(
            generated_query="SELECT * FROM users) AS a --",
            explanation="e",
            adapter=adapter,
            config_dict={},
            connected=True,
            cache_hit=False,
            provider=provider,
            configured_model="m",
            configured_max_tokens=1024,
            system_prompt="",
            history=[],
            message="q",
            schema=DataSourceSchema(source_type="postgresql"),
            options={},
            settings=settings,
            privacy=PrivacySettings(row_filter_sql=_F),
        )

        executed = [c.args[0] for c in adapter.execute_query.await_args_list]
        assert executed == [f"SELECT * FROM (\n{good}\n) AS _sift_q WHERE {_F}"]
        assert exe.status == "executed"
        assert exe.generated_query == good
        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "syntax error" in prompt


# ── The dialect-aware scanner ──────────────────────────────────────────────────
# Each "breaks out" shape below returned other tenants' rows on a real engine when the
# check used sqlparse's lexing: the engine reads a comment, string or operator differently.

_UNION_TAIL = " AS a UNION ALL SELECT * FROM (SELECT * FROM users WHERE 1=0"


def _ok(sql: str, dialect: str) -> bool:
    try:
        _inject_row_filter(sql, _F, dialect)
    except RowFilterRejectedError:
        return False
    return True


class TestPostgresScanner:
    @pytest.mark.parametrize(
        "sql",
        [
            # nested block comments: PostgreSQL ends the comment only at the matching */
            "SELECT * FROM users /* /* */ ( */ )" + _UNION_TAIL + " /* /* */ ) */",
            # '#' is an operator in PostgreSQL, not a comment
            "SELECT * FROM users WHERE 1 # 2 = 3)" + _UNION_TAIL,
            # '\' is a complete string under standard_conforming_strings
            "SELECT * FROM users WHERE email <> '\\' )" + _UNION_TAIL + " AND email <> '\\'",
            # a dollar-quoted string hides a quote
            "SELECT $a$ ' $a$ )" + _UNION_TAIL + " OR $b$ ' $b$ = ''",
            # PostgreSQL takes any byte >= 0x80 as a dollar-tag character: "$aé$" opens a
            # dollar quote, not "$" followed by the identifier "aé$". This returned another
            # tenant's row on PostgreSQL 16 with the filter "tenant_id = 5".
            "SELECT $aé$($aé$ AS x, id, secret FROM orders) AS a,"
            " (SELECT 5 AS tenant_id, $aé$)$aé$ AS y",
        ],
    )
    def test_breakout_shapes_are_rejected(self, sql: str) -> None:
        assert not _ok(sql, "PostgreSQL")

    @pytest.mark.parametrize(
        "tag",
        ["é", "aé", "aéb", "abé", "a\u0080", "ab\u00a0", "a1é", "_日本"],
    )
    def test_a_dollar_tag_with_a_non_ascii_character_anywhere_is_rejected(self, tag: str) -> None:
        # Balanced if "$tag$" were not a quote, unbalanced to the engine.
        sql = f"SELECT ${tag}$(${tag}$ AS x, tenant_id FROM users WHERE ${tag}$)${tag}$ = ''"
        assert not _ok(sql, "PostgreSQL")

    def test_a_non_ascii_run_after_a_dollar_is_rejected_even_without_a_closing_dollar(
        self,
    ) -> None:
        # "$aé" alone is "$" then the identifier "aé" to PostgreSQL; the scanner does not
        # try to tell that apart from a tag and fails closed.
        assert not _ok("SELECT $aé, tenant_id FROM users", "PostgreSQL")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, tenant_id FROM users -- note (",
            "SELECT id, tenant_id FROM users /* comment ) */",
            "SELECT id, tenant_id FROM users /* outer /* inner ) */ still a comment ( */",
            "SELECT id, tenant_id FROM users WHERE note = ') -- ('",
            'SELECT id, tenant_id, "odd)""name" FROM users',
            "SELECT $$ ) $$ AS x, tenant_id FROM users",
            "SELECT $tag$ ( it's $tag$ AS x, tenant_id FROM users",
            "SELECT E'it\\'s )' AS x, tenant_id FROM users",
            "SELECT (5 # 3) AS x, tenant_id FROM users",
            "SELECT U&'data )' AS x, tenant_id FROM users",
            "SELECT a$b, tenant_id FROM users WHERE id = $1",
            "SELECT $a_1$ ) $a_1$ AS x, tenant_id FROM users",
            "SELECT $t$ é ) $t$ AS x, tenant_id FROM users",  # non-ASCII inside the body
            "SELECT café$x, naïve, tenant_id FROM users",  # non-ASCII identifiers with "$"
        ],
    )
    def test_legitimate_queries_pass(self, sql: str) -> None:
        assert _ok(sql, "PostgreSQL")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'a\\b' AS x, tenant_id FROM users",  # standard_conforming_strings varies
            "SELECT 'unterminated",
            'SELECT "unterminated',
            "SELECT 1 /* unterminated",
            "SELECT 1 /* /* nested, closed once */",
            "SELECT $t$ unterminated",
            "SELECT 1$a$ x $a$",  # a dollar quote glued to a number
            "SELECT $é$ ) $é$",  # a non-ASCII tag
            "SELECT E'unterminated \\'",
        ],
    )
    def test_fail_closed(self, sql: str) -> None:
        assert not _ok(sql, "PostgreSQL")


class TestMySqlScanner:
    @pytest.mark.parametrize(
        "sql",
        [
            # '--' not followed by whitespace is two minus signs in MySQL
            "SELECT * FROM users WHERE id = 1 --1)" + _UNION_TAIL,
            # an executable comment's content is run
            "SELECT * FROM users /*!)" + _UNION_TAIL + " */",
            # NO_BACKSLASH_ESCAPES makes '\' a complete string
            "SELECT * FROM users WHERE email <> '\\' )" + _UNION_TAIL + " AND email <> '\\'",
        ],
    )
    def test_breakout_shapes_are_rejected(self, sql: str) -> None:
        assert not _ok(sql, "MySQL")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, tenant_id FROM users -- note (",
            "SELECT id, tenant_id FROM users # note (",
            "SELECT id, tenant_id FROM users /* comment ) */",
            "SELECT id, tenant_id FROM users /*+ BKA(users) */",
            "SELECT id, tenant_id FROM users WHERE note = ') -- ('",
            'SELECT id, tenant_id FROM users WHERE note = ")"',
            "SELECT `odd)``name`, tenant_id FROM users",
            "SELECT (id--1) AS x, tenant_id FROM users",
            "SELECT id, tenant_id FROM users WHERE id = 2 --",
        ],
    )
    def test_legitimate_queries_pass(self, sql: str) -> None:
        assert _ok(sql, "MySQL")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'a\\b' AS x, tenant_id FROM users",
            'SELECT "a\\b" AS x, tenant_id FROM users',
            "SELECT 1 /*! 1 */",
            "SELECT 1 /*M! 1 */",
            "SELECT 1 /* a /* b */",  # '/*' inside a comment: MySQL's handling varies
            "SELECT 'unterminated",
            'SELECT "unterminated',
            "SELECT `unterminated",
            "SELECT 1 /* unterminated",
            "SELECT 1 --\xa0(",  # is NBSP whitespace to the server's charset?
            "SELECT 1 --\x7f(",
            "SELECT 1 # note\r( more",  # does a lone CR end a MySQL line comment?
        ],
    )
    def test_fail_closed(self, sql: str) -> None:
        assert not _ok(sql, "MySQL")

    @pytest.mark.parametrize(
        "sql",
        [
            # MySQL 9 may read "$tag$…$tag$" as a dollar-quoted string; MySQL 8 reads an
            # identifier. Either reading is possible, so a "$" pair with only identifier
            # characters between them fails closed.
            "SELECT $x$($x$ AS y, tenant_id FROM users WHERE $x$)$x$ = ''",
            "SELECT $$($$ AS y, tenant_id FROM users WHERE $$)$$ = ''",
            "SELECT $é$($é$ AS y, tenant_id FROM users WHERE $é$)$é$ = ''",
            "SELECT a$b$c, tenant_id FROM users",  # an identifier, rejected all the same
        ],
    )
    def test_dollar_pairs_fail_closed(self, sql: str) -> None:
        assert not _ok(sql, "MySQL")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT a$b, tenant_id FROM users",
            "SELECT $a, tenant_id FROM users",
            "SELECT $1, tenant_id FROM users",
            "SELECT a$ + b$, tenant_id FROM users",  # a space between the two "$"
            "SELECT 'x$y$z', tenant_id FROM users",
            "SELECT `a$b$`, tenant_id FROM users",
            "SELECT tenant_id FROM users /* $$ ) $$ */",
            "SELECT tenant_id FROM users -- $x$ )",
        ],
    )
    def test_single_dollars_and_quoted_dollar_pairs_pass(self, sql: str) -> None:
        assert _ok(sql, "MySQL")


class TestUnknownDialect:
    def test_both_rule_sets_must_pass(self) -> None:
        # Balanced for MySQL ('#' comment) but not for PostgreSQL ('#' operator).
        assert _ok("SELECT id, tenant_id FROM users # )", "MySQL")
        assert not _ok("SELECT id, tenant_id FROM users # )", "PostgreSQL")
        assert not _ok("SELECT id, tenant_id FROM users # )", "sql")

    def test_dialect_names_are_case_insensitive(self) -> None:
        assert _ok("SELECT (id--1) AS x, tenant_id FROM users", "mysql")
        assert not _ok("SELECT (5 # 3)) AS x FROM users", "postgresql")
