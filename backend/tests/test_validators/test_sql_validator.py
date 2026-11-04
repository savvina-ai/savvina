# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for SQL query validation logic (base + PostgreSQL-specific)."""

from unittest.mock import patch

import pytest

from app.datasources.validators.base_sql_validator import BaseSQLValidator
from app.datasources.validators.mysql_validator import MySQLValidator
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


class TestBaseSQLValidatorLeadingSet:
    """The type check lets a SET through when its text mentions SELECT (in a comment or a
    subquery); a session SET would change the time limit or the mode for every later query
    on the pooled connection, so the first token after comments decides."""

    def setup_method(self):
        self.v = BaseSQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "SET statement_timeout = 0 /* SELECT */",
            "SET LOCAL statement_timeout = 0 -- SELECT",
            "/* lead */ SET statement_timeout = 0 /* SELECT */",
            "SET search_path = (SELECT 'public')",
            "set statement_timeout = 0 /* select */",
        ],
    )
    def test_a_statement_that_starts_with_set_is_refused(self, query):
        result = self.v.validate(query)
        assert not result.is_valid
        assert result.error_message == "Query is a SET statement, which is not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1 /* SET */",
            "SELECT id FROM t WHERE note = 'SET x'",
            "-- SET\nSELECT 1",
        ],
    )
    def test_set_elsewhere_is_not_a_statement(self, query):
        assert self.v.validate(query).is_valid


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

    @pytest.mark.parametrize(
        "query",
        [
            # A value stored with set_config comes back under another column, unmasked.
            "SELECT id, current_setting('my.l') AS v FROM customers "
            "WHERE id = 2 AND set_config('my.l', email, true) IS NOT NULL",
            "WITH s AS MATERIALIZED (SELECT SET_CONFIG('my.l', email, true) AS x FROM customers) "
            "SELECT current_setting('my.l') FROM s",
        ],
    )
    def test_set_config_blocked(self, query):
        r = self.v.validate(query)
        assert not r.is_valid
        assert "set_config" in r.error_message


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

    def test_set_that_mentions_select_in_a_comment_blocked(self):
        for query in (
            "SET statement_timeout = 0 /* SELECT */",
            "SET LOCAL statement_timeout = 0 -- SELECT",
        ):
            result = self.v.validate(query)
            assert not result.is_valid
            assert result.error_message == "Query is a SET statement, which is not allowed"

    def test_dml_still_blocked(self):
        """Ensure base validator rules still apply in the PG subclass."""
        assert not self.v.validate("DELETE FROM users").is_valid
        assert not self.v.validate("INSERT INTO users VALUES (1)").is_valid

    def test_multi_statement_still_blocked(self):
        assert not self.v.validate("SELECT 1; SELECT 2").is_valid


class TestUnparseableQueryFailsClosed:
    """sqlparse >= 0.5.5 raises SQLParseError when its DoS guards trip.

    On 0.5.4 the same input returned a silently ungrouped tree, so the validator
    still reached ``get_type()``. Now the parse itself raises, and an unhandled
    exception here would turn the one gate that keeps non-SELECT SQL out of the
    customer's database into a 500 — so it must reject, never propagate.
    """

    def setup_method(self):
        self.v = BaseSQLValidator()

    def test_grouping_depth_limit_is_rejected(self):
        query = "SELECT " + "(" * 300 + "1" + ")" * 300
        result = self.v.validate(query)
        assert not result.is_valid
        assert "too complex" in (result.error_message or "")

    def test_token_limit_is_rejected(self):
        query = "SELECT * FROM t WHERE x IN (" + ",".join(str(i) for i in range(20000)) + ")"  # noqa: S608
        result = self.v.validate(query)
        assert not result.is_valid
        assert "too complex" in (result.error_message or "")

    def test_dml_past_the_parse_limit_is_still_rejected(self):
        # Fail-closed means the DML never gets a second chance either.
        ids = ",".join(str(i) for i in range(20000))
        result = self.v.validate(f"DELETE FROM users WHERE id IN ({ids})")  # noqa: S608
        assert not result.is_valid


