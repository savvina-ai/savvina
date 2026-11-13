# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Contract rules of the PostgreSQL and MySQL validators that test_sql_validator.py does
not pin: the REPLACE() string function passes, a FETCH FIRST/NEXT clause is a row limit, a
parenthesis must open with a read, and every fixed error message is safe to show and to
send to the correction prompt."""

from __future__ import annotations

import ast
import inspect
import time

import pytest

from app.datasources.validators import base_sql_validator, mysql_validator, postgresql_validator
from app.datasources.validators.base_sql_validator import TABLE_STATEMENT_ERROR
from app.datasources.validators.mysql_validator import MySQLValidator
from app.datasources.validators.postgresql_validator import PostgreSQLValidator
from tests.test_validators.test_sql_validator import (
    GLUED_LOCK_SHAPES,
    LONG_LOCK_WORD_RUNS,
    MYSQL_COMMENT_LOCK_SHAPES,
    MYSQL_INTO_SHAPES,
    PG_INTO_SHAPES,
    PG_NESTED_COMMENT_LOCK_SHAPES,
    SHARE_LOCK_SHAPES,
)

_VALIDATORS = [PostgreSQLValidator, MySQLValidator]
_INTO_ERROR = "SELECT … INTO is not allowed: it writes to the database"
_MODULES = [base_sql_validator, postgresql_validator, mysql_validator]


class TestReplaceIsAFunctionNotAStatement:
    """The word REPLACE is not blocked on its own, so REPLACE(str, from, to) passes. The
    REPLACE INTO pattern refuses the DML form."""

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_the_string_function_passes(self, validator):
        result = validator().validate("SELECT REPLACE(name, 'a', 'b') AS n FROM t")
        assert result.is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_replace_into_is_refused(self, validator):
        assert not validator().validate("REPLACE INTO t (id) VALUES (1)").is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_replace_into_after_a_read_is_refused_by_the_into_rule(self, validator):
        # As code the two words never reach the pattern: the first-word rule or the INTO
        # token rule refuses them first.
        result = validator().validate("SELECT 1 FROM t WHERE x = 1 REPLACE INTO t")
        assert not result.is_valid
        assert result.error_message == "SELECT … INTO is not allowed: it writes to the database"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize("query", ["SELECT 'replace into' AS s", "SELECT 1 /* replace into */"])
    def test_the_pattern_refuses_the_words_in_a_literal_or_a_comment(self, validator, query):
        # The pattern is the backstop behind those two rules and reads the raw text, so it
        # also refuses the words where they are not code. A known over-refusal, pinned so
        # that removing the pattern is a decision and not an accident.
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == "Query contains a dangerous pattern"


class TestFetchIsARowLimit:
    """FETCH FIRST n ROWS ONLY is a row limit (PostgreSQL, MariaDB); a LIMIT appended after
    it is a syntax error, so none is appended. Only the statement's own clause counts, as
    code: on the non-streaming execute path the LIMIT is the only bound on the rows read
    into memory, so the word FETCH in a comment, a literal or a name must still get one,
    and so must a clause inside a subquery, which bounds the subquery alone. The
    statement's own body may itself stand in parentheses."""

    def test_no_limit_is_appended_after_fetch_first(self):
        q = "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY"
        result = PostgreSQLValidator().validate(q)
        assert result.is_valid
        assert result.sanitized_query == q

    def test_offset_fetch_next_is_left_alone(self):
        q = "SELECT id FROM t ORDER BY id OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY"
        result = PostgreSQLValidator().validate(q)
        assert result.is_valid
        assert "LIMIT" not in result.sanitized_query.upper()

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_comment_between_the_two_words_does_not_hide_the_clause(self, validator):
        q = "SELECT id FROM t ORDER BY id FETCH /* c */ FIRST 10 ROWS ONLY"
        assert validator().validate(q).sanitized_query == q

    def test_limit_is_still_appended_without_either(self):
        result = PostgreSQLValidator().validate("SELECT id FROM t")
        assert result.sanitized_query == "SELECT id FROM t\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        ("query", "sanitized"),
        [
            ("SELECT id FROM t -- Fetch first the orders", "SELECT id FROM t\nLIMIT 1000"),
            ("SELECT id FROM t /* fetch next page */", "SELECT id FROM t\nLIMIT 1000"),
            (
                "SELECT * FROM logs WHERE action = 'fetch first'",
                "SELECT * FROM logs WHERE action = 'fetch first'\nLIMIT 1000",
            ),
            ("SELECT id, fetch FROM t", "SELECT id, fetch FROM t\nLIMIT 1000"),
            ("SELECT fetch AS first FROM t", "SELECT fetch AS first FROM t\nLIMIT 1000"),
        ],
    )
    def test_the_word_alone_is_not_a_row_limit(self, validator, query, sanitized):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == sanitized

    def test_mysql_reads_a_hash_comment_naming_the_clause_as_a_gap(self):
        result = MySQLValidator().validate("SELECT id FROM t # fetch first ten\n")
        assert result.sanitized_query == "SELECT id FROM t\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM (SELECT a FROM t ORDER BY a FETCH FIRST 5 ROWS ONLY) s JOIN b ON true",
            "WITH c AS (SELECT a FROM t ORDER BY a FETCH FIRST 5 ROWS ONLY) SELECT * FROM c",
            "(SELECT a FROM t FETCH FIRST 5 ROWS ONLY) UNION ALL (SELECT a FROM u)",
            "(SELECT a FROM t) UNION ALL (SELECT a FROM u FETCH FIRST 5 ROWS ONLY)",
            "SELECT 1 AS a UNION ALL (SELECT a FROM u FETCH FIRST 5 ROWS ONLY)",
            "WITH c AS (SELECT 1 AS a) (SELECT a FROM c FETCH FIRST 5 ROWS ONLY) UNION SELECT 2",
            "SELECT a FROM t WHERE b IN (SELECT b FROM u FETCH NEXT 5 ROWS ONLY)",
            "SELECT (SELECT a FROM t FETCH FIRST 1 ROWS ONLY) AS x FROM big",
        ],
    )
    def test_a_clause_inside_parentheses_does_not_bound_the_statement(self, validator, query):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == f"{query}\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t WHERE b IN (SELECT 1) ORDER BY a FETCH FIRST 5 ROWS ONLY",
            "SELECT a FROM t WHERE b = '(' ORDER BY a FETCH FIRST 5 ROWS ONLY",
            "SELECT a FROM t /* ( */ ORDER BY a FETCH FIRST 5 ROWS ONLY",
            # The parentheses are the statement's own body: the clause is its own, and
            # PostgreSQL refuses a LIMIT after it (multiple LIMIT clauses). The body may
            # be the whole statement, follow a WITH clause or precede the ORDER BY.
            "(SELECT a FROM t ORDER BY a FETCH FIRST 5 ROWS ONLY)",
            "((SELECT a FROM t ORDER BY a FETCH FIRST 5 ROWS ONLY));",
            "WITH c AS (SELECT 1 AS a) (SELECT a FROM c FETCH FIRST 5 ROWS ONLY)",
            "WITH c AS (SELECT 1 AS a) ((SELECT a FROM c FETCH NEXT 5 ROWS ONLY))",
            "(SELECT a FROM t FETCH FIRST 5 ROWS ONLY) ORDER BY a",
            # The clause of a set operation stands outside its branches.
            "(SELECT a FROM t) UNION ALL (SELECT a FROM u) FETCH FIRST 5 ROWS ONLY",
        ],
    )
    def test_the_statements_own_clause_still_counts(self, validator, query):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == query

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_text_whose_quotes_are_not_followed_keeps_the_limit(self, validator):
        # A parenthesis inside a literal would be counted there, so the clause is not
        # recognised: a syntax error on the server rather than an unbounded read.
        query = r"SELECT a FROM t WHERE b LIKE '%\_%' ESCAPE '\' ORDER BY a FETCH FIRST 5 ROWS ONLY"
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == f"{query}\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            # After a dot FETCH is a column name, which both servers take for a reserved
            # word too, and FIRST is its alias. PostgreSQL also takes a gap after the dot.
            'SELECT o.id, s.fetch FIRST FROM (SELECT 1 AS "fetch") s, orders o',
            'SELECT o.id, s . fetch FIRST FROM (SELECT 1 AS "fetch") s, orders o',
            'SELECT o.id, s./* c */fetch FIRST FROM (SELECT 1 AS "fetch") s, orders o',
            # Both servers read a non-ASCII character as part of a name, so a no-break space
            # or a euro sign glued to FETCH makes one name of it and the word beside it.
            "SELECT o.id FETCH\u00a0FIRST FROM orders o",
            "SELECT o.id, x\u00a0FETCH FIRST FROM (SELECT 1 AS x\u00a0FETCH) s, orders o",
            "SELECT o.id, \u20acFETCH FIRST FROM (SELECT 1 AS \u20acFETCH) s, orders o",
            # A dollar sign is a name character to both servers after a name's first
            # character, and MySQL reads @fetch as a user variable.
            "SELECT a$fetch first FROM (SELECT id AS a$fetch FROM orders) d",
            "SELECT fetch$a first FROM (SELECT id AS fetch$a FROM orders) d",
            "SELECT @fetch first FROM orders",
            # The digit before the dot belongs to a name.
            'SELECT t1.fetch FIRST FROM (SELECT 1 AS "fetch") t1, orders o',
        ],
    )
    def test_fetch_as_part_of_a_name_is_no_row_limit(self, validator, query):
        # Without the LIMIT each returns every order on the sample servers (the gapped
        # dot forms on PostgreSQL only, a syntax error on MySQL; test_validator_live.py),
        # so the LIMIT is appended.
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == f"{query}\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_dollar_name_elsewhere_leaves_the_clause_alone(self, validator):
        query = "SELECT a$b, @v FROM t ORDER BY a$b FETCH FIRST 5 ROWS ONLY"
        assert validator().validate(query).sanitized_query == query

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_non_ascii_character_elsewhere_leaves_the_clause_alone(self, validator):
        query = "SELECT a FROM t WHERE label = '\u20ac\u00a0' ORDER BY a FETCH FIRST 5 ROWS ONLY"
        assert validator().validate(query).sanitized_query == query


