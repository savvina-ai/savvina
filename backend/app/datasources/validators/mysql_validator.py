# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB-specific query validator."""

import re
from typing import ClassVar

import sqlparse.sql as sql_nodes
import sqlparse.tokens as sql_tokens

from ..models import ValidationResult
from .base_sql_validator import (
    LOCKING_READ_ERROR,
    SET_STATEMENT_ERROR,
    BaseSQLValidator,
    has_second_statement,
    not_a_read_error,
    strip_query,
    takes_row_locks,
)

# What the server reads as a gap between two words: whitespace, a /* … */ comment, a -- or #
# comment to the end of the line, and the */ that closes an executable comment (opened by
# _EXECUTABLE_OPENER, which the scan removes first). Wider than the server's rules (it needs
# a space after --), which only refuses more; as the gap before the first word it lets
# nothing extra through, since no statement starts with a minus. The alternatives overlap
# ("/*/" is a "/*" opener or a "*/" closer), so the group is atomic and every quantifier over
# it possessive: once a gap is matched it is never re-split, and a long run of such pieces
# cannot make the scan backtrack exponentially.
_GAP = r"(?>\s|/\*.*?\*/|\*/|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$))"
# Two dashes the server does not read as a comment: it needs whitespace or a control
# character after them, so --1 is two minus signs. sqlparse reads them as a comment.
_DASHES_AS_CODE = r"--(?=[^\s\x00-\x1f])"
# _GAP as the server reads it before the first word: -- starts a comment only when whitespace,
# a control character or the end of the text follows. Narrower than _GAP, which here refuses
# more (--1 then SELECT is not a read).
_LEADING_GAP = r"(?>\s|/\*.*?\*/|\*/|--(?![^\s\x00-\x1f])[^\n]*(?:\n|$)|#[^\n]*(?:\n|$))"
# A statement that starts with SET, after any gaps (a # comment with no space after it
# included, which sqlparse reads as an operator).
_LEADING_SET = re.compile(rf"(?:{_LEADING_GAP})*+SET\b", re.IGNORECASE | re.DOTALL)
# A statement that starts with a read, after any gaps: SELECT, WITH or an opening parenthesis.
_LEADING_READ = re.compile(
    rf"(?:{_LEADING_GAP})*+(?:SELECT\b|WITH\b|\()", re.IGNORECASE | re.DOTALL
)
# The opener of a MySQL or MariaDB executable comment, with its optional version number: the
# server runs the content, so for the pattern scan it is code, not a comment. Result masking
# and the strict guard use the same opener (``services.result_masking.EXECUTABLE_COMMENT``);
# keep the two in step.
_EXECUTABLE_OPENER = re.compile(r"/\*M?!\d*")
# Text sqlparse reads as a comment while the server runs it: an executable-comment opener, or
# two dashes the server does not read as a comment. Where one is present, sqlparse's tokens
# do not show all the code, so the INTO check falls back to the scan text.
_HIDDEN_FROM_SQLPARSE = re.compile(rf"/\*M?!|{_DASHES_AS_CODE}")