class TestBaseSQLValidatorSelectInto:
    """INTO is rejected where it is SQL, and only there."""

    def setup_method(self):
        self.v = BaseSQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * INTO new_table FROM orders",
            "select * into new_table from orders",
            "SELECT *\n\tINTO\nnew_table FROM orders",
            "SELECT id INTO TEMP t FROM orders",
            "SELECT id INTO UNLOGGED t FROM orders",
            "WITH c AS (SELECT id FROM orders) SELECT * INTO copy_t FROM c",
            "SELECT/**/*/**/INTO/**/t/**/FROM/**/orders",
            "SELECT id FROM t INTO @x",
        ],
    )
    def test_into_as_a_keyword_is_rejected(self, query):
        r = self.v.validate(query)
        assert not r.is_valid
        assert r.error_message == "SELECT … INTO is not allowed: it writes to the database"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1 FROM notes WHERE body LIKE '%logged into%'",
            "SELECT title FROM films WHERE title = 'Into the Wild'",
            "SELECT title FROM films WHERE title = 'it''s into it'",
            "-- split into monthly buckets\nSELECT date_trunc('month', d) FROM t",
            "SELECT /* fold into one row */ count(*) FROM t",
            'SELECT "into" FROM t',
            "SELECT into_date, date_into FROM t",
        ],
    )
    def test_into_in_a_literal_comment_or_name_is_allowed(self, query):
        assert self.v.validate(query).is_valid

    def test_mysql_validator_inherits_the_check(self):
        from app.datasources.validators.mysql_validator import MySQLValidator

        v = MySQLValidator()
        assert not v.validate("SELECT id FROM t INTO @x").is_valid
        assert v.validate("SELECT `into` FROM t").is_valid

    def test_postgresql_validator_inherits_the_check(self):
        v = PostgreSQLValidator()
        assert not v.validate("SELECT * INTO new_table FROM orders").is_valid
        assert v.validate("SELECT 1 FROM notes WHERE body LIKE '%logged into%'").is_valid


# ── First significant token ───────────────────────────────────────────────────


