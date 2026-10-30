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

    def test_the_parsed_tree_is_returned_when_on(self) -> None:
        from sqlglot import exp

        tree = check_strict_sensitive("SELECT id FROM u", _privacy(), "postgresql")
        assert isinstance(tree, exp.Select)
        assert check_strict_sensitive("SELECT ) FROM u", None, "postgresql") is None


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

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM u ORDER BY 1",
            "SELECT * FROM u ORDER BY 2",
            "SELECT id, * FROM u ORDER BY 3",
            "SELECT * FROM u GROUP BY 2",
            "SELECT u.* FROM u ORDER BY 2",
            "SELECT * FROM u ORDER BY 99",
            "SELECT * FROM a UNION ALL SELECT x FROM b ORDER BY 1",
        ],
    )
    def test_a_position_at_or_past_a_star_is_rejected(self, sql: str) -> None:
        # The star expands to an unknown number of columns, so the position can land on a
        # sensitive one inside it.
        assert "a position that refers to a star" in str(_rejected(sql))

    def test_a_position_before_a_star_is_resolved(self) -> None:
        check_strict_sensitive("SELECT id, * FROM u ORDER BY 1", _privacy(), "postgresql")
        check_strict_sensitive("SELECT id AS x, * FROM u ORDER BY 1", _privacy(), "postgresql")
        _column_message(_rejected("SELECT email, * FROM u ORDER BY 1"), "email")
        _column_message(_rejected("SELECT email, * FROM u ORDER BY 1", dialect="mysql"), "email")

    def test_count_star_before_a_position_is_not_a_star(self) -> None:
        check_strict_sensitive("SELECT count(*), id FROM u GROUP BY 2", _privacy(), "postgresql")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT email FROM u LIMIT 1) FROM t ORDER BY 1",
            "SELECT (SELECT email FROM u LIMIT 1) AS e FROM t ORDER BY 1",
            "SELECT (SELECT email FROM u LIMIT 1) AS e, count(*) FROM t GROUP BY 1",
        ],
    )
    def test_a_position_on_a_scalar_subquery_item_is_rejected(self, sql: str) -> None:
        _column_message(_rejected(sql), "email")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT * FROM b LIMIT 1) AS v FROM t ORDER BY 1",
            "SELECT (SELECT * FROM b LIMIT 1) AS v FROM t GROUP BY 1",
        ],
    )
    def test_a_position_on_a_scalar_subquery_star_is_a_shape_rejection(self, sql: str) -> None:
        exc = _rejected(sql)
        assert "a position that refers to a star" in str(exc)
        assert "the column *" not in str(exc)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM u ORDER BY 0",
            "SELECT id, email FROM u ORDER BY 0",
        ],
    )
    def test_position_zero_is_left_to_the_engine(self, sql: str) -> None:
        # The engine refuses ORDER BY 0; it must not wrap around to the last item.
        check_strict_sensitive(sql, _privacy(), "postgresql")

    @pytest.mark.parametrize(
        "sql",
        [
            "(SELECT id FROM a UNION ALL SELECT email FROM b) ORDER BY 1 LIMIT 1",
            "(SELECT id, email FROM users) ORDER BY 2 LIMIT 1",
            "SELECT id FROM ((SELECT id, email FROM users) ORDER BY 2 LIMIT 1) t",
            "SELECT t.id FROM ((SELECT id, name FROM a UNION ALL SELECT id, email FROM b)"
            " ORDER BY 2 LIMIT 1) t",
        ],
    )
    def test_a_position_on_a_parenthesised_query_is_resolved_against_it(self, sql: str) -> None:
        # sqlglot hangs the ORDER BY on the Subquery wrapper, not on the query inside it.
        _column_message(_rejected(sql), "email")

    @pytest.mark.parametrize(
        "sql",
        [
            "(SELECT id, name FROM users) ORDER BY 2",
            "SELECT id FROM a UNION ALL (SELECT name FROM b ORDER BY 1)",
        ],
    )
    def test_a_position_on_a_parenthesised_query_without_a_sensitive_item_passes(
        self, sql: str
    ) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")

    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            ("SELECT DISTINCT ON (1) email FROM u", "email"),
            # The position refers to the first branch's own id, which the later branch
            # launders.
            ("SELECT DISTINCT ON (1) id FROM a UNION ALL SELECT email FROM b", "id"),
        ],
    )
    def test_a_position_in_distinct_on_is_resolved(self, sql: str, column: str) -> None:
        _column_message(_rejected(sql), column)

    def test_a_position_in_distinct_on_on_a_plain_item_passes(self) -> None:
        check_strict_sensitive("SELECT DISTINCT ON (1) id, email FROM u", _privacy(), "postgresql")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT *, row_number() OVER (ORDER BY 1) FROM u",
            "SELECT email, row_number() OVER (ORDER BY 1) FROM u",
        ],
    )
    def test_a_number_in_a_window_order_by_is_a_constant(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")


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

    def test_a_table_named_column_gets_a_hint_to_qualify_it(self) -> None:
        # Without the schema the check cannot tell the column country from the row country;
        # the hint turns the refusal into a one-step correction instead of a loop.
        exc = _rejected("SELECT country FROM country")
        assert "the whole row of country" in str(exc)
        assert "country.country" in exc.llm_hint
        assert "table or alias" in exc.llm_hint

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


class TestSubqueriesUsedAsValues:
    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            (
                "SELECT id FROM orders o WHERE (SELECT email FROM users u WHERE u.id = o.user_id)"
                " LIKE 'a%'",
                "email",
            ),
            (
                "SELECT id FROM orders o WHERE 'alice@x.com' IN (SELECT email FROM users u"
                " WHERE u.id = o.user_id)",
                "email",
            ),
            (
                "SELECT id FROM users ORDER BY (SELECT email FROM users u2 WHERE u2.id = users.id)",
                "email",
            ),
            ("SELECT 1 FROM o JOIN u ON o.note = (SELECT email FROM users WHERE id = 1)", "email"),
            ("SELECT 1 FROM u WHERE x = ANY (SELECT email FROM v)", "email"),
            ("SELECT ARRAY(SELECT email FROM u) FROM t", "email"),
            (
                "SELECT 1 FROM u WHERE x IN (SELECT id FROM v UNION ALL SELECT email FROM w)",
                "email",
            ),
            ("SELECT 1 FROM u WHERE x IN (SELECT e FROM (SELECT email AS e FROM v) s)", "e"),
            (
                "SELECT e FROM (SELECT (SELECT email FROM users LIMIT 1) AS e) t WHERE e LIKE 'a%'",
                "e",
            ),
            ("SELECT id FROM t ORDER BY 1, (SELECT email FROM u LIMIT 1)", "email"),
        ],
    )
    def test_a_subquery_that_projects_the_column_is_rejected_where_a_column_would_be(
        self, sql: str, column: str
    ) -> None:
        _column_message(_rejected(sql), column)

    def test_a_subquery_that_projects_a_star_is_a_shape_rejection(self) -> None:
        exc = _rejected("SELECT id FROM t WHERE (SELECT * FROM u LIMIT 1) = 'x'")
        assert "a star inside a subquery used as a value" in str(exc)

    def test_the_alias_of_a_scalar_item_is_sensitive_at_the_same_level(self) -> None:
        _column_message(
            _rejected("SELECT (SELECT email FROM users LIMIT 1) AS e FROM t ORDER BY e"), "e"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM o WHERE (SELECT email FROM u LIMIT 1) LIKE 'a%'",
            "SELECT id FROM o WHERE 'x' IN (SELECT `email` FROM u)",
        ],
    )
    def test_mysql_value_subqueries_are_rejected(self, sql: str) -> None:
        _column_message(_rejected(sql, dialect="mysql"), "email")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT email FROM users LIMIT 1) AS e FROM t",
            "SELECT (SELECT email FROM users LIMIT 1) FROM t",
            "SELECT ((SELECT email FROM users LIMIT 1)) FROM t",
            "SELECT id FROM orders o WHERE (SELECT count(*) FROM users u WHERE u.id = o.user_id)"
            " > 0",
            "SELECT 1 FROM u WHERE EXISTS (SELECT email FROM v)",
            "SELECT 1 FROM u WHERE id IN (SELECT id FROM v)",
            "(SELECT email FROM u)",
        ],
    )
    def test_a_plain_scalar_item_and_non_sensitive_subqueries_pass(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")


class TestSetOperations:
    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            (
                "SELECT x FROM (SELECT id AS x FROM users WHERE false UNION ALL SELECT email"
                " FROM users) t WHERE x LIKE 'a%'",
                "x",
            ),
            # A position on the set operation is checked in every branch, later branches
            # first, so the message names the sensitive column rather than the laundered
            # first-branch name.
            ("SELECT id FROM u UNION ALL SELECT email FROM u ORDER BY 1", "email"),
            ("SELECT id FROM u UNION ALL SELECT email AS e FROM u ORDER BY 1", "email"),
            ("SELECT id, name FROM u UNION ALL SELECT id, email FROM v ORDER BY 2", "email"),
            (
                "WITH c AS (SELECT id FROM a UNION ALL SELECT email FROM b) SELECT id FROM c"
                " WHERE id = 'x'",
                "id",
            ),
            (
                "SELECT id FROM a UNION ALL SELECT id FROM b UNION ALL SELECT email FROM c"
                " ORDER BY 1",
                "email",
            ),
            (
                "SELECT id FROM a UNION ALL SELECT (SELECT email FROM b LIMIT 1) ORDER BY 1",
                "email",
            ),
            (
                "SELECT id FROM a UNION ALL (SELECT x FROM b UNION ALL SELECT email FROM c)"
                " ORDER BY 1",
                "email",
            ),
            (
                "WITH RECURSIVE r AS (SELECT id FROM a UNION ALL SELECT email FROM b)"
                " SELECT id FROM r WHERE id = 'x'",
                "id",
            ),
            # The laundered name is sensitive everywhere, by name: the first branch's own
            # WHERE on id is refused too (Decisions).
            ("SELECT id FROM a WHERE id > 5 UNION ALL SELECT email FROM b", "id"),
        ],
    )
    def test_a_later_branch_makes_the_first_branch_output_sensitive(
        self, sql: str, column: str
    ) -> None:
        _column_message(_rejected(sql), column)

    @pytest.mark.parametrize(
        ("sql", "what"),
        [
            (
                "SELECT id FROM a UNION SELECT * FROM b",
                "a star in a later branch of a set operation",
            ),
            # Refused whatever the tables hold: the check cannot see the expansion (Decisions).
            (
                "SELECT * FROM a UNION ALL SELECT * FROM b",
                "a star in a later branch of a set operation",
            ),
            (
                "SELECT * FROM a UNION SELECT email FROM b",
                "a star in the first branch of a set operation",
            ),
            (
                "SELECT id, * FROM a UNION SELECT id, email FROM b",
                "a star in the first branch of a set operation",
            ),
            (
                "SELECT lower(name) FROM a UNION SELECT email FROM b",
                "a set operation that reads a sensitive column under an unnamed output",
            ),
        ],
    )
    def test_a_set_operation_the_mapping_cannot_follow_is_rejected(
        self, sql: str, what: str
    ) -> None:
        assert what in str(_rejected(sql))

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT email FROM a UNION ALL SELECT email FROM b",
            "SELECT id FROM a UNION SELECT id FROM b ORDER BY 1",
            "SELECT id FROM a INTERSECT SELECT id FROM b",
            "SELECT * FROM a UNION ALL SELECT id, name FROM b",
            "SELECT id, email FROM a UNION ALL SELECT id, email FROM b ORDER BY 1",
            "SELECT lower(name) FROM a UNION SELECT id FROM b",
            "SELECT id FROM a INTERSECT SELECT id FROM b EXCEPT SELECT id FROM c",
        ],
    )
    def test_set_operations_without_a_sensitive_mapping_pass(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")


def _compared_message(exc: SensitiveColumnRejectedError, column: str) -> None:
    message = str(exc)
    assert f"compares the column {column} across a UNION, INTERSECT or EXCEPT" in message
    assert "UNION ALL" in exc.llm_hint
    assert column in exc.llm_hint
    stripped = message.replace(column, "")
    assert "'" not in stripped and '"' not in stripped


class TestSetOperationComparisons:
    # Duplicate removal and row comparison reveal whether values match, so only UNION ALL
    # may carry a sensitive item.
    @pytest.mark.parametrize(
        ("sql", "column"),
        [
            ("SELECT id, email FROM users INTERSECT SELECT id, 'alice@x.com' FROM users", "email"),
            ("SELECT email FROM users EXCEPT SELECT 'alice@x.com'", "email"),
            ("SELECT email FROM users EXCEPT ALL SELECT 'alice@x.com'", "email"),
            ("SELECT email FROM users INTERSECT ALL SELECT 'alice@x.com'", "email"),
            ("SELECT email FROM a UNION SELECT email FROM b", "email"),
            ("SELECT email FROM a UNION DISTINCT SELECT email FROM b", "email"),
            ("SELECT 1 FROM users u WHERE EXISTS (SELECT u.email INTERSECT SELECT 'x')", "email"),
            ("SELECT 1 FROM users u, LATERAL (SELECT u.email INTERSECT SELECT 'x') l", "email"),
            ("SELECT count(*) FROM (SELECT email FROM users INTERSECT SELECT 'x') s", "email"),
            ("SELECT count(*) FROM (SELECT email FROM users UNION SELECT 'x') s", "email"),
            (
                "WITH c AS (SELECT email FROM users INTERSECT SELECT 'x') SELECT count(*) FROM c",
                "email",
            ),
            (
                "SELECT 1 FROM t WHERE 1 IN (SELECT 1 FROM (SELECT email FROM u EXCEPT"
                " SELECT 'x') s)",
                "email",
            ),
            # The UNION removes duplicates across every branch under it.
            ("SELECT id FROM a UNION ALL SELECT email FROM b UNION SELECT 'x'", "email"),
            # A laundered name counts too.
            ("SELECT e FROM (SELECT email AS e FROM u) s INTERSECT SELECT 'x'", "e"),
        ],
    )
    def test_a_sensitive_item_in_a_comparing_set_operation_is_rejected(
        self, sql: str, column: str
    ) -> None:
        _compared_message(_rejected(sql), column)
        _compared_message(_rejected(sql, dialect="mysql"), column)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM a UNION SELECT id, name FROM b",
            "SELECT u.* FROM u INTERSECT SELECT id FROM a",
            "SELECT (SELECT * FROM v LIMIT 1) AS x FROM a EXCEPT SELECT 1",
        ],
    )
    def test_a_star_in_a_comparing_set_operation_is_a_shape(self, sql: str) -> None:
        assert "a star in a UNION, INTERSECT or EXCEPT" in str(_rejected(sql))

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT email FROM a UNION ALL SELECT email FROM b",
            "SELECT id FROM a INTERSECT SELECT id FROM b",
            "SELECT id FROM a EXCEPT SELECT id FROM b",
            "SELECT count(*) FROM (SELECT email FROM users UNION ALL SELECT 'x') s",
        ],
    )
    def test_union_all_and_non_sensitive_comparisons_pass(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")
        check_strict_sensitive(sql, _privacy(), "mysql")


class TestSetOperationOperands:
    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    @pytest.mark.parametrize(
        "sql",
        [
            # sqlglot reads TABLE users as a table named TABLE (nested) or a column named
            # TABLE (top level), so the check cannot see the columns it brings in.
            "SELECT name FROM (TABLE users UNION ALL SELECT email FROM users) t"
            " WHERE name LIKE 'a%'",
            "TABLE users UNION ALL SELECT email FROM users",
        ],
    )
    def test_an_operand_that_is_not_a_query_is_rejected(self, sql: str, dialect: str) -> None:
        exc = _rejected(sql, dialect=dialect)
        assert "a set operation operand the check cannot read" in str(exc)

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("VALUES (1) UNION ALL SELECT email FROM b", "postgresql"),
            ("VALUES (1) UNION SELECT id FROM b", "postgresql"),
            ("SELECT id FROM b UNION ALL VALUES (1)", "postgresql"),
            ("SELECT 1 UNION ALL (VALUES (1))", "postgresql"),
            ("VALUES ROW(1) UNION ALL SELECT email FROM b", "mysql"),
        ],
    )
    def test_a_values_list_in_a_set_operation_is_named(self, sql: str, dialect: str) -> None:
        message = str(_rejected(sql, dialect=dialect))
        assert "a VALUES list in a set operation" in message
        assert "a star" not in message

    def test_a_first_operand_values_list_without_a_comparison_passes(self) -> None:
        # Documented in the user guide: only a later operand is refused unconditionally.
        check_strict_sensitive("VALUES (1) UNION ALL SELECT id FROM b", _privacy(), "postgresql")