class TestLimitIsARowLimitWhenItIsTheStatementsOwn:
    """The word LIMIT is a row limit only as the statement's own clause, as code, like FETCH:
    on the non-streaming execute path the LIMIT is the only bound on the rows read into
    memory, so the word in a comment, a literal or a subquery must not drop it."""

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t LIMIT 5",
            "select a from t limit 5",
            "SELECT a FROM t ORDER BY a LIMIT 5 OFFSET 2",
            "SELECT a FROM t OFFSET 2 LIMIT 5",
            "SELECT a FROM t LIMIT 5;",
            "SELECT a FROM t /* c */ LIMIT /* d */ 5",
            "SELECT a FROM t WHERE b IN (SELECT 1) AND c = 'x' LIMIT 5",
            "WITH c AS (SELECT 1 AS a) SELECT a FROM c LIMIT 5",
            "(SELECT a FROM t LIMIT 5)",
            "(SELECT a FROM t) UNION ALL (SELECT a FROM u) LIMIT 5",
            "(SELECT a FROM t LIMIT 5) ORDER BY a",
            "SELECT a FROM t LIMIT 5, 10",
            "SELECT a FROM t LIMIT ALL",
            "SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 5",
            "WITH c AS (SELECT 1) (SELECT a FROM t LIMIT 5)",
            # A name called limit earlier on does not hide the clause.
            "SELECT a AS limit FROM t LIMIT 5",
            # A quoted name stands between the dot, or the AS, and the LIMIT: it is the clause.
            'SELECT a FROM "s"."t" LIMIT 5',
            'SELECT a FROM t AS "x" LIMIT 5',
            "SELECT a FROM `db`.`t` LIMIT 5",
            "SELECT a FROM t AS `x` LIMIT 5",
            # A number with a trailing dot (1.) ends in that dot, which is not the dot of a
            # name: the LIMIT after it is the keyword. The gap forms read the same.
            "SELECT a FROM t WHERE a > 1. LIMIT 5",
            "SELECT a FROM t WHERE a > 1.\nLIMIT 5",
            "SELECT a FROM t WHERE a > 1./* c */LIMIT 5",
            "SELECT a FROM t WHERE a > 12. LIMIT 5",
            "SELECT a FROM t WHERE a > 1. FETCH FIRST 5 ROWS ONLY",
        ],
    )
    def test_the_statements_own_limit_is_left_alone(self, validator, query):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == query

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        ("query", "sanitized"),
        [
            ("SELECT a FROM big WHERE note = 'limit'", None),
            ("SELECT a FROM big WHERE a LIKE '%rate limit%'", None),
            ("SELECT a FROM big -- limit to top rows", "SELECT a FROM big"),
            ("SELECT a FROM big /* limit */", "SELECT a FROM big"),
            # A subquery's own LIMIT bounds the subquery, not the rows the statement returns.
            ("SELECT a FROM big WHERE a IN (SELECT a FROM big LIMIT 5000)", None),
            ("SELECT * FROM (SELECT a FROM big LIMIT 5000) s", None),
            ("WITH c AS (SELECT a FROM big LIMIT 5000) SELECT * FROM c", None),
            ("SELECT (SELECT 1 LIMIT 1) AS x FROM big", None),
            ("(SELECT a FROM big LIMIT 5) UNION ALL (SELECT a FROM big)", None),
            # A name, not the keyword: reserved, but a server takes it after a dot and,
            # on PostgreSQL, after AS.
            ("SELECT t.limit FROM big t", None),
            # The digit before the dot belongs to a name here, not to a number.
            ("SELECT t1.limit FROM big t1", None),
            ("SELECT a FROM big t1 WHERE t1.limit > 3", None),
            ("SELECT t . limit FROM big t", None),
            ("SELECT t./* c */limit FROM big t", None),
            ("SELECT a AS limit FROM big", None),
            ("SELECT a$limit FROM big", None),
            ("SELECT a FROM big WHERE a = 1 AND limit_value > 3", None),
        ],
    )
    def test_the_word_elsewhere_still_gets_the_limit(self, validator, query, sanitized):
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == f"{sanitized or query}\nLIMIT 1000"

    def test_mysql_reads_a_hash_comment_naming_the_clause_as_a_gap(self):
        result = MySQLValidator().validate("SELECT a FROM big # limit 5\n")
        assert result.sanitized_query == "SELECT a FROM big\nLIMIT 1000"

    def test_mysql_executable_comment_content_is_code(self):
        query = "SELECT a FROM big /*! LIMIT 5 */"
        assert MySQLValidator().validate(query).sanitized_query == query

    def test_postgresql_reads_the_words_after_a_hash_as_code(self):
        # # is the XOR operator there, so the LIMIT after it is the statement's own.
        query = "SELECT a # 1 FROM big LIMIT 5"
        assert PostgreSQLValidator().validate(query).sanitized_query == query

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t WHERE n = 'O\\'Brien' ORDER BY a LIMIT 10",
            "SELECT $$x$$ AS a FROM t LIMIT 10",
            "SELECT a FROM t WHERE n = 'it''s' AND m = 'a\\\\' LIMIT 10",
        ],
    )
    def test_a_text_whose_quotes_are_not_followed_keeps_the_word_rule(self, validator, query):
        # Where a literal ends is a guess there, so the clause is not found on the code
        # pieces and the word anywhere counts, as it always has: a LIMIT appended after a
        # LIMIT the query has is a syntax error, and this form (an escaped quote and a
        # LIMIT) is an ordinary MySQL query.
        result = validator().validate(query)
        assert result.is_valid
        assert result.sanitized_query == query

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_text_whose_quotes_are_not_followed_still_adds_a_limit_when_the_word_is_absent(
        self, validator
    ):
        query = "SELECT a FROM t WHERE n = 'O\\'Brien'"
        assert validator().validate(query).sanitized_query == f"{query}\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            'SELECT a FROM public."Orders" FETCH FIRST 5 ROWS ONLY',
            'SELECT a FROM "s"."t" ORDER BY a FETCH NEXT 5 ROWS ONLY',
        ],
    )
    def test_a_quoted_name_before_a_fetch_clause_does_not_make_it_a_name(self, validator, query):
        # The same adjacency rule as for LIMIT: the dot before the quoted name is not the
        # token next to FETCH.
        assert validator().validate(query).sanitized_query == query