class TestFirstTokenAllowList:
    """The statement must start with SELECT, WITH or an opening parenthesis. Mentioning
    SELECT elsewhere, in a comment, is not enough: RESET, LOCK, HANDLER or SHOW would run
    as written (a LIMIT in the comment keeps the appended LIMIT from breaking them)."""

    @pytest.mark.parametrize(
        "query",
        [
            "RESET statement_timeout /* SELECT LIMIT 1 */",
            "RESET ALL -- SELECT LIMIT 1",
            "DISCARD ALL /* SELECT LIMIT 1 */",
            "LOCK TABLE t IN ACCESS SHARE MODE /* SELECT 1 LIMIT 1 */",
            "BEGIN /* SELECT LIMIT 1 */",
            "/* SELECT LIMIT 1 */",
            "TABLE users",
            "VALUES (1)",
        ],
    )
    def test_postgresql_statement_that_does_not_start_with_a_read_is_refused(self, query):
        result = PostgreSQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message.startswith("Only SELECT queries and CTEs (WITH) are allowed")

    @pytest.mark.parametrize(
        "query",
        [
            "HANDLER users READ FIRST /* SELECT LIMIT 1 */",
            "SHOW GRANTS /* SELECT LIMIT 1 */",
            "DO 1 /* SELECT LIMIT 1 */",
            "/* SELECT LIMIT 1 */",
            # An executable comment runs on the server, so its content is the first word.
            "/*!SHOW TABLES WHERE*/ (SELECT 1) /* LIMIT */",
            "/*!50000 SHOW GRANTS*/ /* SELECT LIMIT 1 */",
        ],
    )
    def test_mysql_statement_that_does_not_start_with_a_read_is_refused(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message.startswith("Only SELECT queries and CTEs (WITH) are allowed")

    @pytest.mark.parametrize(
        "query",
        [
            "/* RESET LOCK SHOW */ SELECT 1",
            "-- RESET ALL\nSELECT 1",
            "SELECT 1 /* RESET ALL, LOCK TABLE, HANDLER, SHOW GRANTS */",
            "/* lead */ WITH c AS (SELECT 1 AS a) SELECT a FROM c",
            "(SELECT 1) UNION ALL (SELECT 2)",
            "  \n((SELECT 1))",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_read_after_comments_passes(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize(
        "query",
        ["#x\nSELECT 1", "# x\nSELECT 1", "/*+ hint */ SELECT 1", "/* a */ /*! */ SELECT 1"],
    )
    def test_mysql_comment_forms_before_a_read_pass(self, query):
        assert MySQLValidator().validate(query).is_valid


class TestPostgreSQLNestedBlockComments:
    """PostgreSQL nests block comments and sqlparse does not, so after ``/* a /* b */`` the
    two disagree on what is code: the server reads ``SELECT 1 */ SET …`` as a comment
    followed by SET while sqlparse reads a SELECT. A block comment inside another is refused."""

    def setup_method(self):
        self.v = PostgreSQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "/* a /* b */ */ SET statement_timeout = 0 /* SELECT LIMIT 1 */",
            "/* a /* b */ SELECT 1 */ SET statement_timeout = 0 /* LIMIT */",
            "/* /* */ SELECT ' */ SET statement_timeout = 0 -- LIMIT '",
            "SELECT 1 /* a /* b */ */",
            "SELECT 1 /*/* */ */",
        ],
    )
    def test_a_nested_block_comment_is_refused(self, query):
        result = self.v.validate(query)
        assert not result.is_valid

    @pytest.mark.parametrize(
        "query",
        [
            "/* a */ SELECT 1 /* b */",
            "/*/ x */ SELECT 1",
            "SELECT '/* a /* b */' AS c",
            'SELECT 1 AS "/* a /* b */"',
            "SELECT 1 -- a /* b /* c\n",
            "SELECT 1 /* a * / b */",
        ],
    )
    def test_comments_that_do_not_nest_pass(self, query):
        assert self.v.validate(query).is_valid

    def test_the_refusal_names_the_nesting(self):
        result = self.v.validate("/* a /* b */ SELECT 1 */ SET statement_timeout = 0 /* LIMIT */")
        assert result.error_message == (
            "Query nests a block comment inside another, which is not allowed"
        )

    def test_mysql_does_not_nest_comments_and_is_unaffected(self):
        assert MySQLValidator().validate("SELECT 1 /* a /* b */").is_valid


class TestMySQLExecutableCommentInto:
    """sqlparse reads ``/*! … */`` as a comment while the server runs its content, so the
    token check for INTO does not see ``/*! INTO @x */``: the selected value goes into a
    session variable on the pooled connection that a later ``SELECT @x`` returns unmasked.
    When the text holds an executable comment, INTO is refused anywhere in it."""

    def setup_method(self):
        self.v = MySQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT id FROM u WHERE id=1 /*! INTO @x */",
            "SELECT id FROM u WHERE id=1 /*!50000 INTO @x */",
            "SELECT id FROM u WHERE id=1 /*M! INTO @x */",
            "SELECT id FROM u WHERE id=1 /*!INTO*/ @x",
            "SELECT email /*! INTO OUTFILE '/tmp/x' */ FROM u",
            "SELECT id /*! STRAIGHT_JOIN */ FROM u WHERE id=1 /*! into @x */",
            # A string inside the executable comment cannot hide the INTO after it.
            "SELECT id FROM u /*! WHERE 'a*/' = 'a' INTO @x */",
        ],
    )
    def test_into_with_an_executable_comment_is_refused(self, query):
        result = self.v.validate(query)
        assert not result.is_valid
        assert result.error_message in (
            "SELECT … INTO is not allowed: it writes to the database",
            "Query contains a dangerous pattern",
        )

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT /*! STRAIGHT_JOIN */ a FROM t",
            "SELECT /*! SQL_NO_CACHE */ a FROM t WHERE note = 'x'",
            "SELECT 1 FROM notes WHERE body LIKE '%logged into%'",
            "SELECT /* fold into one row */ count(*) FROM t",
            "SELECT `into` FROM t",
        ],
    )
    def test_into_without_an_executable_comment_or_an_executable_comment_alone_pass(self, query):
        assert self.v.validate(query).is_valid


