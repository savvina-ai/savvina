# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``services/sensitive_guard``: strict sensitive-column mode rejects a query that uses a
sensitive column anywhere but as a plain item of its SELECT list, or a shape the check
cannot follow."""

from __future__ import annotations

import pytest

from app.datasources.models import PrivacySettings
from app.services.row_filter_errors import QueryRejectedError, SensitiveColumnRejectedError
from app.services.sensitive_guard import check_strict_sensitive


def _privacy(**kwargs) -> PrivacySettings:
    return PrivacySettings(
        strict_sensitive_columns=True,
        sensitive_column_patterns=["email", "salary"],
        **kwargs,
    )


def _rejected(sql: str, privacy: PrivacySettings | None = None, dialect: str = "postgresql"):
    with pytest.raises(SensitiveColumnRejectedError) as info:
        check_strict_sensitive(sql, privacy or _privacy(), dialect)
    exc = info.value
    assert isinstance(exc, QueryRejectedError)
    assert str(exc).startswith("Strict sensitive columns: ")
    assert exc.llm_hint
    return exc


def _column_message(exc: SensitiveColumnRejectedError, column: str) -> None:
    message = str(exc)
    assert f"the column {column} outside its SELECT list" in message
    assert column in exc.llm_hint
    stripped = message.replace(column, "")
    assert "'" not in stripped and '"' not in stripped


class TestOff:
    def test_off_runs_nothing(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=False, sensitive_column_patterns=["email"]
        )
        check_strict_sensitive("SELECT ) FROM u WHERE email = 'x'", privacy, "postgresql")

    def test_none_privacy_runs_nothing(self) -> None:
        check_strict_sensitive("SELECT ) FROM u WHERE email = 'x'", None, "postgresql")


class TestAllowed:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT email FROM u",
            "SELECT u.email AS e FROM u",
            "SELECT email, id FROM u WHERE id = 1",
            "SELECT DISTINCT email FROM u",
            "SELECT * FROM u",
            "SELECT u.* FROM u JOIN o ON o.uid = u.id",
            "SELECT count(*) FROM u",
            "SELECT count(*) OVER () FROM u",
            "SELECT id FROM u WHERE name = 'x'",  # name is not sensitive here
            "SELECT 'email' AS literal FROM u",  # a string, not a column
            'SELECT "Email" FROM u',
            "SELECT email FROM (SELECT email FROM u) AS s",
            "WITH c AS (SELECT email FROM u) SELECT email FROM c",
            "SELECT id FROM u ORDER BY 1",  # position 1 is id
            "SELECT id, email FROM u ORDER BY 1",
            "SELECT id FROM u UNION SELECT id FROM v ORDER BY 1",
            "SELECT email, count(*) FROM u GROUP BY 2",  # position 2 is count(*)
            "SELECT * FROM u JOIN v USING (id)",
            "SELECT email FROM u LIMIT 10 OFFSET 2",
            "SELECT 1 FROM u WHERE EXISTS (SELECT email FROM v)",
            "SELECT x FROM u, LATERAL (SELECT email FROM v WHERE v.id = u.id) l",
        ],
    )
    def test_plain_projection_items_pass(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")

    def test_mysql_double_quotes_are_strings(self) -> None:
        check_strict_sensitive('SELECT id FROM u WHERE name = "email"', _privacy(), "mysql")


class TestRejectedUses:
    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            ("SELECT id FROM u WHERE email = 'x'", "email"),
            ("SELECT id FROM u JOIN o ON o.email = u.email", "email"),
            ("SELECT count(*) FROM u GROUP BY email", "email"),
            ("SELECT id FROM u GROUP BY id HAVING count(email) > 1", "email"),
            ("SELECT id FROM u ORDER BY salary", "salary"),
            ("SELECT lower(email) FROM u", "email"),
            ("SELECT CASE WHEN email = 'x' THEN 1 END FROM u", "email"),
            ("SELECT count(email) FROM u", "email"),
            ("SELECT salary + 1 AS s FROM u", "salary"),
            ("SELECT id FROM u WHERE id IN (SELECT id FROM v WHERE email = 'x')", "email"),
            ("WITH c AS (SELECT id FROM u WHERE email = 'x') SELECT id FROM c", "email"),
            ("SELECT id FROM u UNION SELECT id FROM v WHERE salary > 1", "salary"),
            ("SELECT id FROM u WHERE salary > 1 LIMIT 5", "salary"),
            ("SELECT id FROM u WHERE (email) IS NOT NULL", "email"),
            ("SELECT * FROM u JOIN v USING (id) WHERE v.email IS NOT NULL", "email"),
            ("SELECT id FROM u u2 WHERE u2.email = 'x'", "email"),
        ],
    )
    def test_any_other_use_is_rejected_naming_the_column(self, sql: str, column: str) -> None:
        _column_message(_rejected(sql), column)

    def test_an_explicit_sensitive_column_counts(self) -> None:
        privacy = _privacy(sensitive_columns=["name"])
        _column_message(_rejected("SELECT id FROM u WHERE name = 'x'", privacy), "name")

    def test_an_excluded_columns_bare_name_counts(self) -> None:
        privacy = _privacy(excluded_columns=["public.u.ssn"])
        _column_message(_rejected("SELECT id FROM u ORDER BY ssn", privacy), "ssn")

    def test_a_quoted_spelling_is_checked_unquoted(self) -> None:
        _column_message(_rejected("SELECT id FROM u WHERE \"Email\" = 'x'"), "Email")
        _column_message(_rejected("SELECT id FROM u WHERE `email` = 'x'", dialect="mysql"), "email")


class TestAliasesAndPositions:
    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            ("WITH c AS (SELECT id, email AS e FROM u) SELECT id FROM c WHERE e = 'x'", "e"),
            ("SELECT id FROM (SELECT id, email AS e FROM u) s WHERE e LIKE 'a%'", "e"),
            ("SELECT id, email AS e FROM u ORDER BY e", "e"),
            (
                "WITH c AS (SELECT email AS e FROM u), d AS (SELECT e AS f FROM c)"
                " SELECT id FROM d WHERE f = 'x'",
                "f",
            ),
            ("SELECT email FROM u ORDER BY 1", "email"),
            ("SELECT email, count(*) FROM u GROUP BY 1", "email"),
            ("SELECT id, email AS e FROM u ORDER BY 2", "email"),
        ],
    )
    def test_an_alias_or_a_position_that_reaches_the_column_is_rejected(
        self, sql: str, column: str
    ) -> None:
        _column_message(_rejected(sql), column)

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH c(e) AS (SELECT email FROM u) SELECT id FROM c WHERE e = 'x'",
            "SELECT id FROM (SELECT email FROM u) AS s(e) WHERE e = 'x'",
        ],
    )
    def test_a_column_list_on_a_table_alias_is_rejected(self, sql: str) -> None:
        assert "a column list on a table alias" in str(_rejected(sql))

    def test_a_position_that_refers_to_a_star_is_rejected(self) -> None:
        assert "a position that refers to a star" in str(_rejected("SELECT * FROM u ORDER BY 1"))


class TestJoins:
    def test_using_a_sensitive_column_is_rejected(self) -> None:
        sql = "SELECT u.id FROM u JOIN (SELECT 'x@y.com' AS email) v USING (email)"
        _column_message(_rejected(sql), "email")

    def test_a_natural_join_is_rejected(self) -> None:
        assert "a natural join" in str(_rejected("SELECT u.id FROM u NATURAL JOIN v"))


class TestWholeRow:
    @pytest.mark.parametrize(
        ("sql", "table"),
        [
            ("SELECT id FROM u WHERE u::text LIKE '%x@y%'", "u"),
            ("SELECT id FROM u WHERE to_jsonb(u)::text LIKE '%x@y%'", "u"),
            ("SELECT id FROM u WHERE (u).email = 'x'", "u"),
            ("SELECT u FROM u", "u"),
            ("SELECT row_to_json(u) FROM u", "u"),
            ("SELECT id FROM u AS t WHERE t::text LIKE '%x%'", "t"),
            ("SELECT s FROM (SELECT email FROM u) s", "s"),
        ],
    )
    def test_a_whole_row_reference_is_rejected(self, sql: str, table: str) -> None:
        assert f"the whole row of {table}" in str(_rejected(sql))

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT row_to_json(u.*) FROM u",
            "SELECT id FROM u WHERE ROW(u.*)::text LIKE '%x%'",
        ],
    )
    def test_a_star_outside_the_select_list_is_rejected(self, sql: str) -> None:
        assert "a star outside the SELECT list" in str(_rejected(sql))


class TestUnparsed:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT email FROM u WHERE )",
            "SELECT ) FROM u",  # names no sensitive column: strict mode parses every query
            "SELECT email FROM u; SELECT 1",
            "EXPLAIN SELECT email FROM u",
        ],
    )
    def test_anything_but_one_plain_statement_is_rejected(self, sql: str) -> None:
        assert "could not be parsed" in str(_rejected(sql))