class TestAParenthesisMustOpenARead:
    """A statement may start with a parenthesis only when the parenthesis itself opens with
    SELECT or WITH. (TABLE t) reads a whole table that no FROM or JOIN names, so the schema
    check never sees it, and the validators before this change refused it."""

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        ["(TABLE hr.salaries)", "((TABLE t))", "(VALUES (1))", "( /* SELECT */ TABLE t)"],
    )
    def test_a_parenthesis_that_does_not_open_a_read_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message.startswith("Only SELECT queries and CTEs (WITH) are allowed")

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "(SELECT 1) UNION (SELECT 2)",
            "((SELECT 1))",
            "( /* c */ SELECT 1)",
            "(WITH c AS (SELECT 1 AS x) SELECT x FROM c)",
        ],
    )
    def test_a_parenthesised_read_passes(self, validator, query):
        assert validator().validate(query).is_valid


class TestTheTableWordIsRefusedAnywhere:
    """In PostgreSQL and MySQL, TABLE t is shorthand for SELECT * FROM t. Only the start of
    the statement was judged before (TABLE t, (TABLE t)), so in any other position it passed
    and ran: SELECT * FROM (TABLE hr.secret) s, a CTE body, a UNION arm, an EXISTS. The
    schema check reads only FROM and JOIN, so a table excluded by the privacy settings was
    readable by naming it after TABLE. The word is refused wherever it is code, except as a
    name right after a dot or AS (t.table, AS table)."""

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM (TABLE hr.salaries) s",
            "WITH s AS (TABLE t) SELECT * FROM s",
            "(SELECT 1) UNION ALL (TABLE t)",
            "SELECT 1 UNION TABLE t",
            "SELECT 1 WHERE EXISTS (TABLE t)",
            "SELECT * FROM u WHERE x IN (TABLE t)",
            "SELECT 1 /* c */ UNION /* d */ TABLE t",
            "select 1 union table t",
            "SELECT '/*' AS a, 'x\\' AS b FROM (TABLE t) s -- */'",
            "SELECT $$/*$$ AS a FROM (TABLE t) s -- */",
        ],
    )
    def test_the_word_as_code_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == TABLE_STATEMENT_ERROR

    def test_mysql_unversioned_executable_comment_content_is_code(self):
        result = MySQLValidator().validate("SELECT * FROM (/*! TABLE t */) s")
        assert not result.is_valid
        assert result.error_message == TABLE_STATEMENT_ERROR

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT 'table' AS s",
            "SELECT 1 /* TABLE t */",
            "SELECT 1 -- TABLE t",
            "SELECT table_name FROM information_schema.tables",
            'SELECT "table" FROM t',
            "SELECT * FROM (VALUES (1, 'a')) AS v(id, name)",
        ],
    )
    def test_the_word_as_text_or_a_name_passes(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            # The end of a WITH clause, and a set operation with its modifiers.
            "WITH s AS (SELECT 1 AS a) TABLE s",
            "SELECT 1 UNION ALL TABLE t",
            "SELECT 1 EXCEPT DISTINCT TABLE t",
            "SELECT 1 INTERSECT TABLE t",
            "SELECT 1 UNION -- c\nTABLE t",
            # A word no list of openers would hold: the name that ends the SEARCH or CYCLE
            # clause of a recursive CTE (PostgreSQL 14 and later), DISTINCTROW (MySQL).
            "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 3)"
            " SEARCH DEPTH FIRST BY n SET o TABLE hr.secret",
            "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 3)"
            " CYCLE n SET m USING p TABLE hr.secret",
            "SELECT 1 UNION DISTINCTROW TABLE t",
            # An opener glued to a number: the server reads the number, then the keyword
            # (MySQL, and PostgreSQL 14 and older).
            "SELECT 1E5UNION TABLE t",
            "SELECT 1.5UNION TABLE t",
            "SELECT 4242E0INTERSECT TABLE t",
            "SELECT 1UNION TABLE t",
            # A quoted name between the dot, or the AS, and TABLE: the word is not the name.
            'SELECT a FROM s."t" TABLE u',
            "SELECT a AS `x` TABLE u",
            # In a text whose quotes are not followed every TABLE word is refused.
            "SELECT 'a\\' AS a UNION /**/ TABLE t -- '",
            "SELECT 'a\\' AS a UNION -- '\nTABLE t",
            "SELECT 'a\\' AS a, (/**/TABLE t) -- '",
            "SELECT 'a\\' AS a UNION TABLE t",
        ],
    )
    def test_the_word_is_refused_whatever_stands_before_it(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == TABLE_STATEMENT_ERROR

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            # A column or an alias called table: valid right after a dot, and after AS on
            # PostgreSQL (a syntax error on MySQL, which the server then reports).
            "SELECT t.table FROM t",
            "SELECT t . table FROM t",
            "SELECT t./* c */table FROM t",
            "SELECT a AS table FROM t",
            "SELECT a AS /* c */ table FROM t",
            "SELECT t.table AS x, u.table AS y FROM t JOIN u ON t.id = u.id",
        ],
    )
    def test_a_name_called_table_right_after_a_dot_or_as_passes(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t WHERE x LIKE '%table%' AND n = 'O\\'Brien'",
            "SELECT a FROM t WHERE n = 'O\\'Brien' -- the table",
            "SELECT t.table FROM t WHERE n = 'O\\'Brien'",
            "SELECT $$a table$$ AS s FROM t",
        ],
    )
    def test_in_a_text_whose_quotes_are_not_followed_every_table_word_is_refused(
        self, validator, query
    ):
        # A known limit, kept on purpose: where a literal ends is a guess there, and a
        # comment opener inside a literal can hide the word from the scan, so no name rule
        # is applied and the message tells how to write the text.
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == TABLE_STATEMENT_ERROR

    def test_the_message_says_how_to_write_a_column_called_table(self):
        assert "quote a column called table" in TABLE_STATEMENT_ERROR

    @pytest.mark.parametrize("query", ["SELECT `table` FROM t", "SELECT 1 # TABLE t"])
    def test_mysql_backtick_name_and_hash_comment_pass(self, query):
        assert MySQLValidator().validate(query).is_valid

    def test_postgresql_tablesample_passes(self):
        assert PostgreSQLValidator().validate("SELECT * FROM t TABLESAMPLE SYSTEM (10)").is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize("query", ["TABLE t", "(TABLE t)"])
    def test_the_leading_forms_keep_their_own_message(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message.startswith("Only SELECT queries and CTEs (WITH) are allowed")


class TestMySQLHashLinesBeforeTheRead:
    """The one shape the hardened MySQL validator accepts that the old one refused: MySQL
    reads a # line as a comment whatever it says, so a response that opens with a markdown
    heading and then a SELECT is judged on the SELECT, as the server runs it. PostgreSQL
    reads # as an operator and still refuses."""

    _RESPONSE = "## QUERY:\nSELECT 3"

    def test_mysql_judges_the_statement_after_the_heading(self):
        result = MySQLValidator().validate(self._RESPONSE)
        assert result.is_valid
        assert result.sanitized_query == "## QUERY:\nSELECT 3\nLIMIT 1000"

    def test_postgresql_refuses_it(self):
        assert not PostgreSQLValidator().validate(self._RESPONSE).is_valid

    def test_mysql_still_refuses_a_heading_followed_by_something_else(self):
        assert not MySQLValidator().validate("## QUERY:\nSHOW TABLES").is_valid


class TestMySQLExecutableCommentTheServerMaySkip:
    """Whether the server runs an executable comment depends on the comment: /*!NNNNN … */
    runs only on a server version of at least NNNNN, and MySQL skips MariaDB's /*M! … */
    entirely. Before the first word, a mixture of comments the server runs and skips can
    lead with a statement no fixed reading of them shows, so an executable comment before
    the first word is refused (the validator before this rule refused them all). Content with no
    word changes the reading too (a quote hides the comment's own closer), so only an empty
    one (/*!*/, whitespace alone) is a gap. After the first word, one the server always runs
    stays allowed (/*! STRAIGHT_JOIN */); one it may skip is refused wherever it stands
    (TestMySQLGatedCommentsAreRefused)."""

    _NOT_A_READ = "Only SELECT queries and CTEs (WITH) are allowed"
    _SET = "Query is a SET statement, which is not allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "/*!99999 SELECT 1 */ SHOW TABLES -- LIMIT",
            "/*M! SELECT 1 */ SHOW TABLES",
            "/*!50000 SELECT 1 */ USE information_schema",
            "/*!50000 SELECT 1 */",
            "/*M! SELECT 1 */",
            "/*!99999 SELECT 1 */ SELECT 2",
            "/*M! SELECT 1 */ SELECT 2",
            "/*!50000 SELECT 1 */ SELECT 2",
            "( /*!50000 SELECT 1 */ SELECT 2)",
            "/*!50000*/ SELECT 1",
        ],
    )
    def test_a_word_bearing_comment_before_the_first_word_is_refused(self, query):
        self._assert_refused_as_a_leading_comment(query)

    @pytest.mark.parametrize(
        "query",
        [
            "/*! ( */ SELECT 1",
            "/*! ' */ SELECT 1",
            "/*! -- */ SELECT 1",
            "/*! # */ SELECT 1",
            "/*! ; */ SELECT 1",
            "/*m! SELECT 1 */ SELECT 2",
            "/*M! SELECT 1 */ SELECT 2",
        ],
    )
    def test_a_leading_comment_that_is_not_empty_is_refused(self, query):
        # Content with no word still changes how the server reads what follows: a quote
        # in a comment it runs hides the comment's own closer. A lower-case m is refused
        # as the upper-case one is.
        self._assert_refused_as_a_leading_comment(query)

    @pytest.mark.parametrize(
        "query",
        [
            # A gated comment and a plain one: a server of 5.0 to 9.9 skips the first and
            # runs the second, and leads with neither reading's first word.
            "/*!99999 SELECT 1 */ /*!50000 SET @x = */ (SELECT 2) -- LIMIT",
            "/*M! SELECT 1 */ /*! SET @x = */ (SELECT 2) -- LIMIT",
        ],
    )
    def test_a_mixture_of_run_and_skipped_comments_is_refused(self, query):
        self._assert_refused_as_a_leading_comment(query)

    @staticmethod
    def _assert_refused_as_a_leading_comment(query: str) -> None:
        # Its own message: sqlparse reads the comment as a comment, so the not-a-read
        # message would say Got: SELECT and the correction loop would resend the query.
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == mysql_validator.EXECUTABLE_COMMENT_ERROR
        assert "Got:" not in result.error_message

    @pytest.mark.parametrize(
        ("query", "word"),
        [
            ("/*!SHOW TABLES WHERE*/ (SELECT 1)", "SHOW"),
            ("/*!50000 SHOW GRANTS*/ /* SELECT LIMIT 1 */", "SHOW"),
            ("/*M! handler t read first */ SELECT 1", "HANDLER"),
        ],
    )
    def test_a_comment_that_opens_with_another_statement_names_it(self, query, word):
        # The comment's own first word is the statement the server runs when it runs the
        # comment, so the not-a-read message names that word, as test_sql_validator.py expects.
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == f"{self._NOT_A_READ}. Got: {word}"

    @pytest.mark.parametrize(
        "query",
        [
            "/*!99999 SELECT 1 */ SET @@session.transaction_read_only = 0",
            "/*M! SELECT 1 */ SET @@session.transaction_read_only = 0 -- LIMIT",
            "/*!SET*/ @x = (SELECT 1)",
            "/*!50000 SET @x = */ (SELECT 2)",
        ],
    )
    def test_a_set_in_or_after_the_comment_gets_the_set_message(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == self._SET

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT /*! STRAIGHT_JOIN */ a FROM t",
            "/* c */ SELECT 1",
            "#x\nSELECT 1",
            "/*!*/ SELECT 1",
            "/*M!*/ SELECT 1",
            "/* a */ /*! */ SELECT 1",
        ],
    )
    def test_a_read_after_empty_or_plain_comments_passes(self, query):
        assert MySQLValidator().validate(query).is_valid

    @pytest.mark.parametrize(
        "query",
        ["/*!SET*/ @x = (SELECT 1)", "/*!SHOW TABLES WHERE*/ (SELECT 1)"],
    )
    def test_the_comment_content_still_counts_as_code(self, query):
        assert not MySQLValidator().validate(query).is_valid

    def test_a_fetch_clause_outside_any_comment_still_counts(self):
        q = "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY"
        assert MySQLValidator().validate(q).sanitized_query == q


