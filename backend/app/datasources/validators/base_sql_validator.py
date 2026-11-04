# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from bisect import bisect_left, bisect_right
from collections.abc import Iterator
import re
from typing import ClassVar

import sqlparse
from sqlparse.exceptions import SQLParseError
import sqlparse.sql as sql_nodes
import sqlparse.tokens as sql_tokens

from ..models import ValidationResult

SET_STATEMENT_ERROR = "Query is a SET statement, which is not allowed"


def not_a_read_error(statement_type: str | None) -> str:
    """The refusal for a statement that does not start with a read."""
    return f"Only SELECT queries and CTEs (WITH) are allowed. Got: {statement_type or 'UNKNOWN'}"


LOCKING_READ_ERROR = "Query takes row locks (FOR SHARE, LOCK IN SHARE MODE), which is not allowed"

# A byte order mark an editor left before the query: not a word, and not whitespace to str.strip.
_BYTE_ORDER_MARK = "\ufeff"


def strip_query(query: str) -> str:
    """The query without surrounding whitespace and leading byte order marks."""
    return query.strip().lstrip(_BYTE_ORDER_MARK).strip()


# A keyword glued to the next word ("SELECTday"): the space the model dropped is put back.
_MISSING_SPACE = re.compile(r"(?i)^(SELECT|WITH|INSERT|UPDATE|DELETE)(?=[^\s(])")


class _Gaps:
    """Skips what the server reads as a gap between two words: whitespace and comments.

    A block comment and a -- comment count on every dialect; ``mysql_comments`` adds a #
    comment and the */ that closes an executable comment (whose opener the caller has
    removed), and narrows -- to MySQL's rule: a comment only when whitespace, a control
    character or the end of the text follows (--1 is two minus signs, which the server
    runs). Without the flag -- is always a comment, as PostgreSQL reads it. An unclosed
    block comment runs to the end of the text. The closing */ and the line breaks are found
    once and looked up by bisection, so many scans over one text stay linear.
    """

    def __init__(self, text: str, *, mysql_comments: bool = False) -> None:
        self.text = text
        self.size = len(text)
        self.mysql_comments = mysql_comments
        self.closers = [m.start() for m in re.finditer(r"\*/", text)]
        self.newlines = [m.start() for m in re.finditer(r"\n", text)]

    def _closer_after(self, i: int) -> int:
        """The end of the first */ that starts at or after ``i``, else the end of the text."""
        k = bisect_left(self.closers, i)
        return self.closers[k] + 2 if k < len(self.closers) else self.size

    def _line_end_after(self, i: int) -> int:
        """The end of the first line break after ``i``, else the end of the text."""
        k = bisect_right(self.newlines, i)
        return self.newlines[k] + 1 if k < len(self.newlines) else self.size

    def _dashes_open_a_comment(self, i: int) -> bool:
        """Whether the -- at ``i`` is a comment: always, or by MySQL's rule under the flag."""
        if not self.mysql_comments:
            return True
        after = i + 2
        return after >= self.size or self.text[after].isspace() or self.text[after] < " "

    def _opens_line_comment(self, i: int) -> bool:
        """Whether a -- comment, or under the flag a # comment, opens at ``i``."""
        if self.text.startswith("--", i):
            return self._dashes_open_a_comment(i)
        return self.mysql_comments and self.text[i] == "#"

    def skip(self, i: int) -> int:
        """The position of the first character at or after ``i`` that is not a gap."""
        text, mysql = self.text, self.mysql_comments
        while i < self.size:
            if text[i].isspace():
                i += 1
            elif text.startswith("/*", i):
                i = self._closer_after(i + 2)
            elif self._opens_line_comment(i):
                i = self._line_end_after(i)
            elif mysql and text.startswith("*/", i):
                i += 2
            else:
                break
        return i


_WORD = re.compile(r"\w+")
# The quotes every dialect has: ' opens a string literal and " a string literal (MySQL) or a
# quoted name (PostgreSQL). Each validator owns its set (QUOTE_CHARS), because a quote the
# dialect does not have would hide the words after it: MySQL adds the backtick.
_COMMON_QUOTES = "'\""
# Text whose quotes the scan does not follow: a backslash may escape a quote (MySQL, a
# PostgreSQL E'…' string) and a dollar sign may open a PostgreSQL dollar-quoted string that
# holds a quote. Both are rare in a generated query; every word then counts, which only
# refuses more.
_QUOTES_NOT_FOLLOWED = re.compile(r"[\\$]")