class TestMySQLHashCommentInto:
    """sqlparse reads ``INTO#x`` as one name, while the server ends the word at ``#`` and
    skips the comment to the end of the line, so ``SELECT 1 INTO#x\\n@v`` sets ``@v``
    (verified on MySQL 8.0.46). When the text holds a ``#``, INTO is refused anywhere in it."""

    def setup_method(self):
        self.v = MySQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1 FROM u LIMIT 1 INTO#x\n@v",
            "SELECT 1 FROM u INTO#\n@v",
            "SELECT 1 FROM u INTO#x\nOUTFILE '/tmp/x'",
            "SELECT 1 FROM u INTO#x\nDUMPFILE '/tmp/x'",
            "SELECT 1 FROM u into#x\n@v",
            "SELECT 1 FROM u #c\nINTO#x\n@v",
            "SELECT 1 FROM u WHERE a = '#' INTO @v",
            "SELECT 1 FROM u WHERE a = 'x' #c\nINTO @v",
        ],
    )
    def test_into_with_a_hash_comment_is_refused(self, query):
        result = self.v.validate(query)
        assert not result.is_valid
        assert result.error_message == "SELECT … INTO is not allowed: it writes to the database"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t #note\nWHERE b = 1",
            "SELECT 1 FROM notes WHERE body LIKE '%#tag%'",
            "SELECT 1 FROM notes WHERE body LIKE '%logged into%'",
            "SELECT * FROM notes WHERE body = 'walked into room #3'",
            "SELECT 1 FROM t WHERE a = 'into' #c\n",
            "SELECT 1 FROM t /*! STRAIGHT_JOIN */ WHERE a = 'into'",
            "SELECT 1 FROM t WHERE a = 'x' -- fold into #1\n",
        ],
    )
    def test_a_hash_without_into_or_into_without_a_hash_pass(self, query):
        assert self.v.validate(query).is_valid