class TestMySQLRegionBeforeTheFirstWord:
    """The verdicts of the region before the first word that no other test pins: gaps,
    parentheses and executable comments in every order. Written before the walk moved
    from a pattern of its own onto _Gaps, and unchanged by the move."""

    _NOT_A_READ = "Only SELECT queries and CTEs (WITH) are allowed"

    @pytest.mark.parametrize(
        "query",
        [
            "( /*!*/ (SELECT 1))",
            "#\n(\n-- c\n/* d */ WITH c AS (SELECT 1 AS a) SELECT a FROM c)",
            "--\tc\nSELECT 1",
            "/*M! */ ( /*!*/ SELECT 1)",
        ],
    )
    def test_a_read_past_gaps_parentheses_and_empty_comments_passes(self, query):
        assert MySQLValidator().validate(query).is_valid

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            # A SET behind a parenthesis is not a SET statement: not a read.
            ("(SET @x = 1)", f"{_NOT_A_READ}. Got: UNKNOWN"),
            ("/*!*/ ( /*! */ SET @x = 1)", f"{_NOT_A_READ}. Got: UNKNOWN"),
            ("/*!*/ SET @x = 1", "Query is a SET statement, which is not allowed"),
            # The comment is judged where it stands, behind a parenthesis too.
            ("( /*! x */ SET @x = 1)", f"{_NOT_A_READ}. Got: X"),
            ("/*! select 1 */ ( SET @x = 1)", mysql_validator.EXECUTABLE_COMMENT_ERROR),
            # An executable comment that is never closed is no gap.
            ("/*! SELECT 1", f"{_NOT_A_READ}. Got: UNKNOWN"),
            ("SETTINGS", f"{_NOT_A_READ}. Got: UNKNOWN"),
        ],
    )
    def test_everything_else_keeps_its_refusal(self, query, message):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == message


