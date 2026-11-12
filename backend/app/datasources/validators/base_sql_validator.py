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
TABLE_STATEMENT_ERROR = (
    "Query reads a table with TABLE, which is not allowed: name the table in FROM instead"
)

# A byte order mark an editor left before the query: not a word, and not whitespace to str.strip.
_BYTE_ORDER_MARK = "\ufeff"


def strip_query(query: str) -> str:
    """The query without surrounding whitespace and leading byte order marks."""
    return query.strip().lstrip(_BYTE_ORDER_MARK).strip()


# A keyword glued to the next word ("SELECTday"): the space the model dropped is put back.
_MISSING_SPACE = re.compile(r"(?i)^(SELECT|WITH|INSERT|UPDATE|DELETE)(?=[^\s(])")

# The word LIMIT anywhere in the text counts as a row limit the query already carries, as it
# always has (a comment or a literal naming it leaves the LIMIT off). The FETCH clause is
# read more strictly: see has_fetch_clause.
_LIMIT_WORD = re.compile(r"\bLIMIT\b", re.IGNORECASE)

# The opener of a MySQL or MariaDB executable comment, with its optional version number. The
# server runs the content, so under MySQL's comment rules the opener alone is a gap and the
# content is code (_Gaps). Case-insensitive: reading /*m! as code only refuses more. Result
# masking and the strict guard keep their own copy (``services.result_masking.
# EXECUTABLE_COMMENT``); keep the two in step.
EXECUTABLE_OPENER = re.compile(r"/\*M?!\d*", re.IGNORECASE)


def mysql_dashes_open_a_comment(after: str) -> bool:
    """Whether MySQL reads -- followed by *after* (the next character, empty at the end of
    the text) as a comment: only when that is an ASCII whitespace or control character.

    MySQL tests the byte with its own classes (my_isspace, my_iscntrl), which hold 0x00 to
    0x20 and 0x7f and nothing else. Python's isspace differs both ways: it takes a no-break
    space, an ideographic space or a next-line character, which MySQL reads as a name
    character (so --, a no-break space and INTO @v is two minus signs and a clause the
    server runs), and it does not take DEL, after which MySQL opens a comment.
    """
    return after == "" or after <= " " or after == "\x7f"


