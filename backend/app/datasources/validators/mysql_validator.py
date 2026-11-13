# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB-specific query validator."""

from collections.abc import Iterator
import re
from typing import ClassVar

import sqlparse.sql as sql_nodes
import sqlparse.tokens as sql_tokens

from ..models import ValidationResult
from .base_sql_validator import (
    _WORD,
    EXECUTABLE_OPENER,
    LOCKING_READ_ERROR,
    MULTIPLE_STATEMENTS_ERROR,
    SET_STATEMENT_ERROR,
    BaseSQLValidator,
    _Gaps,
    code_pieces,
    has_second_statement,
    mysql_dashes_open_a_comment,
    not_a_read_error,
    strip_query,
    takes_row_locks,
)

# A gap between two words, for the two MYSQL_DANGEROUS_PATTERNS that are matched on the raw
# text (SET STATEMENT, SET_VAR): whitespace, a /* … */ comment, a -- or # comment to the end
# of the line, and the */ that closes an executable comment (whose opener the pattern scan
# replaces with a space first). Deliberately wider than the server's rules (it needs a space
# after --), which only refuses more. This is the one place a MySQL gap is spelled as a
# pattern: SET_VAR sits inside an optimizer hint, which is a comment, so a scan that skips
# comments cannot find it. Every other scan, the region before the first word included,
# reads the server's own rules from _Gaps. The alternatives overlap ("/*/" is a "/*" opener
# or a "*/" closer), so the group is atomic and every quantifier over it possessive: once
# a gap is matched it is never re-split, and a long run of such pieces cannot make the
# scan backtrack exponentially.
_GAP = r"(?>\s|/\*.*?\*/|\*/|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$))"
# The refusal for a non-empty executable comment before the first word. sqlparse reads the
# comment as a comment, so the not-a-read message would name the SELECT after it (Got:
# SELECT) and send the correction loop back with the same query. A comment whose content
# opens with another statement (SHOW, HANDLER) gets the not-a-read message naming that word.
EXECUTABLE_COMMENT_ERROR = (
    "Query opens with an executable comment (/*! or /*M!), which is not allowed"
)
# The refusal for an executable comment the server may skip, anywhere in the query.
GATED_COMMENT_ERROR = (
    "Query holds a versioned or MariaDB-only executable comment (/*!NNNNN or /*M!), "
    "which is not allowed"
)
# The one empty executable comment (whitespace alone: a version number is content), and one
# whose content opens with SET. Each is matched against a single comment, from its opener to
# its first */. Case-insensitive, which only refuses more (/*m! too).
_EMPTY_EXECUTABLE = re.compile(r"/\*M?!\s*\*/", re.IGNORECASE)
_EXECUTABLE_SET = re.compile(r"/\*M?!\d*\s*SET\b", re.IGNORECASE)
# The first word of an executable comment's content, after its version number.
_EXECUTABLE_FIRST_WORD = re.compile(r"/\*M?!\d*\s*([A-Z_]\w*)", re.IGNORECASE)
# An executable comment the server may skip: /*!NNNNN runs only on a server of at least that
# version, and MySQL skips MariaDB's /*M! entirely. Every scan here reads an executable
# comment's content as code, which is the server's reading only for a comment it always
# runs (/*! … */ with no version). One it may skip has two readings, and a mixture of the
# two kinds has readings no single scan shows: the quote in /*!99999 ' */ hides the
# /*! INTO @v */ after it from a scan that runs both, while a server that skips the first
# runs the second. So these are refused wherever they stand, except an empty /*M!*/, which
# is nothing on either server. A digit straight after the ! is taken for a version whatever
# its length. Matched on the raw text, so one inside a string literal is refused too.
_GATED_COMMENT = re.compile(r"/\*(?:M!(?!\s*\*/)|!\d)", re.IGNORECASE)