class TestMySQLGatedCommentsAreRefused:
    """Every MySQL scan reads an executable comment's content as code. That is the server's
    reading only for a comment it always runs (/*! … */ with no version). A versioned
    /*!NNNNN and MariaDB's /*M! are run by one server and skipped by another, and a mixture
    has readings no single scan shows: on MySQL 8 the quote in /*!99999 ' */ is skipped
    with its comment, so the /*! INTO @v */ after it runs, while a scan that reads both as
    code sees the quote open a literal that swallows the INTO. So a comment the server may
    skip is refused anywhere in the query, not only before the first word."""

    _INTO = "SELECT … INTO is not allowed: it writes to the database"

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT a FROM t WHERE 1 /*!99999 ' */ /*! INTO @v */ -- '",
            "SELECT a FROM t WHERE 1 /*M! ' */ /*! INTO @v */ -- '",
            "SELECT a FROM t WHERE 1 /*!99999 ' */ /*! FOR SHARE */ -- '",
            # The clause straddles a skipped comment and a run one.
            "SELECT a FROM t FOR /*!99999 x */ /*! SHARE */",
            "SELECT a /*!50000 SQL_NO_CACHE */ FROM t",
            "SELECT a /*M! x */ FROM t",
            "SELECT a /*m! x */ FROM t",
            # A digit straight after the ! is taken for a version whatever its length.
            "SELECT a /*!1 x */ FROM t",
            "SELECT id FROM t /*M! FETCH FIRST 1 ROWS ONLY */",
            "SELECT id FROM t /*!99999 FETCH FIRST 1 ROWS ONLY */",
        ],
    )
    def test_a_comment_the_server_may_skip_is_refused_anywhere(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == mysql_validator.GATED_COMMENT_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT /*! STRAIGHT_JOIN */ a FROM t",
            "SELECT a /*! */ FROM t",
            # An empty MariaDB-only comment is nothing on either server.
            "SELECT a /*M!*/ FROM t",
            "SELECT a /*M! */ FROM t",
            # A plain comment that merely mentions a version.
            "SELECT a FROM t /* since !50000 */",
        ],
    )
    def test_a_comment_the_server_always_runs_or_an_empty_one_passes(self, query):
        assert MySQLValidator().validate(query).is_valid

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            ("SELECT id FROM u WHERE id=1 /*!50000 INTO @x */", _INTO),
            ("SELECT * FROM t /*!50000 FOR SHARE */", base_sql_validator.LOCKING_READ_ERROR),
            ("SELECT 1;/*!50000 SHOW GRANTS*/", base_sql_validator.MULTIPLE_STATEMENTS_ERROR),
        ],
    )
    def test_what_the_comment_holds_is_named_first(self, query, message):
        # The gated-comment refusal is the last check, so the more specific message wins.
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == message

    def test_postgresql_has_no_executable_comments(self):
        assert PostgreSQLValidator().validate("SELECT a /*!50000 x */ FROM t").is_valid