class _Gaps:
    """Skips what the server reads as a gap between two words: whitespace and comments.

    A block comment and a -- comment count on every dialect; ``mysql_comments`` adds a #
    comment, reads an executable comment (/*! … */, /*M! … */) as code between two gaps (its
    opener and its */), and narrows -- to MySQL's rule: a comment only when an ASCII
    whitespace or control character or the end of the text follows (--1 is two minus signs,
    which the server runs; see mysql_dashes_open_a_comment). The rules are read off the
    text itself, never off a copy with the openers replaced: a space in an opener's place
    turns the dashes of --/*! into a comment, and the content the server runs after them is
    lost. Without the flag -- is always a comment, as PostgreSQL reads it, and it ends at a
    carriage return as well as at a line feed, as PostgreSQL ends it. Ending it there does
    not only read more as code: a quote between the two would open, for this reading, a
    literal that hides the words after it from a scan (-- c\\r' \\nFOR SHARE 'x' locks on a
    server that ends the comment at the line feed). MySQL ends it at a line feed only and
    keeps its own flag, which reads it so. An unclosed block comment runs to the end of
    the text. The closing */ and the line breaks are found once and looked up by
    bisection, so many scans over one text stay linear.
    """

    def __init__(self, text: str, *, mysql_comments: bool = False) -> None:
        self.text = text
        self.size = len(text)
        self.mysql_comments = mysql_comments
        self.closers = [m.start() for m in re.finditer(r"\*/", text)]
        line_break = r"\n" if mysql_comments else r"[\n\r]"
        self.newlines = [m.start() for m in re.finditer(line_break, text)]

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
        return mysql_dashes_open_a_comment(self.text[i + 2 : i + 3])

    def _opens_line_comment(self, i: int) -> bool:
        """Whether a -- comment, or under the flag a # comment, opens at ``i``."""
        if self.text.startswith("--", i):
            return self._dashes_open_a_comment(i)
        return self.mysql_comments and self.text[i] == "#"

    def executable_end(self, i: int) -> int | None:
        """The position after the */ of the executable comment that opens at ``i``; ``None``
        when none opens there or it is never closed."""
        if EXECUTABLE_OPENER.match(self.text, i) is None:
            return None
        k = bisect_left(self.closers, i + 2)
        return self.closers[k] + 2 if k < len(self.closers) else None

    def skip(self, i: int, *, stop_at_executable: bool = False) -> int:
        """The position of the first character at or after ``i`` that is not a gap.

        Under MySQL's rules an executable comment's opener is a gap and its content is not;
        with ``stop_at_executable`` the scan stops at the opener instead, for the caller
        that judges the comment itself (the region before the first word).
        """
        text, mysql = self.text, self.mysql_comments
        while i < self.size:
            if text[i].isspace():
                i += 1
            elif text.startswith("/*", i):
                opener = EXECUTABLE_OPENER.match(text, i) if mysql else None
                if opener is None:
                    i = self._closer_after(i + 2)
                elif stop_at_executable:
                    break
                else:
                    i = opener.end()
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
# Text whose quotes the scan does not follow, because where a quoted region ends depends on
# the server: a backslash directly before a quote character escapes it on MySQL and in a
# PostgreSQL E'…' string and does not elsewhere, and $tag$ opens a PostgreSQL dollar-quoted
# string that may hold a quote. The tag is the server's: empty, or a letter, an underscore
# or any non-ASCII character, then those and digits. A tag of word characters only would
# miss a tag such as a euro sign, and the quote inside that string would then read as
# opening a literal that hides the clause after it. The tag's tail is one character class,
# never two overlapping alternatives (a word character or a non-ASCII one): every
# non-ASCII letter is both, and a run of them with no closing $ made that form backtrack
# exponentially on a query no one would call long. The INTO check and the share-lock scan
# then take any word for the clause's start, a literal's or a comment's too (_into_is_code,
# takes_row_locks), which only refuses more. A backslash anywhere else ('\s+', '\d') and a
# dollar sign that opens no such string ($1, '$.address', "$path") move no region's end, so
# they are not listed: listing them refused the word INTO in a comment or a literal of
# every query that held a regular expression or a JSON path.
_QUOTES_NOT_FOLLOWED = re.compile(r"\\['\"`]|\$(?:(?:[^\W\d]|[^\x00-\x7f])[\w\x80-\U0010ffff]*)?\$")
# The exponent of a number, as the head of a word: the E5 of E5INTO.
_EXPONENT = re.compile(r"E\d+")


def _reads_as(word: str, keyword: str) -> bool:
    """Whether the server may read the upper-case *word* as *keyword*: the word itself, or
    the keyword glued to the number before it.

    PostgreSQL 14 and older lex 1INTO as the number and then the keyword. MySQL reads that
    one as a name, but a number that ends in an exponent as the number and then the
    keyword: 1E5INTO, and 7.E5INTO, where the dot ends the word before it and the scan is
    left with E5INTO. So the keyword counts behind a head that starts with a digit or is an
    exponent. A name of that shape (a column called e5into) is refused with it, which only
    refuses more.
    """
    if not word.endswith(keyword):
        return False
    head = word[: -len(keyword)]
    return not head or head[0].isdigit() or _EXPONENT.fullmatch(head) is not None


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


