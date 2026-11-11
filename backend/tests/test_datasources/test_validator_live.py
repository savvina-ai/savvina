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

    async def test_dashes_always_open_a_comment(self, pg) -> None:
        # test_mysql_dashes_without_whitespace_do_not_hide_the_clause (its PostgreSQL half):
        # --1 is a comment here, so the statement returns 1 and takes no lock.
        assert await pg.fetchval("SELECT 1 --1 FOR SHARE") == 1

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

    async def test_a_hash_ends_the_word_before_it(self, my) -> None:
        # TestMySQLHashCommentInto: INTO#x then @y on the next line sets @y.
        await _one(my, "SELECT 8 INTO#x\n@savvina_y")
        assert await _one(my, "SELECT @savvina_y") == 8

    async def test_dashes_without_a_space_are_arithmetic(self, my) -> None:
        # TestMySQLDashesWithoutWhitespace: 1 --1 is 1 - (-1).
        assert await _one(my, "SELECT 1 --1") == 2

    async def test_dashes_with_a_space_open_a_comment(self, my) -> None:
        assert await _one(my, "SELECT 1 -- 1") == 1

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