class TestMultiStatementAfterAComment:
    """A comment between the semicolon and the next statement is a gap to the server, so
    ``;/**/SET`` is two statements as much as ``; SET``."""

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1;/**/SET statement_timeout=0 /* LIMIT */",
            "SELECT 1;/* a\nb */RESET ALL /* LIMIT */",
            "SELECT 1;-- c\nSET statement_timeout=0 /* LIMIT */",
            "SELECT 1; /**/ /**/ DISCARD ALL /* LIMIT */",
            "SELECT 1;(SELECT 2)",
            "SELECT 1;\n-- c\n-- d\nSET statement_timeout=0 /* LIMIT */",
            "SELECT 1;/* a */\n/* b */SET statement_timeout=0 /* LIMIT */",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_second_statement_after_a_comment_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == "Multiple statements are not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1;/**/HANDLER t READ FIRST /* LIMIT */",
            "SELECT 1;#x\nHANDLER t READ FIRST /* LIMIT */",
            "SELECT 1; /*! HANDLER t READ FIRST */",
            "SELECT 1;/*!50000 SHOW GRANTS*/",
        ],
    )
    def test_mysql_second_statement_after_a_comment_is_refused(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == "Multiple statements are not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            # Under MySQL's rule --1 is two minus signs, code and not a comment: the
            # server rejects these as syntax errors, but a run of dashes before a word or a
            # parenthesis after a semicolon may start a statement, so the scan refuses.
            "SELECT 1 FROM t;#x\n--1\nSET @x=1",
            "SELECT 1 FROM t /*! ; */ --1\nSET @x=1",
            "SELECT 1 FROM t;--1 SET @x=1",
            "SELECT 1 FROM t;---1\nSET @x=1",
            "SELECT 1 FROM t;--(1)\nSET @x=1",
        ],
    )
    def test_mysql_dashes_that_are_code_after_a_semicolon_are_refused(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == "Multiple statements are not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1;",
            "SELECT 1;;",
            "SELECT 1; -- c",
            "SELECT 1;--\n",
            "SELECT a--1 FROM t",
            "SELECT 1; /* c */",
            "SELECT 1 ; \n -- x\n",
            "SELECT ';' AS x",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_trailing_semicolon_or_comment_passes(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize("gap", ["/*/", "/**/ ", "-- \n", " "])
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_long_run_of_gaps_validates_quickly(self, validator, gap):
        import time

        start = time.monotonic()
        validator().validate("SELECT 1;" + gap * 5000)
        assert time.monotonic() - start < 1.0

    @pytest.mark.parametrize("piece", [";/*", ";/**/", "; --", ";\n", ";#", ";*/", ";-- \n"])
    @pytest.mark.parametrize("mysql_comments", [False, True])
    def test_the_semicolon_scan_stays_linear(self, piece, mysql_comments):
        # Every semicolon starts a scan; the closers and line ends are looked up, not
        # searched for again, so many semicolons each followed by an unclosed comment stay
        # fast (sqlparse itself is the slower part of the validator on such input).
        import time

        from app.datasources.validators.base_sql_validator import has_second_statement

        start = time.monotonic()
        has_second_statement("SELECT 1" + piece * 50000, mysql_comments=mysql_comments)
        assert time.monotonic() - start < 1.0


class TestMySQLDashesWithoutWhitespace:
    """MySQL reads -- as a comment only when whitespace or a control character follows;
    ``--1`` is two minus signs. sqlparse reads both as a comment, so after ``1 --1`` it does
    not see the ``INTO @x`` the server runs."""

    def setup_method(self):
        self.v = MySQLValidator()

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT email FROM u WHERE id=1 AND 1 --1 INTO @x",
            "SELECT email FROM u WHERE id=1 AND 1 --1 into @x",
            "SELECT email FROM u WHERE id=1 AND 1 --(1) INTO @x, @y",
        ],
    )
    def test_into_after_dashes_that_are_not_a_comment_is_refused(self, query):
        result = self.v.validate(query)
        assert not result.is_valid
        assert result.error_message == "SELECT … INTO is not allowed: it writes to the database"

    def test_a_statement_that_starts_with_dashes_is_not_a_read(self):
        assert not self.v.validate("--1\nSELECT 1").is_valid

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 1 -- comment",
            "SELECT 1 -- split into buckets",
            "SELECT 1 --\tsplit into buckets",
            "SELECT a--1 FROM t",
            "-- c\nSELECT 1",
            "--\nSELECT 1",
        ],
    )
    def test_comments_and_arithmetic_keep_their_verdict(self, query):
        assert self.v.validate(query).is_valid


class TestLimitAfterATrailingSemicolon:
    """The appended LIMIT must join the statement: a trailing semicolon, and the comments
    and whitespace after the last statement, are dropped first, or ``SELECT 1; -- c`` would
    become ``SELECT 1; -- c`` + ``LIMIT 1000``, a second statement that fails at run time."""

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("SELECT 1; -- c", "SELECT 1\nLIMIT 1000"),
            ("SELECT 1; /* c */", "SELECT 1\nLIMIT 1000"),
            ("SELECT 1;\n-- a\n/* b */\n", "SELECT 1\nLIMIT 1000"),
            ("SELECT 1 -- c", "SELECT 1\nLIMIT 1000"),
            ("SELECT 1;;", "SELECT 1\nLIMIT 1000"),
            ("SELECT ';--' AS x; -- c", "SELECT ';--' AS x\nLIMIT 1000"),
            ("SELECT 'a; -- b' AS x", "SELECT 'a; -- b' AS x\nLIMIT 1000"),
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_trailing_semicolon_and_comments_are_dropped(self, validator, query, expected):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("SELECT 1; # c", "SELECT 1\nLIMIT 1000"),
            ("SELECT 1;#c\n", "SELECT 1\nLIMIT 1000"),
            # Two minus signs to the server, not a comment: kept.
            ("SELECT 1 --1", "SELECT 1 --1\nLIMIT 1000"),
            # An executable comment runs on the server: kept, the semicolon after it dropped.
            ("SELECT 1 /*! SQL_NO_CACHE */;", "SELECT 1 /*! SQL_NO_CACHE */\nLIMIT 1000"),
        ],
    )
    def test_mysql_trailing_comment_forms(self, query, expected):
        result = MySQLValidator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == expected

    def test_postgresql_keeps_a_trailing_hash(self):
        # PostgreSQL reads # as the XOR operator, not a comment.
        result = PostgreSQLValidator().validate("SELECT 1 # 2")
        assert result.sanitized_query == "SELECT 1 # 2\nLIMIT 1000"


