# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The servers' reading of every shape the hardened validators judge, on the real engines.

Each case backs a unit test in ``tests/test_validators/`` (or ``test_mysql.py``) and names
it: the unit suite assumes how PostgreSQL and MySQL read a comment, a quote, two dashes or
a session variable, and this file is where that assumption is checked against the engine.
Opt-in:
``SAVVINA_LIVE_TEST_DBS=1`` with the ``test-dbs`` compose profile up
(``docker compose --profile test-dbs up -d sample-postgres sample-mysql`` from the repo root:
``sample-postgres`` on 127.0.0.1:5435, ``sample-mysql`` on 127.0.0.1:3307). Skipped otherwise.
The statements go through the drivers directly, so nothing of ours sits between the
statement and the server; the one exception is ``TestMySQLAdapterReadsSqlMode``, which
drives ``MySQLDataSource`` on purpose, because what it checks is the adapter's own reading
of ``@@sql_mode``. Never run this file in parallel with another live run against the same
servers: one case changes the shared MySQL server's GLOBAL ``sql_mode`` (and restores it),
which a concurrent run would read.
"""

from __future__ import annotations

import os

import aiomysql
import asyncpg
import pytest

from app.datasources.adapters.mysql import MySQLDataSource

pytestmark = pytest.mark.skipif(
    os.environ.get("SAVVINA_LIVE_TEST_DBS") != "1",
    reason="set SAVVINA_LIVE_TEST_DBS=1 with the test-dbs compose profile running",
)

_PG = {
    "host": "127.0.0.1",
    "port": 5435,
    "database": "savvina_test",
    "user": "savvina",
    "password": os.environ.get("SAMPLE_POSTGRES_PASSWORD", "savvina_demo"),
}
_MYSQL = {
    "host": "127.0.0.1",
    "port": 3307,
    "db": "sample_delivery",
    "user": "savvina",
    "password": os.environ.get("SAMPLE_MYSQL_PASSWORD", "savvina_demo"),
}
_MYSQL_ROOT = {
    **_MYSQL,
    "user": "root",
    "password": os.environ.get("SAMPLE_MYSQL_ROOT_PASSWORD", "savvina_demo_root"),
}


# ── PostgreSQL ────────────────────────────────────────────────────────────────


@pytest.fixture
async def pg():
    conn = await asyncpg.connect(**_PG)
    try:
        yield conn
    finally:
        await conn.close()


class TestPostgreSQLReads:
    async def test_set_config_reads_back_in_the_same_statement(self, pg) -> None:
        # test_set_config_blocked: a value stored with set_config comes back under another
        # column of the same statement, past any mask on the first.
        row = await pg.fetchrow(
            "SELECT set_config('savvina.leak', 'secret', true) AS a,"
            " current_setting('savvina.leak') AS b"
        )
        assert row["b"] == "secret"

    async def test_block_comments_nest(self, pg) -> None:
        # TestPostgreSQLNestedBlockComments: the first SELECT is inside the outer comment.
        assert await pg.fetchval("/* a /* b */ SELECT 1 */ SELECT 2") == 2
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await pg.fetchval("SELECT 1 /* a /* b */")

    async def test_a_nested_comment_runs_where_sqlparse_shows_none(self, pg) -> None:
        # TestPostgreSQLNestedCommentsOnTheText: the comment nests, so the quote inside it
        # opens no literal and the clause after it runs. sqlparse reads the text after a #
        # as a comment, and [...] or a pair of acute accents (U+00B4) as a name, and shows
        # no comment token inside any of them.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute("SELECT 1 # /* /* */ ' */ 2 AS x INTO TEMP savvina_nest -- '")
            assert await pg.fetchval("SELECT x FROM savvina_nest") == 3
        finally:
            await tr.rollback()
        for sql in (
            "SELECT 1 # /* /* */ ' */ 2 AS x FROM ecommerce.orders FOR SHARE -- '",
            "SELECT (ARRAY[1,2]) [/* /* */ ' */ 1] FROM ecommerce.orders FOR SHARE -- '",
            "SELECT 1 AS \u00b4 /* /* */ ' */ FROM ecommerce.orders FOR SHARE -- ' \u00b4",
        ):
            async with pg.transaction():
                await pg.fetch(sql)
                modes = await pg.fetch(
                    "SELECT mode FROM pg_locks"
                    " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
                )
            assert "RowShareLock" in {r["mode"] for r in modes}, sql

    async def test_dashes_always_open_a_comment(self, pg) -> None:
        # test_mysql_dashes_without_whitespace_do_not_hide_the_clause (its PostgreSQL half):
        # --1 is a comment here, so the statement returns 1 and takes no lock.
        assert await pg.fetchval("SELECT 1 --1 FOR SHARE") == 1

    async def test_a_carriage_return_ends_a_line_comment(self, pg) -> None:
        # TestACarriageReturnEndsALineComment: the FOR SHARE after the \r is code and locks.
        async with pg.transaction():
            await pg.fetch("SELECT * FROM ecommerce.orders -- c\rFOR SHARE")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_for_share_takes_a_row_share_lock(self, pg) -> None:
        # TestLockingReads: FOR SHARE is a locking read; the lock is visible to the session.
        async with pg.transaction():
            await pg.fetch("SELECT * FROM ecommerce.orders FOR SHARE")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_a_literal_naming_a_lock_takes_none(self, pg) -> None:
        # test_columns_and_strings_named_share_or_lock_pass.
        async with pg.transaction():
            await pg.fetch("SELECT id FROM ecommerce.orders WHERE status = 'for share'")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" not in {r["mode"] for r in modes}

    async def test_a_comment_naming_a_lock_takes_none(self, pg) -> None:
        # test_columns_and_strings_named_share_or_lock_pass: the words in a comment.
        async with pg.transaction():
            await pg.fetch("SELECT id FROM ecommerce.orders -- for share")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" not in {r["mode"] for r in modes}

    async def test_a_limit_after_a_trailing_comment_is_a_second_statement(self, pg) -> None:
        # TestLimitAfterATrailingSemicolon: the shape the old validator produced.
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await pg.fetch("SELECT 1; -- c\nLIMIT 1000")
        assert await pg.fetchval("SELECT 1\nLIMIT 1000") == 1

    async def test_a_limit_after_fetch_first_is_a_syntax_error(self, pg) -> None:
        # TestFetchIsARowLimit.
        with pytest.raises(asyncpg.PostgresSyntaxError):
            await pg.fetch("SELECT 1 FETCH FIRST 1 ROWS ONLY LIMIT 1000")
        assert await pg.fetchval("SELECT 1 FETCH FIRST 1 ROWS ONLY") == 1

    async def test_a_limit_after_a_parenthesised_fetch_first(self, pg) -> None:
        # TestFetchIsARowLimit: a LIMIT after a closing parenthesis is valid when the
        # parenthesis closes a subquery, a CTE or one branch of a set operation, and a
        # syntax error when the parentheses are the statement's own body: the whole
        # statement, the query after a WITH clause, or the query before the ORDER BY.
        for sql in (
            "SELECT * FROM (SELECT 1 AS a FETCH FIRST 1 ROWS ONLY) s\nLIMIT 1000",
            "WITH c AS (SELECT 1 AS a FETCH FIRST 1 ROWS ONLY) SELECT * FROM c\nLIMIT 1000",
            "(SELECT 1 AS a FETCH FIRST 1 ROWS ONLY) UNION ALL (SELECT 2)\nLIMIT 1000",
            "(SELECT 1 AS a) UNION ALL (SELECT 2 FETCH FIRST 1 ROWS ONLY)\nLIMIT 1000",
            "SELECT 1 AS a UNION ALL (SELECT 2 FETCH FIRST 1 ROWS ONLY)\nLIMIT 1000",
            "WITH c AS (SELECT 1 AS a) (SELECT a FROM c FETCH FIRST 1 ROWS ONLY)"
            " UNION SELECT 2\nLIMIT 1000",
        ):
            assert len(await pg.fetch(sql)) >= 1
        for sql in (
            "(SELECT 1 AS a FETCH FIRST 1 ROWS ONLY)\nLIMIT 1000",
            "((SELECT 1 AS a FETCH FIRST 1 ROWS ONLY))\nLIMIT 1000",
            "WITH c AS (SELECT 1 AS a) (SELECT a FROM c FETCH FIRST 1 ROWS ONLY)\nLIMIT 1000",
            "(SELECT 1 AS a FETCH FIRST 1 ROWS ONLY) ORDER BY a\nLIMIT 1000",
            "(SELECT 1 AS a) UNION ALL (SELECT 2) FETCH FIRST 1 ROWS ONLY\nLIMIT 1000",
        ):
            with pytest.raises(asyncpg.PostgresSyntaxError):
                await pg.fetch(sql)
            # Without the LIMIT each one runs: the clause is the statement's own.
            assert len(await pg.fetch(sql.removesuffix("\nLIMIT 1000"))) == 1

    async def test_a_backslash_does_not_escape_the_quote(self, pg) -> None:
        # TestABackslashDoesNotHideInto: with standard_conforming_strings on, 'a\' is a whole
        # literal, so the INTO after it runs, and a '/*' literal opens no comment.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute(r"SELECT 'a\' AS x, '/*' AS y INTO TEMP savvina_into -- */'")
            row = await pg.fetchrow("SELECT x, y FROM savvina_into")
        finally:
            await tr.rollback()
        assert (row["x"], row["y"]) == ("a\\", "/*")

    async def test_a_comment_opener_literal_does_not_hide_a_share_lock(self, pg) -> None:
        # TestABackslashDoesNotHideAShareLock: 'a\' is a whole literal and '/*' opens no
        # comment, so the FOR SHARE after them runs and locks.
        async with pg.transaction():
            await pg.fetch(r"SELECT 'a\' AS x, '/*' AS y FROM ecommerce.orders FOR SHARE -- */'")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_a_dollar_quoted_comment_opener_hides_nothing(self, pg) -> None:
        # TestADollarQuoteKeepsTheFallback: $$/*$$ is a whole literal, so the INTO and the
        # FOR SHARE after it run.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute("SELECT $$/*$$ AS y INTO TEMP savvina_dollar -- */")
            assert await pg.fetchval("SELECT y FROM savvina_dollar") == "/*"
        finally:
            await tr.rollback()
        async with pg.transaction():
            await pg.fetch("SELECT $$/*$$ AS y FROM ecommerce.orders FOR SHARE -- */")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_a_dollar_quote_tag_takes_any_non_ascii_character(self, pg) -> None:
        # TestADollarQuoteKeepsTheFallback: the tag of a dollar-quoted string may hold any
        # non-ASCII character, so the quote inside is part of the string, and the INTO and
        # the FOR SHARE after it run.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute("SELECT $\u20ac$ ' $\u20ac$ AS s INTO TEMP savvina_tag -- '")
            assert await pg.fetchval("SELECT s FROM savvina_tag") == " ' "
        finally:
            await tr.rollback()
        async with pg.transaction():
            await pg.fetch(
                "SELECT * FROM ecommerce.orders WHERE $\u20ac$ ' $\u20ac$ <> 'x' FOR SHARE -- '"
            )
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_a_dollar_literal_does_not_hide_a_spaced_share_lock(self, pg) -> None:
        # TestTheFallbackReadsWhatTheCodeWordScanReads: '$$--' is a whole literal, so the
        # comment between FOR and SHARE is a gap and the clause locks.
        async with pg.transaction():
            await pg.fetch("SELECT id FROM ecommerce.orders WHERE '$$--' <> '' FOR /* x */\nSHARE")
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_a_line_comment_opener_in_a_literal_hides_no_nested_comment(self, pg) -> None:
        # TestPostgreSQLNestedCommentsOnTheText::test_a_line_comment_opener_in_a_literal_
        # hides_no_nested_comment: '--a\' is a whole literal, so the nested comment after it
        # is one comment and the FOR SHARE around it locks.
        async with pg.transaction():
            await pg.fetch(
                r"SELECT id FROM ecommerce.orders WHERE '--a\' <> '' FOR /* /* */ x */ SHARE -- '"
            )
            modes = await pg.fetch(
                "SELECT mode FROM pg_locks"
                " WHERE relation = 'ecommerce.orders'::regclass AND pid = pg_backend_pid()"
            )
        assert "RowShareLock" in {r["mode"] for r in modes}

    async def test_fetch_as_part_of_a_name_bounds_nothing(self, pg) -> None:
        # TestFetchIsARowLimit::test_fetch_as_part_of_a_name_is_no_row_limit: FETCH after a
        # dot, or glued to a non-ASCII character, is a name, so each returns every order,
        # and with the LIMIT the validator appends it runs and is bounded.
        total = await pg.fetchval("SELECT count(*) FROM ecommerce.orders")
        assert total > 5
        for sql in (
            'SELECT o.id, s.fetch FIRST FROM (SELECT 1 AS "fetch") s, ecommerce.orders o',
            'SELECT o.id, s . fetch FIRST FROM (SELECT 1 AS "fetch") s, ecommerce.orders o',
            'SELECT o.id, s./* c */fetch FIRST FROM (SELECT 1 AS "fetch") s, ecommerce.orders o',
            "SELECT o.id FETCH\u00a0FIRST FROM ecommerce.orders o",
            "SELECT o.id, x\u00a0FETCH FIRST FROM (SELECT 1 AS x\u00a0FETCH) s, ecommerce.orders o",
            "SELECT o.id, \u20acFETCH FIRST FROM (SELECT 1 AS \u20acFETCH) s, ecommerce.orders o",
            "SELECT a$fetch first FROM (SELECT id AS a$fetch FROM ecommerce.orders) d",
            "SELECT fetch$a first FROM (SELECT id AS fetch$a FROM ecommerce.orders) d",
        ):
            assert len(await pg.fetch(sql)) == total, sql
            assert len(await pg.fetch(f"{sql}\nLIMIT 5")) == 5, sql

    async def test_a_hash_is_an_operator_and_the_into_after_it_runs(self, pg) -> None:
        # TestIntoOnCodeWords: # is XOR here (sqlparse reads a comment), so the INTO after
        # it creates the table.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute("SELECT 1 # 2 AS x INTO TEMP savvina_hash")
            assert await pg.fetchval("SELECT x FROM savvina_hash") == 3
        finally:
            await tr.rollback()

    async def test_an_acute_accent_is_a_name_and_the_into_after_it_runs(self, pg) -> None:
        # TestIntoOnCodeWords: sqlparse reads a pair of acute accents (U+00B4) as the
        # quotes of a name; here the first is an alias and the INTO after it creates the table.
        tr = pg.transaction()
        await tr.start()
        try:
            await pg.execute("SELECT 1 AS \u00b4 INTO TEMP savvina_acute -- \u00b4")
            assert await pg.fetchval("SELECT count(*) FROM savvina_acute") == 1
        finally:
            await tr.rollback()

    async def test_a_set_changes_the_session(self, pg) -> None:
        # TestBaseSQLValidatorLeadingSet: a SET outlives its statement on the connection.
        await pg.execute("SET statement_timeout = 12345")
        assert await pg.fetchval("SHOW statement_timeout") == "12345ms"


# ── MySQL ─────────────────────────────────────────────────────────────────────


@pytest.fixture
async def my():
    conn = await aiomysql.connect(**_MYSQL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
async def my_root():
    conn = await aiomysql.connect(**_MYSQL_ROOT, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


async def _one(conn, sql: str):
    async with conn.cursor() as cur:
        await cur.execute(sql)
        row = await cur.fetchone()
    return row[0] if row else None


async def _row_count(conn, sql: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql)
        return len(await cur.fetchall())


async def _record_lock_modes(conn, sql: str) -> set[str]:
    """The record-lock modes *sql* holds on ``orders`` while its transaction is open."""
    async with conn.cursor() as cur:
        await cur.execute("START TRANSACTION")
        await cur.execute(sql)
        await cur.fetchall()
        await cur.execute(
            "SELECT lock_mode FROM performance_schema.data_locks"
            " WHERE object_schema = 'sample_delivery' AND object_name = 'orders'"
            " AND lock_type = 'RECORD'"
        )
        modes = {row[0] for row in await cur.fetchall()}
        await cur.execute("ROLLBACK")
    return modes


class TestMySQLReads:
    async def test_a_user_variable_assignment_outlives_the_statement(self, my) -> None:
        # test_blocks_variable_assignment (test_mysql.py): the value sits on the pooled
        # connection for a later SELECT @v.
        await _one(my, "SELECT @savvina_v := 42")
        assert await _one(my, "SELECT @savvina_v") == 42

    async def test_an_executable_comment_runs_its_into(self, my) -> None:
        # TestMySQLExecutableCommentInto.
        await _one(my, "SELECT 7 /*! INTO @savvina_x */")
        assert await _one(my, "SELECT @savvina_x") == 7

    async def test_a_versioned_comment_above_the_server_version_is_skipped(self, my) -> None:
        # TestMySQLExecutableCommentTheServerMaySkip: /*!99999 runs only on a server of at
        # least that version, so the SELECT inside it is skipped and the one after it runs.
        assert await _one(my, "/*!99999 SELECT 1 */ SELECT 2") == 2

    async def test_a_mariadb_only_comment_is_skipped(self, my) -> None:
        # TestMySQLExecutableCommentTheServerMaySkip: MySQL skips a /*M! comment entirely.
        assert await _one(my, "/*M! SELECT 1 */ SELECT 2") == 2

    async def test_a_quote_in_a_run_comment_hides_its_closer(self, my) -> None:
        # TestMySQLExecutableCommentTheServerMaySkip::
        # test_a_leading_comment_that_is_not_empty_is_refused: the quote inside the comment
        # the server runs opens a literal that swallows the comment's own closing */.
        assert await _one(my, "SELECT /*! '*/' */ AS v") == "*/"

    async def test_a_mixed_pair_runs_one_comment_and_skips_the_other(self, my) -> None:
        # TestMySQLExecutableCommentTheServerMaySkip::
        # test_a_mixture_of_run_and_skipped_comments_is_refused: the server skips the gated
        # (or MariaDB-only) comment and runs the plain one, so it leads with the second
        # comment's content, which neither the content-as-code nor the content-as-gap
        # reading of the pair shows first.
        assert await _one(my, "/*!99999 SELECT 1 */ /*!50000 SELECT 2 */") == 2
        assert await _one(my, "/*M! SELECT 1 */ /*! SELECT 3 */") == 3

    async def test_a_skipped_comment_does_not_hide_the_one_after_it(self, my) -> None:
        # TestMySQLGatedCommentsAreRefused: the server skips the gated (or MariaDB-only)
        # comment, quote and all, and runs the INTO in the plain comment after it. A scan
        # that reads both as code sees the quote open a literal that swallows the INTO.
        await _one(my, "SET @savvina_g = NULL")
        await _one(my, "SELECT 7 FROM DUAL WHERE 1 /*!99999 ' */ /*! INTO @savvina_g */ -- '")
        assert await _one(my, "SELECT @savvina_g") == 7
        await _one(my, "SELECT 8 FROM DUAL WHERE 1 /*M! ' */ /*! INTO @savvina_g */ -- '")
        assert await _one(my, "SELECT @savvina_g") == 8

    async def test_a_hash_ends_the_word_before_it(self, my) -> None:
        # TestMySQLHashCommentInto: INTO#x then @y on the next line sets @y.
        await _one(my, "SELECT 8 INTO#x\n@savvina_y")
        assert await _one(my, "SELECT @savvina_y") == 8

    async def test_into_after_a_dot_is_the_clause(self, my) -> None:
        # TestIntoOnCodeWords: 7. is a number and the INTO after it is the clause (sqlparse
        # reads a name after a dot).
        await _one(my, "SET @savvina_n = NULL")
        await _one(my, "SELECT 7.INTO @savvina_n")
        assert await _one(my, "SELECT @savvina_n") == 7

    async def test_into_behind_an_exponent_is_the_clause(self, my) -> None:
        # TestIntoOnCodeWords: 1E5 and 7.E5 are numbers and the INTO glued to them is the
        # clause. Glued to a plain integer it is part of a name: a syntax error here.
        await _one(my, "SET @savvina_e = NULL")
        await _one(my, "SELECT 1E5INTO @savvina_e")
        assert await _one(my, "SELECT @savvina_e") == 100000
        await _one(my, "SELECT 7.E5INTO @savvina_e")
        assert await _one(my, "SELECT @savvina_e") == 700000
        with pytest.raises(aiomysql.ProgrammingError):
            await _one(my, "SELECT 1INTO @savvina_e")

    async def test_an_acute_accent_is_a_name_and_the_into_after_it_runs(self, my) -> None:
        # TestIntoOnCodeWords: sqlparse reads a pair of acute accents (U+00B4) as the
        # quotes of a name; here the first is an alias and the INTO after it is the clause.
        await _one(my, "SET @savvina_a = NULL")
        await _one(my, "SELECT 7 AS \u00b4 INTO @savvina_a -- \u00b4")
        assert await _one(my, "SELECT @savvina_a") == 7

    async def test_dashes_without_a_space_are_arithmetic(self, my) -> None:
        # TestMySQLDashesWithoutWhitespace: 1 --1 is 1 - (-1).
        assert await _one(my, "SELECT 1 --1") == 2

    async def test_dashes_with_a_space_open_a_comment(self, my) -> None:
        assert await _one(my, "SELECT 1 -- 1") == 1

    async def test_dashes_before_a_non_ascii_space_are_arithmetic(self, my) -> None:
        # TestMySQLDashesWithoutWhitespace::test_into_after_dashes_and_a_non_ascii_space_
        # is_refused: a no-break or an ideographic space is a name character here, not a
        # space, so the dashes are two minus signs (1 - -2, 1 - -5) and the INTO runs.
        await _one(my, "SET @savvina_s = NULL")
        await _one(my, "SELECT 1 --\u00a0 INTO @savvina_s FROM (SELECT 2 AS `\u00a0`) d")
        assert await _one(my, "SELECT @savvina_s") == 3
        await _one(my, "SELECT 1 --\u3000 INTO @savvina_s FROM (SELECT 5 AS `\u3000`) d")
        assert await _one(my, "SELECT @savvina_s") == 6

    async def test_dashes_before_a_delete_character_open_a_comment(self, my) -> None:
        # TestMySQLDashesWithoutWhitespace::test_dashes_before_a_delete_character_open_a_
        # comment: DEL is a control character here, so --DEL opens a comment (not 1 - -1)
        # and the FETCH clause inside it bounds nothing.
        assert await _one(my, "SELECT 1 --\x7f1") == 1
        total = await _one(my, "SELECT count(*) FROM orders")
        assert total > 5
        sql = "SELECT id FROM orders --\x7f FETCH FIRST 1 ROWS ONLY"
        assert await _row_count(my, sql) == total
        assert await _row_count(my, "SELECT id FROM orders\nLIMIT 5") == 5

    async def test_fetch_as_part_of_a_name_bounds_nothing(self, my) -> None:
        # TestFetchIsARowLimit::test_fetch_as_part_of_a_name_is_no_row_limit: FETCH after a
        # dot, or glued to a non-ASCII character, is a name, so each returns every order,
        # and with the LIMIT the validator appends it runs and is bounded.
        total = await _one(my, "SELECT count(*) FROM orders")
        assert total > 5
        for sql in (
            "SELECT o.id, s.fetch FIRST FROM (SELECT 1 AS `fetch`) s, orders o",
            "SELECT o.id FETCH\u00a0FIRST FROM orders o",
            "SELECT o.id, x\u00a0FETCH FIRST FROM (SELECT 1 AS x\u00a0FETCH) s, orders o",
            "SELECT o.id, \u20acFETCH FIRST FROM (SELECT 1 AS \u20acFETCH) s, orders o",
            "SELECT a$fetch first FROM (SELECT id AS a$fetch FROM orders) d",
            "SELECT fetch$a first FROM (SELECT id AS fetch$a FROM orders) d",
            "SELECT @fetch first FROM orders",
        ):
            assert await _row_count(my, sql) == total, sql
            assert await _row_count(my, f"{sql}\nLIMIT 5") == 5, sql

    async def test_into_behind_an_exponent_after_a_dollar_literal_runs(self, my) -> None:
        # TestTheFallbackReadsWhatTheCodeWordScanReads: '$$/* limit' is a whole literal, so
        # the INTO glued to the exponent after it is the clause.
        await _one(my, "SET @savvina_p = NULL")
        await _one(my, "SELECT 3 FROM DUAL WHERE '$$/* limit' <> '' AND 1 <> 7.E5INTO @savvina_p")
        assert await _one(my, "SELECT @savvina_p") == 3

    async def test_a_carriage_return_does_not_end_a_line_comment(self, my) -> None:
        # TestACarriageReturnEndsALineComment (its MySQL half): the + 1 after the \r is
        # still inside the comment.
        assert await _one(my, "SELECT 1 -- c\r+ 1") == 1
        assert await _one(my, "SELECT 1 # c\r+ 1") == 1

    async def test_dashes_before_an_executable_comment_are_arithmetic(self, my) -> None:
        # TestMySQLDashesBeforeAnExecutableComment: no whitespace follows the dashes, so
        # they are two minus signs and the comment's content runs: 7 - -0 INTO the variable.
        await _one(my, "SET @savvina_d = NULL")
        await _one(my, "SELECT 7 --/*! 0 INTO @savvina_d*/")
        assert await _one(my, "SELECT @savvina_d") == 7

    async def test_a_literal_naming_into_is_only_a_literal(self, my) -> None:
        # test_into_in_a_literal_comment_or_name_is_allowed.
        assert await _one(my, "SELECT 'walked into room #3'") == "walked into room #3"

    async def test_lock_in_share_mode_takes_a_shared_lock(self, my_root) -> None:
        # TestLockingReads (MySQL): the lock is listed in performance_schema while the
        # transaction is open. Root, because performance_schema needs a grant.
        modes = await _record_lock_modes(my_root, "SELECT * FROM orders LOCK IN SHARE MODE")
        assert any(mode.startswith("S") for mode in modes), modes

    async def test_dashes_without_a_space_do_not_hide_a_share_lock(self, my_root) -> None:
        # test_mysql_dashes_without_whitespace_do_not_hide_the_clause: --1 is arithmetic
        # (1 - -1, true for every row), so the FOR SHARE after it is code and locks.
        modes = await _record_lock_modes(my_root, "SELECT * FROM orders WHERE 1 --1 FOR SHARE")
        assert any(mode.startswith("S") for mode in modes), modes

    async def test_a_share_lock_behind_an_exponent_locks(self, my_root) -> None:
        # test_a_share_lock_glued_to_a_number_is_refused: 1E0 and 1.E0 are numbers, so the
        # FOR SHARE glued to them is code and locks. Glued to a plain integer (1FOR) it is
        # part of a name: a syntax error here.
        for sql in (
            "SELECT * FROM orders WHERE 1 = 1E0FOR SHARE",
            "SELECT * FROM orders WHERE 1 = 1.E0FOR SHARE",
        ):
            modes = await _record_lock_modes(my_root, sql)
            assert any(mode.startswith("S") for mode in modes), (sql, modes)
        with pytest.raises(aiomysql.ProgrammingError):
            await _one(my_root, "SELECT * FROM orders WHERE 1 = 1FOR SHARE")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders WHERE '$$/* limit' <> '' FOR /* x */ SHARE",
            "SELECT id FROM orders WHERE '$$/* limit' <> '' LOCK /* x */ IN SHARE MODE",
            "SELECT id FROM orders WHERE '$$/* limit' <> '' AND id = 1.E0FOR SHARE",
        ],
    )
    async def test_a_dollar_literal_does_not_hide_a_share_lock(self, my_root, sql) -> None:
        # TestTheFallbackReadsWhatTheCodeWordScanReads: '$$/* limit' is a whole literal, so
        # the clause after it, spaced by a comment or glued to an exponent, locks.
        modes = await _record_lock_modes(my_root, sql)
        assert any(mode.startswith("S") for mode in modes), modes

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders -- lock in share mode",
            "SELECT *, 'Up for share' AS note FROM orders",
        ],
    )
    async def test_a_comment_or_a_literal_naming_a_lock_takes_none(self, my_root, sql) -> None:
        # test_columns_and_strings_named_share_or_lock_pass.
        assert await _record_lock_modes(my_root, sql) == set()

    async def test_a_set_changes_the_session_mode(self, my) -> None:
        # TestBaseSQLValidatorLeadingSet: a SET on a pooled connection changes how every
        # later statement reads a double quote.
        async with my.cursor() as cur:
            await cur.execute("SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',ANSI_QUOTES')")
        assert "ANSI_QUOTES" in str(await _one(my, "SELECT @@SESSION.sql_mode"))
        # Under ANSI_QUOTES a double-quoted token is a name: this now fails as an unknown
        # column instead of returning the string.
        with pytest.raises(Exception, match="Unknown column"):
            await _one(my, 'SELECT "not_a_column"')


class TestMySQLAdapterReadsSqlMode:
    async def test_default_mode_reads_double_quotes_as_literals(self) -> None:
        adapter = MySQLDataSource()
        result = await adapter.connect(
            {
                "host": _MYSQL["host"],
                "port": _MYSQL["port"],
                "database": _MYSQL["db"],
                "username": _MYSQL["user"],
                "password": _MYSQL["password"],
            }
        )
        try:
            assert result.success, result.message
            assert adapter.double_quote_is_string_literal is True
        finally:
            await adapter.disconnect()

    async def test_ansi_quotes_global_mode_flips_the_flag(self, my_root) -> None:
        # The adapter reads the session mode of a fresh pool connection, which inherits the
        # global mode: set ANSI_QUOTES globally, connect, and restore.
        original = str(await _one(my_root, "SELECT @@GLOBAL.sql_mode"))
        async with my_root.cursor() as cur:
            await cur.execute("SET GLOBAL sql_mode = CONCAT(@@GLOBAL.sql_mode, ',ANSI_QUOTES')")
        adapter = MySQLDataSource()
        try:
            result = await adapter.connect(
                {
                    "host": _MYSQL["host"],
                    "port": _MYSQL["port"],
                    "database": _MYSQL["db"],
                    "username": _MYSQL["user"],
                    "password": _MYSQL["password"],
                }
            )
            assert result.success, result.message
            assert adapter.double_quote_is_string_literal is False
        finally:
            # The server is shared: restore the global mode first, and disconnect even if
            # the restore raises.
            try:
                async with my_root.cursor() as cur:
                    await cur.execute("SET GLOBAL sql_mode = %s", (original,))
            finally:
                await adapter.disconnect()
