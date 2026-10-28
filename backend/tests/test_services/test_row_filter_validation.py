# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""validate_row_filter: what an admin may save as the connection's row filter."""

from __future__ import annotations

import pytest

from app.services.row_filter import RowFilterInvalidError, validate_row_filter, validate_row_filters

_SEMICOLON = "must be a single condition without ';'"
_COMMENTS = "must not contain comment markers, even inside quotes"
_DOLLAR = "must not contain '$'"
_LEADING_WHERE = "is a condition: write it without the leading WHERE"
_TOO_LONG = "is too long (1000 characters max)"


class TestEmptyMeansNoFilter:
    @pytest.mark.parametrize("text", [None, "", "   ", "\n\t"])
    def test_returns_none(self, text: str | None) -> None:
        assert validate_row_filter(text, "PostgreSQL") is None


class TestAccepted:
    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL", ""])
    @pytest.mark.parametrize(
        "text",
        [
            "tenant_id = 42",
            "org_id = 'acme' AND region IN ('EU', 'US')",
            "tenant_id IN (SELECT id FROM tenants WHERE active)",
            "note = ')'",
            "where_flag = 1",
        ],
    )
    def test_returns_the_stripped_text(self, text: str, dialect: str) -> None:
        assert validate_row_filter(f"  {text}\n", dialect) == text

    def test_hash_is_not_a_comment_on_postgresql(self) -> None:
        assert validate_row_filter("tag = '#1'", "PostgreSQL") == "tag = '#1'"

    def test_flags_xor_is_not_a_comment_on_postgresql(self) -> None:
        assert validate_row_filter("(flags # 4) = 0", "PostgreSQL") == "(flags # 4) = 0"

    @pytest.mark.parametrize("dialect", ["PostgreSQL", "MySQL", ""])
    def test_a_newline_inside_is_kept(self, dialect: str) -> None:
        text = "tenant_id = 42\nAND region = 'EU'"
        assert validate_row_filter(text, dialect) == text

    def test_exactly_the_maximum_length_is_accepted(self) -> None:
        text = "a = '" + "x" * 994 + "'"
        assert len(text) == 1000
        assert validate_row_filter(text, "PostgreSQL") == text
        assert validate_row_filter(f"  {text}\n", "PostgreSQL") == text


class TestRejected:
    @pytest.mark.parametrize(
        ("text", "dialect", "message"),
        [
            ("tenant_id = 42; DROP TABLE t", "PostgreSQL", _SEMICOLON),
            ("note = 'a;b'", "PostgreSQL", _SEMICOLON),
            ("WHERE a = 1; b", "PostgreSQL", _SEMICOLON),
            ("tenant_id = 42 -- all", "PostgreSQL", _COMMENTS),
            ("tenant_id = 42 /* all */", "MySQL", _COMMENTS),
            ("tenant_id = 42 # all", "MySQL", _COMMENTS),
            ("tenant_id = 42 # all", "", _COMMENTS),
            ("tag = '#1'", "MySQL", _COMMENTS),
            ("tag = $$x$$", "PostgreSQL", _DOLLAR),
            ("a$b = 1", "MySQL", _DOLLAR),
            ("WHERE tenant_id = 42", "PostgreSQL", _LEADING_WHERE),
            ("where tenant_id = 42", "MySQL", _LEADING_WHERE),
            ("WHERE(tenant_id = 42)", "PostgreSQL", _LEADING_WHERE),
            ("x = 1 AND " * 200, "PostgreSQL", _TOO_LONG),
            ("a = '" + "x" * 995 + "'", "PostgreSQL", _TOO_LONG),
        ],
    )
    def test_raises_with_the_message(self, text: str, dialect: str, message: str) -> None:
        with pytest.raises(RowFilterInvalidError) as excinfo:
            validate_row_filter(text, dialect)
        assert str(excinfo.value) == message
        # The message completes the sentence "Row filter ...".
        assert str(excinfo.value)[0].islower()


_PG_PARSE = "is not a valid condition for PostgreSQL"
_MY_PARSE = "is not a valid condition for MySQL"
_OBJ = "row_filters must be an object mapping schema.table to a condition"