class TestABackslashDoesNotHideInto:
    """sqlparse reads a backslash before a quote as an escape, so SELECT 'a\\' INTO t -- '
    is one literal to it. PostgreSQL (standard_conforming_strings on) and MySQL under
    NO_BACKSLASH_ESCAPES end the literal at that quote and run the INTO. When a backslash
    stands directly before a quote, or the text opens a dollar-quoted string, where a
    literal ends depends on the server, so INTO as a word anywhere is refused. A backslash
    or a dollar sign anywhere else moves no literal's end and changes nothing."""

    _INTO = "SELECT … INTO is not allowed: it writes to the database"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            r"SELECT 'a\' INTO t -- '",
            r"SELECT 'a\' , '/*' INTO t -- */'",
            "SELECT $$a$$ INTO t",
        ],
    )
    def test_into_after_a_backslash_or_dollar_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == self._INTO

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize("query", ["SELECT 'walked into room #3' AS s", r"SELECT 'a\b' AS s"])
    def test_a_literal_without_into_as_code_still_passes(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            r"SELECT 'a\' AS s -- split into buckets",
            r"SELECT a FROM t WHERE b LIKE '%\_%' ESCAPE '\' -- split into buckets",
            # A backslash before a backtick triggers too, on both validators: conservative,
            # since only MySQL quotes with a backtick and it does not escape inside one.
            r"SELECT `a\` AS s -- split into buckets",
            "SELECT $$a$$ AS s -- split into buckets",
            "SELECT $tag$a$tag$ AS s -- split into buckets",
            "SELECT $\u20ac$a$\u20ac$ AS s -- split into buckets",
        ],
    )
    def test_into_in_a_comment_of_such_a_text_is_refused_too(self, validator, query):
        # A known over-refusal, pinned: with the quotes not followed, a comment opener inside
        # a literal ('/*', the second case above) would hide the INTO, so a comment is no gap.
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == self._INTO

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            # A regular expression, a bind parameter, a JSON path, a file-path column: the
            # backslash stands before no quote and the dollar sign opens no string.
            r"SELECT 'a\b' AS s -- split into buckets",
            r"SELECT regexp_replace(name, '\s+', ' ') FROM t WHERE d ILIKE '%into%'",
            "SELECT $1, $2 FROM t -- load into view",
            "SELECT JSON_EXTRACT(doc, '$.address') FROM t -- bucket customers into tiers",
            "SELECT \"$path\" FROM t WHERE note = 'moved into storage'",
            r"SELECT regexp_replace(a, '\d+', '') FROM t WHERE note = 'up for share'",
            'SELECT price AS "cost in $" FROM t /* for share of wallet */',
        ],
    )
    def test_a_backslash_or_dollar_that_moves_no_quote_changes_nothing(self, validator, query):
        assert validator().validate(query).is_valid

    def test_the_backslash_is_in_the_text(self):
        # The raw strings above hold one backslash, not two: the shape under test.
        assert r"SELECT 'a\' INTO t -- '".count("\\") == 1


class TestABackslashDoesNotHideAShareLock:
    """The share-lock scan reads code words, and with a backslash before a quote in the
    text the quotes are not followed, so a '/*' literal opens what the scan takes for a
    comment and hides the clause after it, while PostgreSQL ends 'a\\' at its quote and
    takes the lock. In such a text the clause counts as consecutive words anywhere."""

    _SHAPE = r"SELECT 'a\' AS x, '/*' AS y FROM t FOR SHARE -- */'"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_share_lock_behind_a_comment_opener_literal_is_refused(self, validator):
        result = validator().validate(self._SHAPE)
        assert not result.is_valid
        assert result.error_message == base_sql_validator.LOCKING_READ_ERROR

    @pytest.mark.parametrize(
        "query",
        [
            r"SELECT 'a\' AS x, '/*' AS y FROM t LOCK IN SHARE MODE -- */'",
            r"SELECT 'a\' AS x, '/*' AS y FROM t FOR KEY SHARE -- */'",
        ],
    )
    def test_the_other_clause_forms_are_refused_on_mysql(self, query):
        result = MySQLValidator().validate(query)
        assert not result.is_valid
        assert result.error_message == base_sql_validator.LOCKING_READ_ERROR

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize("query", ["SELECT 'up for share' AS s", r"SELECT 'a\b' AS s"])
    def test_without_the_clause_as_words_after_a_backslash_it_passes(self, validator, query):
        assert validator().validate(query).is_valid

    def test_the_shape_holds_one_backslash(self):
        assert self._SHAPE.count("\\") == 1