class MySQLValidator(BaseSQLValidator):
    """MySQL/MariaDB-specific query validator extending the shared base."""

    # MySQL quotes a name with a backtick as well; the lock and INTO scans skip that region.
    QUOTE_CHARS: ClassVar[str] = "'\"`"

    # A hash line is a gap, --1 is code and an executable comment's content is code. The only
    # executable comments a query that validates may hold are the ones the server always runs
    # (see _GATED_COMMENT), so a row limit inside one is a clause the server runs.
    MYSQL_COMMENTS: ClassVar[bool] = True

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

        # A second statement after a # comment or inside an executable comment, which the
        # base scan does not read as gaps (;#x HANDLER …, ; /*! HANDLER … */). Read by
        # MySQL's comment rules on the text itself (_Gaps).
        if has_second_statement(query_upper, mysql_comments=True):
            return ValidationResult(
                is_valid=False,
                error_message=MULTIPLE_STATEMENTS_ERROR,
            )
        # A share-locking read inside an executable comment, after a # comment or after
        # dashes the server does not read as a comment (--1 FOR SHARE), which the base
        # scan reads as gaps or a comment.
        if takes_row_locks(query_upper, mysql_comments=True, quotes=self.QUOTE_CHARS):
            return ValidationResult(is_valid=False, error_message=LOCKING_READ_ERROR)

        # The patterns run on the raw text with executable-comment openers replaced by a
        # space, so a comment between two words or a word inside /*! … */ does not hide
        # them. Only the patterns read this copy: they follow no comment rule of their own,
        # and a scan that does must not (see _Gaps).
        scan = EXECUTABLE_OPENER.sub(" ", query_upper)
        for pattern in self.MYSQL_DANGEROUS_PATTERNS:
            if re.search(pattern, scan, re.DOTALL):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a blocked MySQL pattern",
                )

        # Last, so a query that is refused for what a comment holds (INTO, a share lock, a
        # second statement) keeps that message.
        if _GATED_COMMENT.search(query):
            return ValidationResult(is_valid=False, error_message=GATED_COMMENT_ERROR)

        return result

    def _code_pieces(self, text: str) -> Iterator[tuple[str, int]]:
        """The pieces the server reads as code by MySQL's rules: a # line is a gap, --1 is
        code, an executable comment's content is code, and a backtick quotes a name.

        The base TABLE check reads the pieces and the INTO check the words among them. INTO
        writes the selected value into a user variable (or a file), where it sits on the
        pooled connection for a later SELECT @x to return past result masking, and sqlparse
        does not show every INTO the server runs: it takes /*! INTO @x */ and the rest of
        the line after --1 for a comment, reads INTO#x then @v on the next line as one name,
        and reads the INTO of SELECT 1.INTO @v as a name after a dot.
        """
        return code_pieces(text, mysql_comments=True, quotes=self.QUOTE_CHARS)

    def _pieces(self, statements: tuple[sql_nodes.Statement, ...]) -> list[tuple[str, bool]]:
        """Each token's text and whether the server reads it as a gap, by MySQL's rules.

        A # comment runs to the end of the line, also when sqlparse reads #x as an operator
        and a name; -- is a comment only when an ASCII whitespace or control character or
        the end of the text follows (--1 is two minus signs; mysql_dashes_open_a_comment);
        an executable comment (/*! … */) is code.
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
                    gap = (
                        text.startswith("#")
                        or (text.startswith("/*") and not EXECUTABLE_OPENER.match(text))
                        or (text.startswith("--") and mysql_dashes_open_a_comment(text[2:3]))
                    )
                else:
                    gap = False
                pieces.append((text, gap))
        return pieces

    def _leading_statement_error(self, stmt: sql_nodes.Statement, query: str) -> str | None:
        """Judge the first word on the raw text rather than on sqlparse's first token.

        sqlparse reads an executable comment as a comment and a hash comment with no space
        after it as an operator. The server skips the second; whether it runs the first is
        decided by the comment itself: a versioned comment runs only on a server of at least
        that version, and MySQL skips a MariaDB-only one. Before the first word, a mixture
        of comments the server runs and skips can lead with a statement that neither the
        content read as code nor the content read as a gap shows, so an executable comment
        there is refused (the earlier validator read every one as code): with the SET message
        when its content or the raw text opens with SET, the not-a-read message naming the
        word when its content opens with another statement, and EXECUTABLE_COMMENT_ERROR
        otherwise, since sqlparse reads the comment as a comment and the not-a-read message
        would name the SELECT after it. Content with no word changes the reading
        too: a quote in a comment the server runs hides the comment's own closer, and a (,
        a -- or a ; moves what follows. Only an empty one (whitespace alone) is a gap.
        After that the statement must start with SELECT or WITH on the raw text, past the
        comment forms the server skips and any opening parentheses; a SET there, with no
        parenthesis before it, gets the SET message. Executable comments after the first
        word stay allowed when the server always runs them (SELECT /*! STRAIGHT_JOIN */ …;
        see _GATED_COMMENT). A hash line is a comment to the server whatever it says, so a
        response that opens with a markdown heading and then a SELECT is judged on the
        SELECT.

        The region before the first word is walked with the gap rules every other MySQL
        scan reads (_Gaps), stopping at each executable comment to judge it.
        """
        gaps = _Gaps(query, mysql_comments=True)
        comment: str | None = None  # the first executable comment there that is not empty
        parenthesised = False
        i = 0
        while True:
            # skip stops where executable_end sees an opener: both read EXECUTABLE_OPENER.
            i = gaps.skip(i, stop_at_executable=True)
            end = gaps.executable_end(i)
            if end is not None:
                if comment is None and not _EMPTY_EXECUTABLE.fullmatch(query, i, end):
                    comment = query[i:end]
                i = end
            elif query.startswith("(", i):
                parenthesised = True
                i += 1
            else:
                break
        word = _WORD.match(query, i)
        first = word.group().upper() if word is not None else ""
        leading_set = first == "SET" and not parenthesised
        if comment is not None:
            if leading_set or _EXECUTABLE_SET.match(comment):
                return SET_STATEMENT_ERROR
            content = _EXECUTABLE_FIRST_WORD.match(comment)
            if content is not None and content.group(1).upper() not in ("SELECT", "WITH"):
                return not_a_read_error(content.group(1).upper())
            return EXECUTABLE_COMMENT_ERROR
        if leading_set:
            return SET_STATEMENT_ERROR
        if first in ("SELECT", "WITH"):
            return None
        return not_a_read_error(stmt.get_type())
