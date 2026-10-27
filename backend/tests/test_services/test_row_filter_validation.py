# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""validate_row_filter: what an admin may save as the connection's row filter."""

from __future__ import annotations

import pytest

from app.services.row_filter import RowFilterInvalidError, validate_row_filter

_SEMICOLON = "must be a single condition without ';'"
_COMMENTS = "must not contain comment markers, even inside quotes"
_DOLLAR = "must not contain '$'"
_LEADING_WHERE = "is a condition: write it without the leading WHERE"
_PARENTHESES = (
    "has parentheses that do not balance, or a string that cannot be checked safely"
    " (a backslash or an unterminated quote)"
)
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
            ("tenant_id IN (1, 2", "PostgreSQL", _PARENTHESES),
            ("tenant_id = 1) OR (1 = 1", "MySQL", _PARENTHESES),
            ("name = 'O\\'Brien'", "MySQL", _PARENTHESES),
            ("name = 'C:\\path'", "PostgreSQL", _PARENTHESES),
            ("name = 'abc", "PostgreSQL", _PARENTHESES),
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