class TestStarAliases:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT * FROM single LIMIT 1) AS x FROM t ORDER BY x",
            "SELECT 1 FROM (SELECT (SELECT * FROM single LIMIT 1) AS x FROM t) s WHERE x = 1",
            "SELECT 1 FROM (SELECT x AS y FROM (SELECT (SELECT * FROM single LIMIT 1) AS x"
            " FROM t) s) r WHERE y = 1",
            "SELECT 1 FROM (SELECT (SELECT * FROM single LIMIT 1) AS x FROM t) s JOIN v USING (x)",
        ],
    )
    def test_an_alias_of_a_star_subquery_is_a_star_outside_the_select_list(self, sql: str) -> None:
        exc = _rejected(sql)
        assert "a star inside a subquery used as a value" in str(exc)
        assert "the column" not in str(exc)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT (SELECT * FROM single LIMIT 1) AS x FROM t",
            "SELECT x FROM (SELECT (SELECT * FROM single LIMIT 1) AS x FROM t) s",
        ],
    )
    def test_an_alias_of_a_star_subquery_as_a_plain_item_passes(self, sql: str) -> None:
        check_strict_sensitive(sql, _privacy(), "postgresql")


class TestUnnamedSubqueryItems:
    # MySQL names an unaliased item after its SQL text, which the check cannot follow.
    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM (SELECT (SELECT ssn FROM u LIMIT 1) FROM t) d"
            " WHERE d.`(SELECT ssn FROM u LIMIT 1)` LIKE 'a%'",
            "SELECT 1 FROM (SELECT (SELECT ssn FROM u LIMIT 1) FROM t) d",
            "WITH c AS (SELECT (SELECT ssn FROM u LIMIT 1) FROM t) SELECT 1 FROM c",
            "SELECT 1 FROM t, LATERAL (SELECT (SELECT ssn FROM u LIMIT 1) FROM v) l",
            "SELECT 1 FROM (SELECT 1 AS a UNION ALL SELECT (SELECT ssn FROM u LIMIT 1)) d",
            "SELECT 1 FROM (SELECT (SELECT * FROM u LIMIT 1) FROM t) d",
        ],
    )
    def test_an_unnamed_sensitive_subquery_item_below_the_top_is_rejected(
        self, sql: str, dialect: str
    ) -> None:
        if dialect == "postgresql":
            sql = sql.replace("`", '"')
        privacy = _privacy(sensitive_columns=["ssn"])
        exc = _rejected(sql, privacy, dialect)
        assert "an unnamed subquery item that reads a sensitive column" in str(exc)

    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM (SELECT (SELECT ssn FROM u LIMIT 1) AS s FROM t) d",
            "SELECT (SELECT ssn FROM u LIMIT 1) FROM t",
            "SELECT id FROM a UNION ALL SELECT (SELECT ssn FROM u LIMIT 1)",
            "(SELECT (SELECT ssn FROM u LIMIT 1) FROM t)",
            "SELECT 1 FROM (SELECT (SELECT id FROM u LIMIT 1) FROM t) d",
        ],
    )
    def test_a_named_or_top_level_subquery_item_passes(self, sql: str, dialect: str) -> None:
        check_strict_sensitive(sql, _privacy(sensitive_columns=["ssn"]), dialect)


class TestNesting:
    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT " + "(" * 3000 + "1" + ")" * 3000,
            "SELECT * FROM " + "(SELECT * FROM " * 800 + "u" + ") s" * 800,
        ],
    )
    def test_a_query_nested_too_deeply_is_rejected(self, sql: str, dialect: str) -> None:
        exc = _rejected(sql, dialect=dialect)
        assert str(exc) == (
            "Strict sensitive columns: the query is nested too deeply to check, so it was not run."
        )
        assert exc.llm_hint == "Simplify the query."

    def test_a_recursion_error_in_the_walk_is_rejected(self) -> None:
        from unittest.mock import patch

        with patch("app.services.sensitive_guard._laundered_names", side_effect=RecursionError):
            exc = _rejected("SELECT id FROM u")
        assert "nested too deeply to check" in str(exc)
