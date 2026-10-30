# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB-specific query validator."""

import re
from typing import ClassVar

import sqlparse
from sqlparse.exceptions import SQLParseError
import sqlparse.tokens as sql_tokens

from ..models import ValidationResult
from .base_sql_validator import BaseSQLValidator

# What the server reads as a gap between two words: whitespace, a /* … */ comment, a -- or #
# comment to the end of the line, and the */ that closes an executable comment (opened by
# _EXECUTABLE_OPENER, which the scan removes first). Wider than the server's rules (it needs
# a space after --), which only refuses more. The alternatives overlap ("/*/" is a "/*"
# opener or a "*/" closer), so the group is atomic and every quantifier over it possessive:
# once a gap is matched it is never re-split, and a long run of such pieces cannot make the
# scan backtrack exponentially.
_GAP = r"(?>\s|/\*.*?\*/|\*/|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$))"
# A statement that starts with SET, after any gaps (a # comment with no space after it
# included, which sqlparse reads as an operator).
_LEADING_SET = re.compile(rf"(?:{_GAP})*+SET\b", re.IGNORECASE | re.DOTALL)
# The opener of a MySQL or MariaDB executable comment, with its optional version number: the
# server runs the content, so for the pattern scan it is code, not a comment.
_EXECUTABLE_OPENER = re.compile(r"/\*M?!\d*")


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

        # A SET statement changes the session (sql_mode, the time limit) for every later
        # query on the pooled connection. The base check lets one through when its value is a
        # subquery (SET sql_mode = (SELECT ...)), since the text mentions SELECT. Checked on
        # the scan text, so a SET inside an executable comment (/*!SET*/ …) or after a #
        # comment counts, and on sqlparse's first token as well.
        if _LEADING_SET.match(scan):
            return ValidationResult(
                is_valid=False,
                error_message="Query is a SET statement, which is not allowed",
            )
        try:
            statements = sqlparse.parse(query)
        except SQLParseError as e:
            return ValidationResult(
                is_valid=False, error_message=f"Query is too complex to validate safely: {e}"
            )
        first = next(
            (
                stmt.token_first(skip_cm=True, skip_ws=True)
                for stmt in statements
                if str(stmt).strip()
            ),
            None,
        )
        if first is not None and first.ttype in sql_tokens.Keyword and first.normalized == "SET":
            return ValidationResult(
                is_valid=False,
                error_message="Query is a SET statement, which is not allowed",
            )

        for pattern in self.MYSQL_DANGEROUS_PATTERNS:
            if re.search(pattern, scan, re.DOTALL):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a blocked MySQL pattern",
                )

        return result