class TestValidateRowFilters:
    def test_returns_the_clean_map_with_stripped_keys_and_conditions(self) -> None:
        raw = {" public.orders ": "  tenant_id = 42\n", "public.items": "org_id = 'acme'"}
        assert validate_row_filters(raw, "PostgreSQL") == {
            "public.orders": "tenant_id = 42",
            "public.items": "org_id = 'acme'",
        }

    @pytest.mark.parametrize(
        ("key", "dialect", "stored"),
        [
            ('"public"."Orders"', "PostgreSQL", "public.Orders"),
            ('public."Orders"', "PostgreSQL", "public.Orders"),
            ("`sample_delivery`.`Orders`", "MySQL", "sample_delivery.Orders"),
            ("Public.Orders", "PostgreSQL", "Public.Orders"),
            ("s_1.t$2", "PostgreSQL", "s_1.t$2"),
        ],
    )
    def test_quoted_parts_are_stored_without_quotes_and_casing_is_kept(
        self, key: str, dialect: str, stored: str
    ) -> None:
        assert validate_row_filters({key: "x = 1"}, dialect) == {stored: "x = 1"}

    def test_keys_differing_only_in_case_are_both_kept(self) -> None:
        raw = {"public.orders": "a = 1", "public.Orders": "b = 2"}
        assert validate_row_filters(raw, "PostgreSQL") == raw

    def test_empty_map_is_allowed(self) -> None:
        assert validate_row_filters({}, "PostgreSQL") == {}

    @pytest.mark.parametrize(
        ("raw", "dialect", "message"),
        [
            (["public.orders"], "PostgreSQL", _OBJ),
            ("x", "PostgreSQL", _OBJ),
            ({"public.orders": 7}, "PostgreSQL", "row_filters keys and conditions must be strings"),
            ({"orders": "x = 1"}, "PostgreSQL", 'row_filters key "orders" must be schema.table'),
            ({"a.b.c": "x = 1"}, "PostgreSQL", 'row_filters key "a.b.c" must be schema.table'),
            ({"public.": "x = 1"}, "PostgreSQL", 'row_filters key "public." has an invalid name'),
            ({".orders": "x = 1"}, "PostgreSQL", 'row_filters key ".orders" has an invalid name'),
            (
                {"public.my table": "x = 1"},
                "PostgreSQL",
                'row_filters key "public.my table" has an invalid name',
            ),
            (
                {"public.1orders": "x = 1"},
                "PostgreSQL",
                'row_filters key "public.1orders" has an invalid name',
            ),
            (
                {'public."Ord"ers"': "x = 1"},
                "PostgreSQL",
                'row_filters key "public."Ord"ers"" has an invalid name',
            ),
            (
                {'public.""': "x = 1"},
                "PostgreSQL",
                'row_filters key "public.""" has an invalid name',
            ),
            ({"`s`.`t`": "x = 1"}, "PostgreSQL", 'row_filters key "`s`.`t`" has an invalid name'),
            ({"public.orders": ""}, "PostgreSQL", 'row_filters["public.orders"] is empty'),
            ({"public.orders": "   "}, "PostgreSQL", 'row_filters["public.orders"] is empty'),
            (
                {"public.orders": "x = 1; DROP TABLE t"},
                "PostgreSQL",
                "row_filters[\"public.orders\"] must be a single condition without ';'",
            ),
            (
                {"public.orders": "WHERE x = 1"},
                "PostgreSQL",
                'row_filters["public.orders"] is a condition: write it without the leading WHERE',
            ),
            (
                {"public.orders": "tag = '#1'"},
                "MySQL",
                'row_filters["public.orders"] must not contain comment markers, even inside quotes',
            ),
            (
                {"public.orders": "tenant_id IN (1, 2"},
                "PostgreSQL",
                f'row_filters["public.orders"] {_PG_PARSE}',
            ),
            (
                {"public.orders": "tenant_id = 1) OR (1 = 1"},
                "MySQL",
                f'row_filters["public.orders"] {_MY_PARSE}',
            ),
            (
                {"public.orders": "name = 'abc"},
                "PostgreSQL",
                f'row_filters["public.orders"] {_PG_PARSE}',
            ),
            (
                {"public.orders": "name = 'O\\'Brien'"},
                "PostgreSQL",
                f'row_filters["public.orders"] {_PG_PARSE}',
            ),
            (
                {"public.orders": "tenant_id = 7 UNION SELECT 1"},
                "PostgreSQL",
                f'row_filters["public.orders"] {_PG_PARSE}',
            ),
            (
                {"public.orders": "AND x = 1"},
                "PostgreSQL",
                f'row_filters["public.orders"] {_PG_PARSE}',
            ),
            (
                {"public.orders": "1=1 INTO OUTFILE '/x'"},
                "MySQL",
                f'row_filters["public.orders"] {_MY_PARSE}',
            ),
        ],
    )
    def test_rejected_with_the_message(self, raw: object, dialect: str, message: str) -> None:
        with pytest.raises(RowFilterInvalidError) as excinfo:
            validate_row_filters(raw, dialect)
        assert str(excinfo.value) == message

    @pytest.mark.parametrize(
        ("cond", "dialect"),
        [
            ("name = 'C:\\path'", "PostgreSQL"),
            ("name = 'C:\\path'", "MySQL"),
            ("name = 'O\\'Brien'", "MySQL"),
        ],
    )
    def test_a_backslash_the_dialect_reads_is_accepted(self, cond: str, dialect: str) -> None:
        # Phase 1 failed closed on any backslash; the parse reads it as the engine does.
        assert validate_row_filters({"s.t": cond}, dialect) == {"s.t": cond}

    def test_too_many_entries(self) -> None:
        raw = {f"public.t{i}": "x = 1" for i in range(201)}
        with pytest.raises(RowFilterInvalidError, match="at most 200 entries"):
            validate_row_filters(raw, "PostgreSQL")

    def test_duplicate_after_stripping(self) -> None:
        with pytest.raises(RowFilterInvalidError, match=r'key "public\.orders" appears twice'):
            validate_row_filters(
                {"public.orders": "a = 1", " public.orders": "b = 2"}, "PostgreSQL"
            )

    def test_unknown_dialect_accepts_only_an_empty_map(self) -> None:
        # The rewriter refuses every query under a non-empty map on a dialect it does not
        # know, so the save is refused instead of producing a connection that runs nothing.
        assert validate_row_filters({}, "") == {}
        with pytest.raises(RowFilterInvalidError, match="not supported for this database"):
            validate_row_filters({"s.t": "x = 1"}, "")

    @pytest.mark.parametrize(
        "key",
        ['public." Orders"', 'public."Orders "', '"public "."t"', 'public." "', 'public."x "'],
    )
    def test_quoted_part_with_surrounding_whitespace_is_rejected(self, key: str) -> None:
        with pytest.raises(RowFilterInvalidError, match="has an invalid name"):
            validate_row_filters({key: "x = 1"}, "PostgreSQL")

    @pytest.mark.parametrize(
        ("key", "dialect"),
        [
            ("s." + "t" * 64, "PostgreSQL"),
            (
                's."' + "é" * 32 + '"',
                "PostgreSQL",
            ),  # 64 bytes in UTF-8; quoted: a bare name is ASCII
            ("s." + "t" * 65, "MySQL"),
        ],
    )
    def test_key_part_longer_than_the_engine_allows_is_rejected(
        self, key: str, dialect: str
    ) -> None:
        with pytest.raises(RowFilterInvalidError, match="is longer than the database allows"):
            validate_row_filters({key: "x = 1"}, dialect)

    @pytest.mark.parametrize(
        ("key", "dialect"),
        [
            ("s." + "t" * 63, "PostgreSQL"),
            ('s."' + "é" * 31 + '"', "PostgreSQL"),  # 62 bytes; quoted: a bare name is ASCII
            ("s." + "t" * 64, "MySQL"),
        ],
    )
    def test_key_part_at_the_engine_limit_is_accepted(self, key: str, dialect: str) -> None:
        assert validate_row_filters({key: "x = 1"}, dialect) == {key.replace('"', ""): "x = 1"}

    @pytest.mark.parametrize(
        "cond",
        ["x = '\x00'", "x = 1\x0b", "x = '\x1b[31m'", "x = '\x7f'"],
    )
    def test_control_characters_are_rejected(self, cond: str) -> None:
        with pytest.raises(RowFilterInvalidError, match="must not contain control characters"):
            validate_row_filters({"s.t": cond}, "PostgreSQL")

    def test_tab_and_newlines_are_allowed(self) -> None:
        cond = "x = 1\n\tAND y = 2\r\n"
        assert validate_row_filters({"s.t": cond}, "PostgreSQL") == {"s.t": cond.strip()}

    @pytest.mark.parametrize(
        "cond",
        [
            "(" * 21 + "x = 1" + ")" * 21,
            "x IN " + "(SELECT y FROM t WHERE y IN " * 20 + "(1)" + ")" * 20,
        ],
    )
    def test_nesting_deeper_than_20_is_rejected(self, cond: str) -> None:
        with pytest.raises(RowFilterInvalidError, match="is nested too deeply"):
            validate_row_filters({"s.t": cond}, "PostgreSQL")

    def test_parentheses_inside_a_string_literal_do_not_hide_the_nesting(self) -> None:
        cond = "x = '" + ")" * 45 + "' OR " + "(" * 45 + "x=1" + ")" * 45
        with pytest.raises(RowFilterInvalidError, match="is nested too deeply"):
            validate_row_filters({"s.t": cond}, "PostgreSQL")

    def test_an_apostrophe_in_a_quoted_name_does_not_hide_the_nesting(self) -> None:
        cond = '"a\'b" = 1 OR ' + "(" * 25 + "x=1" + ")" * 25 + " OR 'z' = 'z'"
        with pytest.raises(RowFilterInvalidError, match="is nested too deeply"):
            validate_row_filters({"s.t": cond}, "PostgreSQL")

    def test_a_mysql_backslash_escaped_quote_does_not_hide_the_nesting(self) -> None:
        cond = "x = 'it\\'s' OR " + "(" * 21 + "x=1" + ")" * 21
        with pytest.raises(RowFilterInvalidError, match="is nested too deeply"):
            validate_row_filters({"s.t": cond}, "MySQL")

    def test_parentheses_inside_a_string_are_not_counted(self) -> None:
        cond = "x = '" + "(" * 21 + "'"
        assert validate_row_filters({"s.t": cond}, "PostgreSQL") == {"s.t": cond}

    def test_a_lone_surrogate_in_a_key_is_an_invalid_name_not_a_500(self) -> None:
        with pytest.raises(RowFilterInvalidError, match="has an invalid name"):
            validate_row_filters({'public."\ud800"': "x = 1"}, "PostgreSQL")

    def test_a_lone_surrogate_in_a_condition_is_rejected(self) -> None:
        with pytest.raises(RowFilterInvalidError, match="must not contain control characters"):
            validate_row_filters({"s.t": "x = '\ud800'"}, "PostgreSQL")

    def test_nesting_of_20_is_accepted(self) -> None:
        cond = "(" * 20 + "x = 1" + ")" * 20
        assert validate_row_filters({"s.t": cond}, "PostgreSQL") == {"s.t": cond}

    def test_deep_operator_chains_are_a_422_not_a_500(self) -> None:
        # No parentheses, so the depth cap does not apply; the parser still recurses.
        cond = "NOT " * 400 + "x"
        with pytest.raises(RowFilterInvalidError):
            validate_row_filters({"s.t": cond[:1000]}, "PostgreSQL")

    @pytest.mark.parametrize(
        ("cond", "dialect"),
        [
            ("x = :tenant", "PostgreSQL"),
            ("x = ?", "PostgreSQL"),
            ("x = %s", "PostgreSQL"),
            ("x = ?", "MySQL"),
        ],
    )
    def test_placeholders_are_rejected(self, cond: str, dialect: str) -> None:
        with pytest.raises(RowFilterInvalidError, match="must not contain a placeholder"):
            validate_row_filters({"s.t": cond}, dialect)

    def test_data_modification_is_rejected(self) -> None:
        cond = "x = (WITH d AS (DELETE FROM t RETURNING id) SELECT 1)"
        with pytest.raises(RowFilterInvalidError, match="must not modify data"):
            validate_row_filters({"s.t": cond}, "PostgreSQL")

    def test_exactly_200_entries_are_accepted(self) -> None:
        raw = {f"public.t{i}": "x = 1" for i in range(200)}
        assert len(validate_row_filters(raw, "PostgreSQL")) == 200

    def test_bare_and_quoted_spellings_of_one_key_are_a_duplicate(self) -> None:
        raw = {"public.orders": "a = 1", '"public"."orders"': "b = 2"}
        with pytest.raises(RowFilterInvalidError, match="appears twice"):
            validate_row_filters(raw, "PostgreSQL")

    def test_double_quoted_key_is_rejected_on_mysql(self) -> None:
        with pytest.raises(RowFilterInvalidError, match="has an invalid name"):
            validate_row_filters({'"s"."t"': "x = 1"}, "MySQL")