class TestTheFallbackReadsWhatTheCodeWordScanReads:
    """A dollar-quote opener inside a literal turns the every-word fallback on, and a comment
    opener beside it ('$$/* limit', '$$--') makes the code-word scan read the rest of the
    text, or of the line, as a comment. The server ends the literal at its quote and runs
    what follows, so the fallback is the one reading that sees it, and it must read what
    the code-word scan reads: INTO and the clause's first word glued to a number (7.E5INTO,
    1.E0FOR, see _reads_as), and the clause's words with gaps between them (FOR /* x */
    SHARE). The MySQL and PostgreSQL 16 shapes run on the sample servers
    (test_validator_live.py)."""

    @pytest.mark.parametrize(
        ("validator", "query"),
        [
            (
                MySQLValidator,
                "SELECT 3 FROM DUAL WHERE '$$/* limit' <> '' AND 1 <> 7.E5INTO @sv_probe",
            ),
            # PostgreSQL 14 and older read 1INTO as the number and then the keyword.
            (PostgreSQLValidator, "SELECT '$$', '/*', 1INTO t FROM x WHERE c = '*/'"),
            (PostgreSQLValidator, "SELECT '$$--', 1.E5INTO t FROM x"),
        ],
    )
    def test_into_glued_to_a_number_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == _INTO_ERROR

    @pytest.mark.parametrize(
        ("validator", "query"),
        [
            (
                MySQLValidator,
                "SELECT id FROM orders WHERE '$$/* limit' <> '' AND id = 1.E0FOR SHARE",
            ),
            (
                PostgreSQLValidator,
                "SELECT id FROM ecommerce.orders WHERE '$$--' <> '' FOR /* x */\nSHARE",
            ),
            (MySQLValidator, "SELECT id FROM orders WHERE '$$/* limit' <> '' FOR /* x */ SHARE"),
            (
                MySQLValidator,
                "SELECT id FROM orders WHERE '$$/* limit' <> '' LOCK /* x */ IN SHARE MODE",
            ),
            (
                PostgreSQLValidator,
                "SELECT id FROM t WHERE '$$--' <> '' FOR /* a */ KEY -- b\nSHARE",
            ),
            (MySQLValidator, "SELECT id FROM orders WHERE '$$/* limit' <> '' FOR #x\nSHARE"),
            (MySQLValidator, "SELECT id FROM orders WHERE '$$/* limit' <> '' FOR /*!*/ SHARE"),
        ],
    )
    def test_a_share_lock_glued_or_spaced_is_refused(self, validator, query):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == base_sql_validator.LOCKING_READ_ERROR

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            # No trigger at all: the code-word scan decides.
            r"SELECT regexp_replace(a, '\d', '') FROM t WHERE b = 'it''s' -- up for review",
            # A trigger, and a FOR that no gap joins to a SHARE: the clause's words must
            # stand with nothing but gaps between them, so the words apart make no clause.
            "SELECT $$a$$ AS s, 'for' AS f FROM t",
            "SELECT $$a$$ AS s, 'for' AS f, share FROM t",
            r"SELECT 'O\'Brien' AS n FROM t -- totals for each share class",
            "SELECT $$a$$ AS s, x.e5intox, into_date FROM x",
        ],
    )
    def test_the_words_apart_still_pass(self, validator, query):
        assert validator().validate(query).is_valid

    @pytest.mark.parametrize(("piece", "count"), LONG_LOCK_WORD_RUNS)
    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_long_run_after_a_trigger_validates_quickly(
        self, validator, piece, count, validate_seconds_without_sqlparse
    ):
        seconds = validate_seconds_without_sqlparse(
            validator(),
            "SELECT $$a$$ FROM t WHERE a = 1 " + piece * count,  # noqa: S608
        )
        assert seconds < 2.0


def _string_literals(module) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def _error_message_texts(module) -> list[str]:
    """The fixed text of every error message in *module*: the constant and f-string parts
    of each ``error_message=`` argument, of each ``*_ERROR`` constant, and of everything a
    function named ``*_error`` returns."""
    values: list[ast.expr] = []
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.keyword) and node.arg == "error_message":
            values.append(node.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.endswith("_ERROR") for target in node.targets
        ):
            values.append(node.value)
        if isinstance(node, ast.FunctionDef) and node.name.endswith("_error"):
            values.extend(
                sub.value
                for sub in ast.walk(node)
                if isinstance(sub, ast.Return) and sub.value is not None
            )
    return [
        part.value
        for value in values
        for part in ast.walk(value)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ]


class TestErrorMessageWording:
    """Two consumers read a validator's message: the user, and the correction prompt.
    `services/correction._is_rate_limit_error` matches `rate_limit`, `tokens per minute`,
    `token_limit` and `413` in any error text, so a message containing one would be
    retried as a provider throttle; and no fixed message carries a quote character, so a
    message quoted into a prompt cannot end a string early."""

    _FORBIDDEN = ("rate_limit", "tokens per minute", "token_limit", "413")

    @pytest.mark.parametrize("module", _MODULES)
    def test_no_rate_limit_fragment(self, module):
        # Every string literal of the module, which includes every error message and
        # every comment pattern; a docstring that mentioned one would fail too, deliberately.
        for text in _string_literals(module):
            for fragment in self._FORBIDDEN:
                assert fragment not in text, f"{module.__name__}: {text!r}"

    @pytest.mark.parametrize("module", _MODULES)
    def test_no_fixed_message_text_carries_a_quote_character(self, module):
        texts = _error_message_texts(module)
        assert texts, f"{module.__name__}: no error message found, the scan is broken"
        for text in texts:
            assert not any(ch in text for ch in "'\"`"), f"{module.__name__}: {text!r}"

    @pytest.mark.parametrize(
        ("validator", "query"),
        [
            (PostgreSQLValidator, "SET statement_timeout = 0"),
            (PostgreSQLValidator, "SELECT * FROM t FOR SHARE"),
            (PostgreSQLValidator, "SELECT id INTO t2 FROM t"),
            (PostgreSQLValidator, "/* a /* b */ */ SELECT 1"),
            (PostgreSQLValidator, "SELECT set_config('a', 'b', true)"),
            (PostgreSQLValidator, "SELECT 1; SELECT 2"),
            (PostgreSQLValidator, "DELETE FROM t"),
            (PostgreSQLValidator, "RESET ALL"),
            (MySQLValidator, "SELECT @v := 1"),
            (MySQLValidator, "SELECT 1 /*! INTO @x */"),
            (MySQLValidator, "SET STATEMENT max_statement_time=0 FOR SELECT 1"),
            (MySQLValidator, "SELECT SLEEP(1)"),
        ],
    )
    def test_refusals_carry_no_quote_character(self, validator, query):
        # The whole message as returned, so the parts filled in at run time are covered too.
        result = validator().validate(query)
        assert not result.is_valid
        assert not any(ch in result.error_message for ch in "'\"`"), result.error_message


class TestADollarQuoteKeepsTheFallback:
    """A dollar-quote opener ($$, $tag$) triggers the every-word INTO and share-lock
    fallbacks as a backslash before a quote does. sqlparse lexes $$/*$$ as one literal, so
    the INTO token check alone would catch the first shape, but the lock scan reads code
    words, and with the quotes not followed the /* inside the literal opens what it takes
    for a comment: without the dollar trigger the FOR SHARE after it passes, and PostgreSQL
    takes the lock."""

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        ("query", "message"),
        [
            (
                "SELECT $$/*$$ AS y INTO TEMP t -- */",
                "SELECT … INTO is not allowed: it writes to the database",
            ),
            (
                "SELECT $$/*$$ AS y FROM ecommerce.orders FOR SHARE -- */",
                base_sql_validator.LOCKING_READ_ERROR,
            ),
        ],
    )
    def test_a_dollar_quoted_comment_opener_hides_nothing(self, validator, query, message):
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == message

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        ("query", "message"),
        [
            (
                "SELECT $\u20ac$ ' $\u20ac$ INTO newt FROM secrets -- '",
                "SELECT … INTO is not allowed: it writes to the database",
            ),
            (
                "SELECT $\u2014tag$ ' $\u2014tag$ INTO newt FROM secrets -- '",
                "SELECT … INTO is not allowed: it writes to the database",
            ),
            (
                "SELECT * FROM t WHERE a = $\u20ac$ ' $\u20ac$ FOR SHARE -- '",
                base_sql_validator.LOCKING_READ_ERROR,
            ),
        ],
    )
    def test_a_tag_of_non_ascii_characters_opens_a_string_too(self, validator, query, message):
        # PostgreSQL takes any non-ASCII character in a dollar-quote tag (a euro sign, a
        # dash), so the quote inside is part of the string and the clause after it runs. A
        # trigger that knew a tag of word characters only did not fire here, and the scan
        # then read that quote as opening a literal that swallowed the clause.
        result = validator().validate(query)
        assert not result.is_valid
        assert result.error_message == message

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_long_tag_with_no_closing_dollar_validates_quickly(self, validator):
        # The tag pattern stays linear. Its tail was once two overlapping alternatives (a
        # word character, a non-ASCII character); every non-ASCII letter is both, so a run
        # of them after a dollar sign with no closing one backtracked exponentially: about a
        # second at 24 letters, doubling with each one after.
        query = "SELECT $" + "\u00e9" * 5000 + " AS s FROM t"
        start = time.monotonic()
        result = validator().validate(query)
        assert time.monotonic() - start < 2.0
        assert result.is_valid


