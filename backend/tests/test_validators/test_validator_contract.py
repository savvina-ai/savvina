# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Contract rules of the PostgreSQL and MySQL validators that test_sql_validator.py does
not pin: the REPLACE() string function passes, a FETCH FIRST/NEXT clause is a row limit, a
parenthesis must open with a read, an executable comment before the first word is refused,
a backslash or a dollar sign does not hide INTO or a share lock, and every fixed error
message is safe to show and to send to the correction prompt."""

from __future__ import annotations

import ast
import inspect

import pytest

from app.datasources.validators import base_sql_validator, mysql_validator, postgresql_validator
from app.datasources.validators.mysql_validator import EXECUTABLE_COMMENT_ERROR, MySQLValidator
from app.datasources.validators.postgresql_validator import PostgreSQLValidator

_VALIDATORS = [PostgreSQLValidator, MySQLValidator]
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
    it is a syntax error, so none is appended. Only the clause counts, as code: on the
    non-streaming execute path the LIMIT is the only bound on the rows read into memory, so
    the word FETCH in a comment, a literal or a name must still get one."""

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


class TestAParenthesisMustOpenARead:
    """A statement may start with a parenthesis only when the parenthesis itself opens with
    SELECT or WITH. (TABLE t) reads a whole table that no FROM or JOIN names, so the schema
    check never sees it, and the validators did not refuse it before this rule."""

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
    one (/*!*/, whitespace alone) is a gap; one after the first word stays allowed
    (/*! STRAIGHT_JOIN */). The FETCH clause counts only when it is code with
    the comments' content read as code and as a gap, the direction that keeps the LIMIT."""

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
        assert result.error_message == EXECUTABLE_COMMENT_ERROR
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

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT id FROM t /*M! FETCH FIRST 1 ROWS ONLY */",
            "SELECT id FROM t /*!99999 FETCH FIRST 1 ROWS ONLY */",
        ],
    )
    def test_a_fetch_clause_the_server_may_skip_keeps_the_limit(self, query):
        result = MySQLValidator().validate(query)
        assert result.is_valid
        assert result.sanitized_query.endswith("\nLIMIT 1000")

    def test_a_fetch_clause_outside_any_comment_still_counts(self):
        q = "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY"
        assert MySQLValidator().validate(q).sanitized_query == q


class TestABackslashDoesNotHideInto:
    """sqlparse reads a backslash before a quote as an escape, so SELECT 'a\\' INTO t -- '
    is one literal to it. PostgreSQL (standard_conforming_strings on) and MySQL under
    NO_BACKSLASH_ESCAPES end the literal at that quote and run the INTO. When the text holds
    a backslash or a dollar sign the token check is not the whole story, so INTO as a word
    anywhere is refused."""

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
    def test_into_in_a_comment_after_a_backslash_is_refused_too(self, validator):
        # A known over-refusal, pinned: with the quotes not followed, a comment opener inside
        # a literal ('/*', the second case above) would hide the INTO, so a comment is no gap.
        result = validator().validate(r"SELECT 'a\b' AS s -- split into buckets")
        assert not result.is_valid
        assert result.error_message == self._INTO

    def test_the_backslash_is_in_the_text(self):
        # The raw strings above hold one backslash, not two: the shape under test.
        assert r"SELECT 'a\' INTO t -- '".count("\\") == 1


class TestABackslashDoesNotHideAShareLock:
    """The share-lock scan reads code words, and with a backslash or a dollar sign in the
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
    """A dollar sign triggers the every-word INTO and share-lock fallbacks as a backslash
    does. sqlparse lexes $$/*$$ as one literal, so the INTO token check alone would catch
    the first shape, but the lock scan reads code words, and with the quotes not followed
    the /* inside the literal opens what it takes for a comment: without the dollar trigger
    the FOR SHARE after it passes, and PostgreSQL takes the lock."""

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