def _quoted_end(text: str, i: int) -> int:
    """The position after the quoted region that opens at ``i``: the next same quote, with
    a doubled quote ('it''s') read as one inside it; the end of the text when unclosed.
    The caller decides which characters open a region (the dialect's quotes)."""
    quote, j, size = text[i], i + 1, len(text)
    while j < size:
        if text[j] == quote:
            if j + 1 < size and text[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    return size


def code_words(
    text: str, *, mysql_comments: bool = False, quotes: str = _COMMON_QUOTES
) -> Iterator[tuple[str, int]]:
    """Each word of *text* the server reads as code, with the position after it.

    A comment is a gap (_Gaps) and a quoted region (a string literal, a quoted name) is
    skipped whole, so the words inside neither count. ``quotes`` are the characters that
    open such a region on the dialect: ' and " everywhere, and the backtick on MySQL only.
    PostgreSQL reads a backtick as an operator character, not a quote, so a scan that
    followed it there would hide the words after an unclosed one (the default leaves it out).
    When the text holds a backslash or a dollar sign (_QUOTES_NOT_FOLLOWED) the quotes are
    read as code and every word counts.
    Linear in the text: each position is visited once, and a gap is skipped by bisection.
    """
    gaps = _Gaps(text, mysql_comments=mysql_comments)
    follow_quotes = _QUOTES_NOT_FOLLOWED.search(text) is None
    i = 0
    while i < gaps.size:
        i = gaps.skip(i)
        if i >= gaps.size:
            break
        if follow_quotes and text[i] in quotes:
            i = _quoted_end(text, i)
            continue
        match = _WORD.match(text, i)
        if match is None:
            i += 1
            continue
        yield match.group(), match.end()
        i = match.end()


def has_second_statement(text: str, *, mysql_comments: bool = False) -> bool:
    """Whether a semicolon is followed by the start of another statement.

    The start is a word character or an opening parenthesis, after any gaps (_Gaps): a
    comment is a gap to the server, so ``;/**/SET`` is two statements as much as ``; SET``.
    Under MySQL's rule ``--1`` is code, not a comment, so the flagged pass stops at it: a run
    of dashes followed by a word character, ``_`` or ``(`` is refused as a possible second
    statement too. Anything else that starts with ``-`` after a semicolon (``--'x'``) cannot
    begin a statement on the server, which rejects it as a syntax error, so it is left to
    the server. Every semicolon counts, one inside a string literal or a comment too
    ('a; b'), which only refuses more; a trailing semicolon, or one followed by comments
    alone, passes.
    """
    gaps = _Gaps(text, mysql_comments=mysql_comments)
    for match in re.finditer(";", text):
        i = gaps.skip(match.end())
        while i < gaps.size and text[i] == "-":
            i += 1
        if i < gaps.size and (text[i].isalnum() or text[i] in "_("):
            return True
    return False


def takes_row_locks(
    text: str, *, mysql_comments: bool = False, quotes: str = _COMMON_QUOTES
) -> bool:
    """Whether the text holds a share-locking clause: FOR [KEY] SHARE, LOCK IN SHARE MODE.

    Read on the words the server reads as code (code_words): a comment between the words
    does not hide the clause, and the words inside a string literal, a quoted name or a
    comment ('Up for share', -- lock in share mode?) do not count, except in a text with a
    backslash or a dollar sign, where every word counts (refuses more). ``quotes`` are the
    dialect's quote characters (see code_words): the backtick is one on MySQL only, and
    PostgreSQL reads it as an operator character, so its words count there. FOR UPDATE and
    FOR NO KEY UPDATE are refused by the UPDATE keyword.
    """
    upper = text.upper()
    gaps = _Gaps(upper, mysql_comments=mysql_comments)

    def follows(i: int, words: tuple[str, ...]) -> bool:
        for word in words:
            match = _WORD.match(upper, gaps.skip(i))
            if match is None or match.group() != word:
                return False
            i = match.end()
        return True

    for word, end in code_words(upper, mysql_comments=mysql_comments, quotes=quotes):
        if word == "FOR":
            if follows(end, ("SHARE",)) or follows(end, ("KEY", "SHARE")):
                return True
        elif word == "LOCK" and follows(end, ("IN", "SHARE", "MODE")):
            return True
    return False


class BaseSQLValidator:
    """Shared SQL safety validation logic for all SQL-based adapters."""

    # The characters that open a string literal or a quoted name on the dialect. The lock
    # scan skips the region they open, so a character the dialect does not quote with must
    # not be listed: PostgreSQL reads a backtick as an operator character (MySQL overrides).
    QUOTE_CHARS: ClassVar[str] = "'\""

    BLOCKED_STATEMENT_TYPES: ClassVar[set[str]] = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "MERGE",
        "REPLACE",
        "CALL",
        "EXECUTE",
        "EXEC",
    }

    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"INTO\s+OUTFILE",
        r"INTO\s+DUMPFILE",
    ]

    # INTO in clause position writes: PostgreSQL's SELECT … INTO creates a table, MySQL's
    # SELECT … INTO @var / OUTFILE / DUMPFILE writes a variable or a file. Checked on
    # sqlparse tokens rather than the raw text so the word inside a comment, a string
    # literal or a quoted identifier ('%logged into%', -- split into buckets) is not
    # rejected — a rejection there would push the correction loop to alter the user's
    # literal. A tokenizer miss is backstopped by the read-only transaction each adapter
    # opens (Task 11).
    WRITING_KEYWORDS: ClassVar[frozenset[str]] = frozenset({"INTO"})

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run shared validation: parse, check type, check patterns, add LIMIT.

        Steps:
        1. Parse with sqlparse (type detection only — not statement count)
        2. Reject an input with no statement
        3. Check the statement's first word after comments: SET is refused with its own
           message, anything but SELECT, WITH or an opening parenthesis is refused
        4. Scan for BLOCKED_STATEMENT_TYPES as whole words in query text
           4b. Reject WRITING_KEYWORDS (INTO) where sqlparse tokenises them as keywords
        5. Reject a second statement (has_second_statement), a share-locking read
           (takes_row_locks) and the DANGEROUS_PATTERNS
        6. Add LIMIT if missing, then return the ValidationResult
        """
        query = strip_query(query)
        if not query:
            return ValidationResult(is_valid=False, error_message="Empty query")

        # Normalise missing whitespace after keywords (e.g. "SELECTday" → "SELECT day")
        query = _MISSING_SPACE.sub(r"\1 ", query)

        # 1. Parse — use sqlparse only for statement-type detection, not statement count.
        # Counting parsed statements to reject "multiple statements" is unreliable:
        # sqlparse misclassifies subqueries inside JOIN (...) as separate statements.
        # A second statement is caught on the text by has_second_statement in step 5.
        # sqlparse >= 0.5.5 raises SQLParseError instead of silently returning an
        # ungrouped tree when its DoS guards trip (grouping depth > 100, > 10 000
        # tokens). Fail closed: a statement we cannot parse is a statement whose
        # type we cannot verify, and this is the gate that keeps non-SELECT SQL out.
        try:
            parsed = sqlparse.parse(query)
        except SQLParseError as e:
            return ValidationResult(
                is_valid=False,
                error_message=f"Query is too complex to validate safely: {e}",
            )
        non_empty = [s for s in parsed if str(s).strip()]

        if not non_empty:
            return ValidationResult(is_valid=False, error_message="Empty query")

        # 3. The first word decides the statement type (see _leading_statement_error).
        type_error = self._leading_statement_error(non_empty[0], query)
        if type_error is not None:
            return ValidationResult(is_valid=False, error_message=type_error)
        dialect_error = self._token_error(parsed)
        if dialect_error is not None:
            return ValidationResult(is_valid=False, error_message=dialect_error)

        query_upper = query.upper().strip()

        # 4. Scan for blocked keywords as whole words
        #    We scan the full text to catch attempts to embed DML inside comments
        #    or disguise them. Accept that quoted identifiers named after keywords
        #    (e.g., "delete") will also be rejected — this is an intentional
        #    safety-first tradeoff for an MVP.
        for blocked in self.BLOCKED_STATEMENT_TYPES:
            if re.search(rf"\b{blocked}\b", query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Query contains blocked keyword: {blocked}",
                )

        # 4b. Keywords that write when they are SQL, checked on tokens so the same word in
        #     a comment, literal or quoted identifier is not mistaken for a clause.
        for statement in non_empty:
            for token in statement.flatten():
                if (
                    token.ttype in sql_tokens.Keyword
                    and token.normalized.upper() in self.WRITING_KEYWORDS
                ):
                    return ValidationResult(
                        is_valid=False,
                        error_message="SELECT … INTO is not allowed: it writes to the database",
                    )

        # 5. A second statement, a share-locking read (it takes row locks other sessions
        #    wait on, as FOR UPDATE, which the UPDATE keyword refuses), dangerous patterns
        if has_second_statement(query):
            return ValidationResult(
                is_valid=False,
                error_message="Multiple statements are not allowed",
            )
        if takes_row_locks(query, quotes=self.QUOTE_CHARS):
            return ValidationResult(is_valid=False, error_message=LOCKING_READ_ERROR)
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a dangerous pattern",
                )

        # 6. Add LIMIT if missing, after the last code token: a trailing semicolon and the
        #    comments and whitespace after the statement go first, or "SELECT 1; -- c" would
        #    get the LIMIT as a second statement.
        sanitized = query
        if not re.search(r"\bLIMIT\b", query, re.IGNORECASE):
            sanitized = f"{self._without_trailing_gaps(parsed)}\nLIMIT {default_limit}"

        return ValidationResult(is_valid=True, sanitized_query=sanitized)

    def _without_trailing_gaps(self, statements: tuple[sql_nodes.Statement, ...]) -> str:
        """The query text up to its last code token: no trailing ;, comment or whitespace.

        Built from sqlparse's tokens, so a ; or -- inside a string literal is code and stays.
        """
        pieces = self._pieces(statements)
        end = len(pieces)
        while end and (pieces[end - 1][1] or pieces[end - 1][0] == ";"):
            end -= 1
        return "".join(text for text, _ in pieces[:end])

    def without_trailing_gaps(self, query: str) -> str | None:
        """*query* up to its last code token, read as ``validate`` reads it (stripped, the
        missing space after a leading keyword put back): no trailing ;, comment or
        whitespace. ``None`` when sqlparse refuses to parse it or nothing is left. One
        parse and no checks: for a query ``validate`` has already accepted, where only the
        place for a LIMIT is wanted (``services.row_filter._with_row_limit``).
        """
        query = _MISSING_SPACE.sub(r"\1 ", strip_query(query))
        try:
            parsed = sqlparse.parse(query)
        except SQLParseError:
            return None
        return self._without_trailing_gaps(parsed) or None

    def _pieces(self, statements: tuple[sql_nodes.Statement, ...]) -> list[tuple[str, bool]]:
        """Each token's text and whether the server reads it as a gap (whitespace, comment).

        A comment is one that opens with -- or /*; sqlparse also reads # … as a comment,
        which PostgreSQL reads as the XOR operator, so that one counts as code.
        """
        return [
            (
                token.value,
                token.ttype in sql_tokens.Whitespace
                or (token.ttype in sql_tokens.Comment and token.value[:2] in ("--", "/*")),
            )
            for statement in statements
            for token in statement.flatten()
        ]

    def _token_error(self, statements: tuple[sql_nodes.Statement, ...]) -> str | None:
        """A dialect's refusal read off sqlparse's tokens (the parse is done once, here)."""
        return None

    def _leading_statement_error(self, stmt: sql_nodes.Statement, query: str) -> str | None:
        """Refuse a statement whose first word after comments is not a read.

        Only SELECT, WITH and an opening parenthesis (a parenthesised query, a set
        operation of parenthesised queries) may start the statement. The words elsewhere do
        not count: RESET, LOCK, DISCARD, HANDLER or SHOW with SELECT in a trailing comment
        would otherwise run as written. Judged on sqlparse's first token. Where sqlparse and
        the server disagree on a comment, the dialect validators step in: the PostgreSQL one
        refuses a nested block comment, the MySQL one overrides this with a scan of the raw
        text.

        SET gets its own message: it changes the session (statement_timeout, sql_mode, the
        role) for every later query on the pooled connection.
        """
        first = stmt.token_first(skip_cm=True, skip_ws=True)
        if first is not None and first.ttype in sql_tokens.Keyword and first.normalized == "SET":
            return SET_STATEMENT_ERROR
        is_read = first is not None and (
            (first.ttype in sql_tokens.Keyword.DML and first.normalized == "SELECT")
            or first.ttype in sql_tokens.Keyword.CTE
            or isinstance(first, sql_nodes.Parenthesis)
        )
        if is_read:
            return None
        return not_a_read_error(stmt.get_type())