class TestLeadingByteOrderMark:
    """A byte order mark (U+FEFF) an editor put before the query is not a first word."""

    @pytest.mark.parametrize("query", ["﻿SELECT 1", "﻿  SELECT 1", "  ﻿﻿-- c\nSELECT 1"])
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_read_after_a_byte_order_mark_passes(self, validator, query):
        result = validator().validate(query)
        assert result.is_valid
        assert not result.sanitized_query.startswith(("﻿", " "))

    @pytest.mark.parametrize(
        "query", ["﻿SET statement_timeout = 0 /* SELECT */", "﻿RESET ALL -- SELECT"]
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_what_follows_the_mark_is_still_judged(self, validator, query):
        assert not validator().validate(query).is_valid

    def test_mysql_executable_set_after_a_mark_is_refused(self):
        assert not MySQLValidator().validate("﻿/*!SET*/ sql_mode = (SELECT 1)").is_valid


class TestLockingReads:
    """A locking read takes row locks other sessions wait on; FOR UPDATE is refused by the
    UPDATE keyword, and the share forms are refused alike, with a comment between the words
    counted as a gap."""

    LOCK_ERROR = "Query takes row locks (FOR SHARE, LOCK IN SHARE MODE), which is not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM t FOR SHARE",
            "select * from t for share nowait",
            "SELECT * FROM t FOR KEY SHARE",
            "SELECT * FROM t FOR KEY SHARE SKIP LOCKED",
            "SELECT * FROM t FOR/**/SHARE",
            "SELECT * FROM t FOR -- c\nSHARE",
            "SELECT * FROM t FOR /* a */ KEY /* b */ SHARE",
            "SELECT * FROM t FOR\n\tSHARE OF t",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_share_locks_are_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM t FOR UPDATE",
            "SELECT * FROM t FOR NO KEY UPDATE",
            "SELECT * FROM t FOR/**/NO KEY UPDATE",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_update_locks_stay_refused(self, validator, query):
        assert not validator().validate(query).is_valid

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM t LOCK IN SHARE MODE",
            "select * from t lock in share mode",
            "SELECT * FROM t LOCK/**/IN/**/SHARE/**/MODE",
            "SELECT * FROM t LOCK #x\nIN SHARE MODE",
            "SELECT * FROM t /*! LOCK IN SHARE MODE */",
            "SELECT * FROM t /*!50000 FOR SHARE */",
            "SELECT * FROM t /*!FOR*/ SHARE",
            "SELECT * FROM t FOR #c\nSHARE",
            "SELECT * FROM t FOR/*!*/SHARE",
        ],
    )
    def test_mysql_share_locks_in_every_comment_form_are_refused(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT share, lock FROM t",
            "SELECT share FROM t WHERE lock = 'share'",
            "SELECT 'lock' AS l, 'share' AS s FROM t",
            "SELECT id FROM t WHERE kind = 'share' ORDER BY share",
            "SELECT shared_for, for_share FROM t",
            "SELECT id FROM t /* for review */ WHERE lock_mode = 'in share'",
            "SELECT id FROM t WHERE x = 1 -- for key lookups\n",
            "SELECT id FROM posts WHERE title = 'Up for share'",
            "SELECT id FROM t WHERE note = 'lock in share mode'",
            "SELECT id FROM t WHERE x = 'it''s' AND y = 'for key share'",
            "SELECT id FROM t -- lock in share mode?\n",
            "SELECT id FROM t /* for share */ WHERE x = 1",
            'SELECT id FROM t WHERE "for share" = 1',
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_columns_and_strings_named_share_or_lock_pass(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize("piece", ["FOR /*", "FOR ", "LOCK IN /*", "FOR -- \n", "FOR #"])
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_long_run_of_words_and_comments_validates_quickly(self, validator, piece):
        import time

        start = time.monotonic()
        validator().validate("SELECT 1 FROM t WHERE a = 1 " + piece * 3000)
        assert time.monotonic() - start < 2.0

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM t WHERE x = 'a' FOR SHARE",
            "SELECT * FROM t WHERE x = 'it''s' FOR SHARE",
            "SELECT * FROM t WHERE x = 'for' /* 'share' */ FOR SHARE",
            "SELECT * FROM t WHERE x = 1 -- c\nLOCK IN SHARE MODE",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_literal_before_the_clause_does_not_hide_it(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            # A backslash may escape a quote, a dollar sign may open a dollar-quoted string
            # that holds one: the scan then reads every word, as before (refuses more).
            "SELECT 'for share\\' FROM t",
            "SELECT $$for share$$ FROM t",
            "SELECT 'a\\' FROM t FOR SHARE -- '",
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_quotes_are_not_followed_next_to_a_backslash_or_a_dollar_sign(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            # PostgreSQL has no backtick quoting: a backtick is an operator character, so
            # it opens no region and the words after it are code.
            "SELECT id FROM t WHERE a ` 1 FOR SHARE",
            "SELECT `for share` FROM t",
        ],
    )
    def test_postgresql_reads_a_backtick_as_an_operator_character(self, query):
        result = PostgreSQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR

    def test_mysql_reads_a_backtick_as_a_quoted_name(self):
        assert MySQLValidator().validate("SELECT `for share` FROM t").is_valid

    def test_mysql_unclosed_backtick_verdict_is_pinned(self):
        # MySQL follows the backtick, so the unclosed region hides FOR SHARE from the
        # lock scan; the server rejects an unclosed backtick as a syntax error, so
        # accepting is safe.
        query = "SELECT id FROM t WHERE a ` 1 FOR SHARE"
        assert MySQLValidator().validate(query).is_valid

    def test_mysql_dashes_without_whitespace_do_not_hide_the_clause(self):
        # MySQL reads --1 as two minus signs: 1 - -1 FOR SHARE is a locking read there,
        # and a comment on PostgreSQL.
        query = "SELECT 1 FROM t WHERE x = 1 --1 FOR SHARE"
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == self.LOCK_ERROR
        assert PostgreSQLValidator().validate(query).is_valid


class TestWithoutTrailingGaps:
    """``without_trailing_gaps``: the query up to its last code token, from one sqlparse
    parse and no checks, for a query ``validate`` has already accepted."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("SELECT 1", "SELECT 1"),
            ("SELECT 1;", "SELECT 1"),
            ("SELECT 1; -- c", "SELECT 1"),
            ("SELECT ';--' AS x; -- c", "SELECT ';--' AS x"),
            ("\ufeff  SELECT 1 /* c */ ;\n", "SELECT 1"),
            ("SELECTday FROM t", "SELECT day FROM t"),
        ],
    )
    @pytest.mark.parametrize("validator", [PostgreSQLValidator, MySQLValidator])
    def test_a_trailing_semicolon_and_comments_go(self, validator, query, expected):
        assert validator().without_trailing_gaps(query) == expected

    def test_a_hash_comment_is_a_gap_on_mysql_and_code_on_postgresql(self):
        assert MySQLValidator().without_trailing_gaps("SELECT 1 #c") == "SELECT 1"
        assert PostgreSQLValidator().without_trailing_gaps("SELECT 1 #c") == "SELECT 1 #c"

    def test_an_empty_or_unparseable_query_is_none(self):
        from sqlparse.exceptions import SQLParseError

        assert PostgreSQLValidator().without_trailing_gaps("  ;  ") is None
        with patch(
            "app.datasources.validators.base_sql_validator.sqlparse.parse",
            side_effect=SQLParseError("too deep"),
        ):
            assert PostgreSQLValidator().without_trailing_gaps("SELECT 1") is None