_LOCK_ERROR = base_sql_validator.LOCKING_READ_ERROR
_NESTED_ERROR = postgresql_validator.NESTED_COMMENT_ERROR
_CODE_WORD_SHAPES = (
    [(PostgreSQLValidator, q, _INTO_ERROR) for q in PG_INTO_SHAPES]
    + [(MySQLValidator, q, _INTO_ERROR) for q in MYSQL_INTO_SHAPES]
    + [(v, q, _LOCK_ERROR) for v in _VALIDATORS for q in GLUED_LOCK_SHAPES + SHARE_LOCK_SHAPES]
    + [(MySQLValidator, q, _LOCK_ERROR) for q in MYSQL_COMMENT_LOCK_SHAPES]
    + [(PostgreSQLValidator, q, _NESTED_ERROR) for q in PG_NESTED_COMMENT_LOCK_SHAPES]
)
# A literal that turns the every-word fallback on and holds a comment opener, put first in
# the select list, and what closes the construction: past the literal the code-word scan
# reads the rest of the text (/*) or of the line (--) as a comment. In the third, sqlparse
# reads the backslash as an escape and keeps everything up to the closing quote of the
# trailing comment inside the literal, so it shows no token there either.
_HIDING = [("'$$/*', ", ""), ("'$$--', ", ""), (r"'--a\', ", " -- '")]


class TestEveryReadingAgreesOnTheCodeWordShapes:
    """The code-word scan and the every-word fallback are two readings of one rule. Each
    shape the code-word tests refuse (test_sql_validator.py: TestIntoOnCodeWords and
    TestLockingReads) stays refused, with the same message, when a literal put first in its
    select list hides it from the code-word scan and leaves the fallback to see it. A
    nested comment behind a line-comment opener in such a literal is one of the shapes."""

    def test_the_literal_hides_the_rest_from_the_code_word_scan(self):
        assert [w for w, _ in base_sql_validator.code_words("SELECT '$$/*', 1INTO T")] == ["SELECT"]
        words = base_sql_validator.code_words("SELECT '$$--', 1INTO T\nFROM X")
        assert [w for w, _ in words] == ["SELECT", "FROM", "X"]

    def test_sqlparse_shows_no_comment_inside_the_escaped_literal(self):
        import sqlparse

        statement = sqlparse.parse(r"SELECT '--a\', * FROM t FOR /* /* */ x */ SHARE -- '")[0]
        assert not any(t.ttype in sqlparse.tokens.Comment for t in statement.flatten())

    @pytest.mark.parametrize(("prefix", "suffix"), _HIDING)
    @pytest.mark.parametrize(("validator", "query", "message"), _CODE_WORD_SHAPES)
    def test_a_shape_hidden_from_the_code_word_scan_is_still_refused(
        self, validator, query, message, prefix, suffix
    ):
        assert query[:7].upper() == "SELECT "
        hidden = query[:7] + prefix + query[7:] + suffix
        result = validator().validate(hidden)
        assert not result.is_valid, hidden
        assert result.error_message == message, hidden


class TestAQueryWithoutAScansKeywordSkipsTheScan:
    """Each text scan looks for a word (INTO, TABLE, SHARE, LIMIT or FETCH, a ; or a second
    /*), so a query that holds none of them runs none of them: the scans are linear but each
    reads the whole text, and nearly every query holds none."""

    _QUERY = "SELECT a, b FROM t WHERE c = 1 ORDER BY a"

    @staticmethod
    def _forbid_the_scans(monkeypatch, validator_class, *, gaps):
        def boom(*args, **kwargs):
            raise AssertionError("a scan ran on a query that holds none of its keywords")

        monkeypatch.setattr(base_sql_validator, "code_pieces", boom)
        monkeypatch.setattr(validator_class, "_code_pieces", boom)
        if gaps:
            monkeypatch.setattr(base_sql_validator, "_Gaps", boom)

    def test_postgresql_runs_no_scan(self, monkeypatch):
        self._forbid_the_scans(monkeypatch, PostgreSQLValidator, gaps=True)
        result = PostgreSQLValidator().validate(self._QUERY)
        assert result.sanitized_query == f"{self._QUERY}\nLIMIT 1000"

    def test_mysql_runs_no_scan_but_the_one_before_the_first_word(self, monkeypatch):
        # The region before the first word is read by _Gaps on every query.
        self._forbid_the_scans(monkeypatch, MySQLValidator, gaps=False)
        result = MySQLValidator().validate(self._QUERY)
        assert result.sanitized_query == f"{self._QUERY}\nLIMIT 1000"

    @pytest.mark.parametrize("validator", _VALIDATORS)
    @pytest.mark.parametrize(
        "query",
        [
            "select 1 into @x",
            "SELECT 1 UNION TABLE t",
            "SELECT 1 FROM t FOR SHARE",
            "SELECT 1; SELECT 2",
        ],
    )
    def test_the_keyword_brings_the_scan_back(self, validator, query):
        assert not validator().validate(query).is_valid

    def test_the_keyword_brings_the_scan_back_for_a_nested_comment(self):
        assert not PostgreSQLValidator().validate("SELECT 1 /* a /* b */ */").is_valid

    @pytest.mark.parametrize("validator", _VALIDATORS)
    def test_a_limit_or_a_fetch_clause_is_still_found(self, validator):
        for query in ("SELECT 1 FROM t LIMIT 5", "SELECT 1 FROM t FETCH FIRST 5 ROWS ONLY"):
            assert validator().validate(query).sanitized_query == query