def _code_pieces(
    text: str, *, mysql_comments: bool = False, quotes: str = _COMMON_QUOTES
) -> Iterator[tuple[str, int]]:
    """Each piece of *text* the server reads as code, with the position after it: a word,
    or one character that is part of none (a parenthesis, an operator character).

    A comment is a gap (_Gaps) and a quoted region (a string literal, a quoted name) is
    skipped whole, so nothing inside either counts. ``quotes`` are the characters that
    open such a region on the dialect: ' and " everywhere, and the backtick on MySQL only.
    PostgreSQL reads a backtick as an operator character, not a quote, so a scan that
    followed it there would hide the words after an unclosed one (the default leaves it out).
    When the text holds a backslash before a quote or a dollar-quote opener
    (_QUOTES_NOT_FOLLOWED) the quotes are read as code, so a literal's words count; a
    comment is still a gap, so a comment opener inside a literal ('$$/*') hides the words
    after it, which is why the INTO check and the share-lock scan read every word there.
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
            yield text[i], i + 1
            i += 1
            continue
        yield match.group(), match.end()
        i = match.end()


def code_words(
    text: str, *, mysql_comments: bool = False, quotes: str = _COMMON_QUOTES
) -> Iterator[tuple[str, int]]:
    """Each word of *text* the server reads as code, with the position after it: the
    words among _code_pieces, which sets out what counts as code."""
    for piece, end in _code_pieces(text, mysql_comments=mysql_comments, quotes=quotes):
        if piece[0] == "_" or piece[0].isalnum():
            yield piece, end


# The words that join two queries into one: a parenthesised query before one of them is a
# branch, not the statement's body.
_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT", "MINUS"})


def _closing_parenthesis(pieces: list[str], opener: int, high: int) -> int:
    """The index of the parenthesis that closes the one at ``opener``, looking no further
    than ``high``; -1 when it is not closed there."""
    depth = 0
    for k in range(opener, high + 1):
        if pieces[k] == "(":
            depth += 1
        elif pieces[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return -1


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

    The clause is its words with nothing but gaps between them (_Gaps), so a comment
    between the words does not hide it, and its first word also counts glued to a number
    (1FOR SHARE, 1.E0FOR SHARE, see _reads_as). Where it may start is read on the words
    the server reads as code (code_words): the words inside a string literal, a quoted name
    or a comment ('Up for share', -- lock in share mode?) start none. ``quotes`` are the
    dialect's quote characters (see code_words): the backtick is one on MySQL only, and
    PostgreSQL reads it as an operator character, so its words count there.

    In a text with a backslash before a quote or a dollar-quote opener
    (_QUOTES_NOT_FOLLOWED) the quotes are not followed, so a literal holding a comment
    opener ('$$/*') would read as a comment and hide the clause after it, while the server
    ends the literal at its quote. There the clause may start at any word, a literal's or a
    comment's too (refuses more), and is read on from that word by the same gap rules,
    which are the server's: between the words of a clause it runs there is nothing but
    whitespace and comments, wherever its first word stands. Words apart make no clause
    ('for' AS f, share). FOR UPDATE and FOR NO KEY UPDATE are refused by the UPDATE keyword.
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

    starts: Iterator[tuple[str, int]]
    if _QUOTES_NOT_FOLLOWED.search(upper):
        starts = ((match.group(), match.end()) for match in _WORD.finditer(upper))
    else:
        starts = code_words(upper, mysql_comments=mysql_comments, quotes=quotes)
    for word, end in starts:
        if _reads_as(word, "FOR"):
            if follows(end, ("SHARE",)) or follows(end, ("KEY", "SHARE")):
                return True
        elif _reads_as(word, "LOCK") and follows(end, ("IN", "SHARE", "MODE")):
            return True
    return False


# The ASCII characters a server takes inside a name that \w does not: $ after a name's first
# character (PostgreSQL, MySQL) and @ as MySQL's user-variable sigil. Glued before FETCH, each
# makes it part of a name. # is listed too: glued before FETCH it starts no clause on either
# server (see _opens_fetch_clause), so reading it as name glue costs nothing.
_NAME_GLUE = "$@#"


def _opens_fetch_clause(text: str, code: list[tuple[str, int]], k: int, gaps: _Gaps) -> bool:
    """Whether the FETCH at ``code[k]`` is the keyword and FIRST or NEXT follows it.

    It is a name in three shapes, which the servers run unbounded: after a dot (s.fetch
    FIRST is a column, even for a reserved word, and its alias); with a non-ASCII character
    glued to it, which PostgreSQL and MySQL read as a name character, so FETCH, a no-break
    space and FIRST is one name; and behind a character of _NAME_GLUE (a$fetch FIRST, and
    @fetch FIRST, a MySQL user variable). Any other character glued after it already stops
    FIRST from following. Reading a name as the keyword only adds the LIMIT, the safe
    direction: a statement that does end in the clause then fails as a syntax error. On
    PostgreSQL # and @ are operators, which no reserved FETCH may follow, and on MySQL # opens
    a comment, so the glue costs no real clause there.
    """
    end = code[k][1]
    start = end - len("FETCH")
    if k and code[k - 1][0] == ".":
        return False
    before = text[start - 1] if start else ""
    if before and (before in _NAME_GLUE or not before.isascii()):
        return False
    if end < len(text) and not text[end].isascii():
        return False
    match = _WORD.match(text, gaps.skip(end))
    return match is not None and match.group() in ("FIRST", "NEXT")


def has_fetch_clause(
    text: str, *, mysql_comments: bool = False, quotes: str = _COMMON_QUOTES
) -> bool:
    """Whether the statement itself carries the standard row-limiting clause: FETCH FIRST
    or NEXT as code, outside every parenthesis but the ones around the statement's own body.

    A LIMIT appended after the statement's own clause is a syntax error (PostgreSQL,
    MariaDB), so a query that carries it gets none. Nothing else counts, because on the
    non-streaming execute path the LIMIT is the only bound on the rows read into memory:

    - the word FETCH in a comment, a string literal or a name (-- Fetch every order, a
      column called fetch) still gets the LIMIT; the clause is read on the pieces the
      server reads as code (_code_pieces);
    - a clause inside parentheses (a subquery, a CTE, one branch of a set operation)
      bounds that subquery and not the rows the statement returns, and a LIMIT after the
      closing parenthesis is valid, so it still gets the LIMIT. The exception is the
      parentheses around the statement's own body: (SELECT … FETCH FIRST 5 ROWS ONLY),
      WITH c AS (…) (SELECT … FETCH FIRST 5 ROWS ONLY) and (SELECT … FETCH FIRST 5 ROWS
      ONLY) ORDER BY a. The clause is the statement's own there, and PostgreSQL refuses a
      LIMIT after each. The body is the parenthesis at depth 0 that opens the statement
      or directly follows a closing parenthesis (the end of a WITH clause), unless a set
      operator follows its closing parenthesis; the scan then repeats inside it;
    - in a text whose quotes are not followed (_QUOTES_NOT_FOLLOWED) a parenthesis inside
      a literal would be counted, so no clause is recognised and the LIMIT stays. A
      statement that does end in the clause then fails on the server with a syntax error
      instead of running unbounded;
    - FETCH as part of a name still gets the LIMIT (_opens_fetch_clause).
    """
    upper = text.upper()
    if _QUOTES_NOT_FOLLOWED.search(upper):
        return False
    gaps = _Gaps(upper, mysql_comments=mysql_comments)
    code = list(_code_pieces(upper, mysql_comments=mysql_comments, quotes=quotes))
    pieces = [piece for piece, _ in code]
    low, high = 0, len(pieces) - 1
    while high >= 0 and pieces[high] == ";":
        high -= 1
    while low <= high:
        depth, body = 0, -1
        for k in range(low, high + 1):
            piece = pieces[k]
            if piece == "(":
                if depth == 0 and (k == low or pieces[k - 1] == ")"):
                    body = k
                depth += 1
            elif piece == ")":
                depth -= 1
            elif piece == "FETCH" and depth == 0 and _opens_fetch_clause(upper, code, k, gaps):
                return True
        closer = _closing_parenthesis(pieces, body, high) if body >= 0 else -1
        if closer < 0 or (closer < high and pieces[closer + 1] in _SET_OPERATORS):
            return False
        low, high = body + 1, closer - 1
    return False


def nests_block_comment(text: str, *, quotes: str = _COMMON_QUOTES) -> bool:
    """Whether a block comment holds a second /*, where PostgreSQL reads the comment.

    PostgreSQL nests block comments and no scan here does: each ends a comment at its
    first */. From there on the two disagree on what is code, and a quote inside the
    server's comment opens what a scan takes for a literal, which hides the clause after
    it (/* /* */ ' */ … FOR SHARE -- '). So a query whose comments nest is refused rather
    than read. The comment is found on the text, by the server's rules, not on sqlparse's
    tokens: sqlparse reads the text after a # as a comment, and […] or a pair of acute
    accents as a quoted name, and shows no comment token inside any of them.

    A -- comment and a quoted region are skipped, as code_words skips them: the
    characters inside either are no comment. In a text whose quotes are not followed
    (_QUOTES_NOT_FOLLOWED) neither is: a -- inside a literal ('--a\\') would read as a line
    comment and hide a nested comment on the rest of the line, which the server, ending
    the literal at its quote, reads as one. There every /* counts, wherever it stands, and
    a second /* before the next */ is refused, a literal's too, which only refuses more.
    """
    gaps = _Gaps(text)
    if _QUOTES_NOT_FOLLOWED.search(text) is not None:
        return any(
            text.find("/*", match.end(), gaps._closer_after(match.end())) != -1
            for match in re.finditer(r"/\*", text)
        )
    i = 0
    while i < gaps.size:
        if text.startswith("/*", i):
            end = gaps._closer_after(i + 2)
            if text.find("/*", i + 2, end) != -1:
                return True
            i = end
        elif gaps._opens_line_comment(i):
            i = gaps._line_end_after(i)
        elif text[i] in quotes:
            i = _quoted_end(text, i)
        else:
            i += 1
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
        "CALL",
        "EXECUTE",
        "EXEC",
    }

    # REPLACE is deliberately not a blocked word: REPLACE(str, from, to) is an ordinary
    # string function on every dialect. MySQL's REPLACE INTO (DML) is refused by the
    # statement rule (its first word is not a read) and by the INTO token rule; the pattern
    # here is the backstop behind both, and it also refuses the two words inside a string
    # literal or a comment.
    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"INTO\s+OUTFILE",
        r"INTO\s+DUMPFILE",
        r"\bREPLACE\s+INTO\b",
    ]

    # INTO in clause position writes: PostgreSQL's SELECT … INTO creates a table, MySQL's
    # SELECT … INTO @var / OUTFILE / DUMPFILE writes a variable or a file. Refused where it
    # is code rather than on the raw text, so the word inside a comment, a string literal
    # or a quoted identifier ('%logged into%', -- split into buckets) is not rejected — a
    # rejection there would push the correction loop to alter the user's literal. See
    # _into_is_code for the readings that decide what is code.
    WRITING_KEYWORDS: ClassVar[frozenset[str]] = frozenset({"INTO"})

    # The words that make the INTO before them part of a read, for a dialect whose INTO
    # can name output columns and write nothing. None on PostgreSQL or MySQL.
    INTO_READ_FOLLOWERS: ClassVar[frozenset[str]] = frozenset()

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run shared validation: parse, check type, check patterns, add LIMIT.

        Steps:
        1. Parse with sqlparse (type detection only — not statement count)
        2. Reject an input with no statement
        3. Check the statement's first word after comments: SET is refused with its own
           message, anything but SELECT, WITH or a parenthesis that opens with one of the
           two is refused
        4. Scan for BLOCKED_STATEMENT_TYPES as whole words in query text
           4b. Reject WRITING_KEYWORDS (INTO) where they are code (_into_is_code)
           4c. Reject the word TABLE where it is code (_table_word_is_code)
        5. Reject a second statement (has_second_statement), a share-locking read
           (takes_row_locks) and the DANGEROUS_PATTERNS
        6. Add LIMIT if the query carries no row limit (the word LIMIT, or the
           statement's own FETCH FIRST/NEXT clause), then return the ValidationResult
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

        # 4b. Keywords that write when they are SQL: refused where they are code, so the
        #     same word in a comment, literal or quoted identifier is not mistaken for a clause.
        if self._into_is_code(query_upper, non_empty):
            return ValidationResult(
                is_valid=False,
                error_message="SELECT … INTO is not allowed: it writes to the database",
            )

        # 4c. TABLE t reads a whole table that no FROM or JOIN names, so the schema check
        #     never sees it: refused wherever it is code, not only at the start.
        if self._table_word_is_code(query_upper):
            return ValidationResult(is_valid=False, error_message=TABLE_STATEMENT_ERROR)

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

        # 6. Add LIMIT if the query carries no row limit, after the last code token: a
        #    trailing semicolon and the comments and whitespace after the statement go
        #    first, or "SELECT 1; -- c" would get the LIMIT as a second statement.
        sanitized = query
        if not self.has_row_limit(query):
            sanitized = f"{self._without_trailing_gaps(parsed)}\nLIMIT {default_limit}"

        return ValidationResult(is_valid=True, sanitized_query=sanitized)

    def _into_is_code(self, upper: str, statements: list[sql_nodes.Statement]) -> bool:
        """Whether a WRITING_KEYWORDS word (INTO) is code on any reading of the query.

        No one reading matches every server, so up to three are taken and one that finds
        the word is enough, which only refuses more:

        - sqlparse's keyword tokens, on every dialect. sqlparse reads # … as a comment where
          PostgreSQL reads an operator (SELECT 1 # 2 INTO t), and the word after a dot as a
          name (SELECT 1.INTO @v, which MySQL reads as the number 1. and then INTO), so on
          its own it misses both.
        - the words the server reads as code by the dialect's own comment and quote rules
          (_code_words), on the dialects that supply them, with the keyword glued to a
          number included (1INTO, see _reads_as).
        - in a text whose quotes are not followed (_QUOTES_NOT_FOLLOWED), every word, a
          comment's too, with the keyword glued to a number counted as on the code words
          (7.E5INTO, 1INTO): sqlparse reads a backslash before a quote as an escape, so in
          'a\\' INTO t -- ' the INTO is inside one literal to it, while PostgreSQL
          (standard_conforming_strings on) and MySQL under NO_BACKSLASH_ESCAPES end the
          literal at that quote and run the INTO; and with the quotes not followed a comment
          opener inside a literal ('$$/*') would hide the INTO from the word scan, so this
          reading is the only one that sees it and must read what that scan reads.

        An INTO followed by one of INTO_READ_FOLLOWERS is part of a read and passes the
        first two readings.
        """
        followers = self.INTO_READ_FOLLOWERS
        tokens = [
            token
            for statement in statements
            for token in statement.flatten()
            if not (token.ttype in sql_tokens.Whitespace or token.ttype in sql_tokens.Comment)
        ]
        for k, token in enumerate(tokens):
            if (
                token.ttype in sql_tokens.Keyword
                and token.normalized.upper() in self.WRITING_KEYWORDS
                and (k + 1 == len(tokens) or tokens[k + 1].value.upper() not in followers)
            ):
                return True
        words = [word for word, _ in self._code_words(upper)]
        for k, word in enumerate(words):
            if any(_reads_as(word, keyword) for keyword in self.WRITING_KEYWORDS) and (
                k + 1 == len(words) or words[k + 1] not in followers
            ):
                return True
        if _QUOTES_NOT_FOLLOWED.search(upper):
            return any(
                _reads_as(word, keyword)
                for word in _WORD.findall(upper)
                for keyword in self.WRITING_KEYWORDS
            )
        return False

    def _table_word_is_code(self, upper: str) -> bool:
        """Whether the word TABLE is code anywhere in the query.

        TABLE t is shorthand for a SELECT of a whole table. Unlike FROM and JOIN it names
        no table to the schema check, so a table excluded by the privacy settings would be
        read unseen. It is judged on the words the dialect's server reads as code
        (_code_words), so the word in a comment, a literal or a quoted name passes. In a
        text whose quotes are not followed (_QUOTES_NOT_FOLLOWED) every word counts, a
        comment's or a literal's too, as for INTO: a comment opener inside a literal would
        hide the word from the code-word scan. A validator with no _code_words is not
        judged here. An unquoted column called table is refused with it (it is valid only
        after a dot or AS): quote the name.
        """
        if any(word == "TABLE" for word, _ in self._code_words(upper)):
            return True
        return _QUOTES_NOT_FOLLOWED.search(upper) is not None and "TABLE" in _WORD.findall(upper)

    def _code_words(self, text: str) -> Iterator[tuple[str, int]]:
        """The words of *text* the dialect's server reads as code, for the INTO check.

        None on the base. The word scan follows only the quotes in QUOTE_CHARS and reads #
        as code, and dialects differ on both (a backtick name, a # comment), so on a
        dialect with other rules it would refuse a quoted name or a comment that holds
        the word: a validator that does not override this is judged on sqlparse's tokens.
        The PostgreSQL and MySQL validators override this.
        """
        return iter(())

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

    def has_row_limit(self, query: str) -> bool:
        """Whether *query* already carries a row limit, so no LIMIT is appended to it: the
        word LIMIT anywhere (_LIMIT_WORD), or the statement's own FETCH FIRST/NEXT clause
        (_has_fetch_clause). ``validate`` and ``services.row_filter._with_row_limit`` both
        ask this, so the validated form and the executed form get a LIMIT in the same cases.
        """
        return _LIMIT_WORD.search(query) is not None or self._has_fetch_clause(query)

    def _has_fetch_clause(self, query: str) -> bool:
        """Whether the statement carries its own FETCH FIRST/NEXT clause, by the dialect's
        comment rules (has_fetch_clause)."""
        return has_fetch_clause(query, quotes=self.QUOTE_CHARS)

    def _token_error(self, statements: tuple[sql_nodes.Statement, ...]) -> str | None:
        """A dialect's refusal read off sqlparse's tokens (the parse is done once, here)."""
        return None

    def _leading_statement_error(self, stmt: sql_nodes.Statement, query: str) -> str | None:
        """Refuse a statement whose first word after comments is not a read.

        Only SELECT, WITH and an opening parenthesis whose own first word is one of the two
        (a parenthesised query, a set operation of parenthesised queries) may start the
        statement: (TABLE t) and (VALUES …) are refused as TABLE t and VALUES … are. The
        words elsewhere do not count: RESET, LOCK, DISCARD, HANDLER or SHOW with SELECT in a
        trailing comment would otherwise run as written, and so would LLM reasoning prose
        that mentions SELECT (the prompt itself says "what to SELECT"). Judged on sqlparse's
        first token.
        Where sqlparse and the server disagree on a comment, the dialect validators step
        in: the PostgreSQL one refuses a nested block comment, the MySQL one overrides this
        with a scan of the raw text.

        SET gets its own message: it changes the session (statement_timeout, sql_mode, the
        role) for every later query on the pooled connection.
        """
        first = stmt.token_first(skip_cm=True, skip_ws=True)
        if first is not None and first.ttype in sql_tokens.Keyword and first.normalized == "SET":
            return SET_STATEMENT_ERROR
        is_read = first is not None and (
            (first.ttype in sql_tokens.Keyword.DML and first.normalized == "SELECT")
            or first.ttype in sql_tokens.Keyword.CTE
            or (isinstance(first, sql_nodes.Parenthesis) and self._opens_with_a_read(first))
        )
        if is_read:
            return None
        return not_a_read_error(stmt.get_type())

    @staticmethod
    def _opens_with_a_read(group: sql_nodes.Parenthesis) -> bool:
        """Whether the first word inside the (possibly nested) parentheses is SELECT or WITH."""
        for token in group.flatten():
            if token.ttype in sql_tokens.Whitespace or token.ttype in sql_tokens.Comment:
                continue
            if token.value == "(":
                continue
            return (
                token.ttype in sql_tokens.Keyword.DML and token.normalized == "SELECT"
            ) or token.ttype in sql_tokens.Keyword.CTE
        return False