class MySQLValidator(BaseSQLValidator):
    """MySQL/MariaDB-specific query validator extending the shared base."""

    MYSQL_DANGEROUS_FUNCTIONS: ClassVar[list[str]] = [
        "SLEEP",
        "BENCHMARK",
        "LOAD_FILE",
        "GET_LOCK",
        "RELEASE_LOCK",
        "MASTER_POS_WAIT",
        "SOURCE_POS_WAIT",
    ]

    MYSQL_DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"LOAD\s+DATA",
        r"SET\s+GLOBAL",
        r"SET\s+SESSION",
        # A variable set for one statement only: MariaDB's SET STATEMENT ... FOR and MySQL's
        # SET_VAR optimizer hint. Either can change sql_mode (ANSI_QUOTES,
        # NO_BACKSLASH_ESCAPES) behind the mode the adapter read at connect, or lift the
        # statement time limit. Checked on the raw text, so a string naming them is refused.
        rf"\bSET{_GAP}++STATEMENT\b",
        rf"\bSET_VAR{_GAP}*+\(",
        r"FLUSH\s+",
    ]

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run base validation then add MySQL-specific checks."""
        result = super().validate(query, default_limit)
        if not result.is_valid:
            return result

        # The scans read the text the base validator judged (no leading byte order mark).
        query = strip_query(query)
        query_upper = query.upper()

        for func in self.MYSQL_DANGEROUS_FUNCTIONS:
            if re.search(rf"\b{func}\b", query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Query uses a blocked MySQL function: {func}",
                )

        # A value assigned to a user variable (@v := col) is read back as @v under another
        # column in the same statement, past result masking. Checked on the raw text, so
        # ':=' inside a string literal is refused too.
        if ":=" in query:
            return ValidationResult(
                is_valid=False,
                error_message="Query assigns a user variable (:=), which is not allowed",
            )

        # The patterns run on the raw text with executable-comment openers removed, so a
        # comment between two words or a word inside /*! … */ does not hide them.
        scan = _EXECUTABLE_OPENER.sub(" ", query_upper)

        # A second statement after a # comment or inside an executable comment, which the
        # base pattern does not read as gaps (;#x HANDLER …, ; /*! HANDLER … */).
        if has_second_statement(scan, mysql_comments=True):
            return ValidationResult(
                is_valid=False,
                error_message="Multiple statements are not allowed",
            )
        # A share-locking read inside an executable comment or after a # comment.
        if takes_row_locks(scan, mysql_comments=True):
            return ValidationResult(is_valid=False, error_message=LOCKING_READ_ERROR)

        # INTO writes the selected value into a user variable (or a file). The base check
        # reads sqlparse's tokens, and sqlparse takes /*! INTO @x */ and the rest of the line
        # after --1 for a comment while the server runs them: the value then sits in a
        # variable on the pooled connection that a later SELECT @x returns past result
        # masking. When the text holds an executable comment or dashes the server does not
        # read as a comment, INTO is refused anywhere in the scan text, a literal or comment
        # included. Executable comments themselves stay allowed (/*! STRAIGHT_JOIN */), as for
        # every other pattern here; the strict sensitive-column mode refuses them outright.
        if _HIDDEN_FROM_SQLPARSE.search(query) and re.search(r"\bINTO\b", scan):
            return ValidationResult(
                is_valid=False,
                error_message="SELECT … INTO is not allowed: it writes to the database",
            )

        for pattern in self.MYSQL_DANGEROUS_PATTERNS:
            if re.search(pattern, scan, re.DOTALL):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a blocked MySQL pattern",
                )

        return result

    def _pieces(self, statements: tuple[sql_nodes.Statement, ...]) -> list[tuple[str, bool]]:
        """Each token's text and whether the server reads it as a gap, by MySQL's rules.

        A # comment runs to the end of the line, also when sqlparse reads #x as an operator
        and a name; -- is a comment only when whitespace, a control character or the end of
        the text follows (--1 is two minus signs); an executable comment (/*! … */) is code.
        """
        pieces: list[tuple[str, bool]] = []
        in_hash_comment = False
        for statement in statements:
            for token in statement.flatten():
                text = token.value
                if in_hash_comment or (token.ttype in sql_tokens.Operator and text == "#"):
                    in_hash_comment = "\n" not in text
                    pieces.append((text, True))
                    continue
                if token.ttype in sql_tokens.Whitespace:
                    gap = True
                elif token.ttype in sql_tokens.Comment:
                    after_dashes = text[2:3]
                    gap = (
                        text.startswith("#")
                        or (text.startswith("/*") and not _EXECUTABLE_OPENER.match(text))
                        or (
                            text.startswith("--")
                            and (after_dashes == "" or after_dashes.isspace() or after_dashes < " ")
                        )
                    )
                else:
                    gap = False
                pieces.append((text, gap))
        return pieces

    def _leading_statement_error(self, stmt: sql_nodes.Statement, query: str) -> str | None:
        """Judge the first word on the raw text rather than on sqlparse's first token.

        sqlparse reads an executable comment as a comment and a # comment with no space
        after it (#x) as an operator; the server runs the first and skips the second. The
        scan removes the executable-comment openers, skips the comment forms the server
        skips (_GAP), and asks for SELECT, WITH or an opening parenthesis next, so neither
        /*!SET*/ … nor /*!SHOW TABLES WHERE*/ (SELECT 1) passes, and #x then SELECT does.
        """
        scan = _EXECUTABLE_OPENER.sub(" ", query)
        if _LEADING_SET.match(scan):
            return SET_STATEMENT_ERROR
        if _LEADING_READ.match(scan):
            return None
        return not_a_read_error(stmt.get_type())
