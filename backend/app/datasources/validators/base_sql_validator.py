# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from bisect import bisect_left, bisect_right
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


class _Gaps:
    """Skips what the server reads as a gap between two words: whitespace and comments.

    A block comment and a -- comment count on every dialect (-- without whitespace after it
    as well, which only refuses more); ``mysql_comments`` adds a # comment and the */ that
    closes an executable comment (whose opener the caller has removed). An unclosed block
    comment runs to the end of the text. The closing */ and the line breaks are found once
    and looked up by bisection, so many scans over one text stay linear.
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

    def skip(self, i: int) -> int:
        """The position of the first character at or after ``i`` that is not a gap."""
        text, mysql = self.text, self.mysql_comments
        while i < self.size:
            if text[i].isspace():
                i += 1
            elif text.startswith("/*", i):
                i = self._closer_after(i + 2)
            elif text.startswith("--", i) or (mysql and text[i] == "#"):
                i = self._line_end_after(i)
            elif mysql and text.startswith("*/", i):
                i += 2
            else:
                break
        return i


_WORD = re.compile(r"\w+")


def has_second_statement(text: str, *, mysql_comments: bool = False) -> bool:
    """Whether a semicolon is followed by the start of another statement.

    The start is a word character or an opening parenthesis, after any gaps (_Gaps): a
    comment is a gap to the server, so ``;/**/SET`` is two statements as much as ``; SET``.
    Every semicolon counts, one inside a string literal or a comment too ('a; b'), which
    only refuses more; a trailing semicolon, or one followed by comments alone, passes.
    """
    gaps = _Gaps(text, mysql_comments=mysql_comments)
    for match in re.finditer(";", text):
        i = gaps.skip(match.end())
        if i < gaps.size and (text[i].isalnum() or text[i] in "_("):
            return True
    return False


def takes_row_locks(text: str, *, mysql_comments: bool = False) -> bool:
    """Whether the text holds a share-locking clause: FOR [KEY] SHARE, LOCK IN SHARE MODE.

    Words are read on the whole text with gaps (_Gaps) between them, so a comment between
    the words does not hide the clause, and the words inside a literal or a comment count as
    well ('for share'), which only refuses more. FOR UPDATE and FOR NO KEY UPDATE are
    refused by the UPDATE keyword.
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

    for match in re.finditer(r"\b(FOR|LOCK)\b", upper):
        if match.group() == "FOR":
            if follows(match.end(), ("SHARE",)) or follows(match.end(), ("KEY", "SHARE")):
                return True
        elif follows(match.end(), ("IN", "SHARE", "MODE")):
            return True
    return False


class BaseSQLValidator:
    """Shared SQL safety validation logic for all SQL-based adapters."""

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
        query = re.sub(r"(?i)^(SELECT|WITH|INSERT|UPDATE|DELETE)(?=[^\s(])", r"\1 ", query)

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
        if takes_row_locks(query):
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
