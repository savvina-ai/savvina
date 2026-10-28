# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The LLM receives our own description of a failed query, never the database's words."""

from __future__ import annotations

import pytest

from app.datasources.errors import ErrorCategory
from app.datasources.models import ColumnInfo, DataSourceSchema, TableInfo
from app.services.error_feedback import (
    _CATEGORY_SENTENCE,
    describe_error_for_llm,
    mentioned_names,
    redact_error_for_user,
)


def _schema() -> DataSourceSchema:
    return DataSourceSchema(
        source_type="postgresql",
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name="orders",
                table_type="table",
                columns=[
                    ColumnInfo(name="amount", data_type="numeric", native_type="numeric"),
                    ColumnInfo(name="status", data_type="text", native_type="text"),
                ],
            )
        ],
    )


class TestMentionedNames:
    def test_a_name_from_the_sql_and_a_suggestion_from_the_schema(self) -> None:
        # "amount" exists only in the schema — a "did you mean", which UNKNOWN_NAME is for.
        names = mentioned_names(
            'column "amont" does not exist; perhaps you meant amount',
            sql="SELECT amont FROM orders",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == ["amont", "amount"]

    def test_schema_only_names_are_not_suggested_for_a_non_suggesting_category(self) -> None:
        # GROUPING echoes names but does not suggest: "status" was written in the SQL,
        # "amount" exists only in the schema.
        names = mentioned_names(
            "column status must appear in the GROUP BY clause; amount is not grouped",
            sql="SELECT status FROM orders GROUP BY 1",
            schema=_schema(),
            category=ErrorCategory.GROUPING,
        )
        assert names == ["status"]

    def test_quoted_and_dotted_names(self) -> None:
        names = mentioned_names(
            'relation "public.ordrs" does not exist',
            sql="SELECT * FROM public.ordrs",
            schema=None,
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == ["public.ordrs"]

    def test_values_keywords_and_literals_are_never_names(self) -> None:
        names = mentioned_names(
            "invalid input syntax for type integer: \"Jane Doe\" near SELECT 'paid'",
            sql="SELECT CAST(status AS int) FROM orders WHERE status = 'paid'",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == []

    def test_a_single_character_name_is_not_trusted(self) -> None:
        names = mentioned_names(
            'column "x" does not exist',
            sql="SELECT x FROM t",
            schema=None,
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == []

    def test_a_secret_never_glues_onto_a_known_name(self) -> None:
        names = mentioned_names(
            "xamount is unknown, and so is amount.xyz",
            sql="SELECT amount FROM orders",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == []

    def test_data_errors_echo_no_names(self) -> None:
        # Oracle: the alias "jane" matches the stored value in the engine text, so echoing
        # it would confirm that value.
        error = 'invalid input syntax for type integer: "Jane"'
        sql = "SELECT CAST(name AS int) AS jane FROM customers"
        assert mentioned_names(error, sql=sql, schema=None, category=ErrorCategory.DATA) == []
        text = describe_error_for_llm(ErrorCategory.DATA, error, sql=sql, schema=None)
        assert "Names the error refers to" not in text

    def test_other_errors_echo_no_names(self) -> None:
        names = mentioned_names(
            "Duplicate entry 'amount' for key 'PRIMARY'",
            sql="SELECT amount FROM orders",
            schema=_schema(),
            category=ErrorCategory.OTHER,
        )
        assert names == []

    def test_a_mysql_double_quoted_value_is_not_echoed_for_data(self) -> None:
        # MySQL tags "jane" in the SQL as a string literal; sqlparse reads it as a Symbol.
        names = mentioned_names(
            "Incorrect integer value: 'jane' for column 'id' at row 1",
            sql='SELECT * FROM customers WHERE id = "jane"',
            schema=None,
            category=ErrorCategory.DATA,
        )
        assert names == []

    @pytest.mark.parametrize(
        "glued", ["2amount", "$amount", "ßamount", "éamount", "amount.9x", "amount.ßx"]
    )
    def test_a_token_glued_to_a_non_name_character_is_rejected(self, glued: str) -> None:
        names = mentioned_names(
            f"unknown {glued} here",
            sql="SELECT amount FROM orders",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == []

    @pytest.mark.parametrize("error", ['column "amount" does not exist', "bad (amount)"])
    def test_a_delimited_name_still_matches_next_to_punctuation(self, error: str) -> None:
        names = mentioned_names(
            error,
            sql="SELECT amount FROM orders",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == ["amount"]

    def test_function_argument_types_are_not_names(self) -> None:
        # PostgreSQL lists the argument types before "does not exist"; "text" and "zone"
        # must not echo back as though the model had written them.
        schema = DataSourceSchema(
            source_type="postgresql",
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name="t",
                    table_type="table",
                    columns=[ColumnInfo(name="zone", data_type="text", native_type="text")],
                )
            ],
        )
        names = mentioned_names(
            "function date_format(timestamp with time zone, unknown) does not exist",
            sql="SELECT date_format(created_at, 'x') FROM t",
            schema=schema,
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == ["date_format"]

    def test_caps_at_ten_names(self) -> None:
        schema = DataSourceSchema(
            source_type="postgresql",
            tables=[
                TableInfo(
                    catalog=None,
                    schema_name="public",
                    name=f"tablenum{i}",
                    table_type="table",
                    columns=[],
                )
                for i in range(20)
            ],
        )
        error = " ".join(f"tablenum{i}" for i in range(20))
        names = mentioned_names(
            error, sql="SELECT 1", schema=schema, category=ErrorCategory.UNKNOWN_NAME
        )
        assert len(names) == 10

    def test_a_giant_dotted_chain_is_not_echoed(self) -> None:
        chain = ".".join(["amount", "status"] * 1000)
        names = mentioned_names(
            f"error near {chain}",
            sql="SELECT amount, status FROM orders",
            schema=_schema(),
            category=ErrorCategory.UNKNOWN_NAME,
        )
        assert names == []


class TestDescribeErrorForLlm:
    def test_the_engine_text_never_appears(self) -> None:
        text = describe_error_for_llm(
            ErrorCategory.DATA,
            'invalid input syntax for type integer: "Jane Doe"',
            sql="SELECT CAST(customer_name AS int) FROM customers",
            schema=None,
        )
        assert "Jane" not in text
        assert "could not be converted, parsed or fitted" in text
        assert "withheld" in text

    def test_names_the_model_already_has_are_listed(self) -> None:
        text = describe_error_for_llm(
            ErrorCategory.UNKNOWN_NAME,
            'column "amont" does not exist',
            sql="SELECT amont FROM orders",
            schema=_schema(),
        )
        assert text.endswith("Names the error refers to: amont.")

    def test_every_category_has_a_sentence(self) -> None:
        for category in ErrorCategory:
            text = describe_error_for_llm(category, "", sql="SELECT 1", schema=None)
            assert text.strip()

    def test_unknown_name_covers_wrong_argument_types(self) -> None:
        text = describe_error_for_llm(ErrorCategory.UNKNOWN_NAME, "", sql="SELECT 1", schema=None)
        assert "argument types" in text


class TestRedactErrorForUser:
    """The error the user sees keeps the engine's words only where they quote names."""

    def test_postgres_conversion_value_is_redacted(self) -> None:
        out = redact_error_for_user(
            ErrorCategory.DATA, 'invalid input syntax for type integer: "Jane Doe"'
        )
        assert "Jane" not in out
        assert "[REDACTED]" in out
        assert "invalid input syntax for type integer" in out

    def test_mysql_single_quoted_value_is_redacted(self) -> None:
        out = redact_error_for_user(
            ErrorCategory.DATA, "Incorrect integer value: 'Jane' for column 'age' at row 1"
        )
        assert "Jane" not in out
        assert "Incorrect integer value" in out

    def test_only_the_first_line_is_kept(self) -> None:
        out = redact_error_for_user(
            ErrorCategory.OTHER, "query failed\nDETAIL: Failing row contains (1, Jane, 42)."
        )
        assert out == "query failed"

    def test_key_detail_is_redacted(self) -> None:
        out = redact_error_for_user(ErrorCategory.OTHER, "Key (email)=(a@b.com) already exists.")
        assert "a@b.com" not in out
        assert "=([REDACTED])" in out

    @pytest.mark.parametrize(
        "error",
        [
            "invalid value: 'it''s Jane'",
            'invalid value: "say ""Jane"" twice"',
        ],
    )
    def test_doubled_quote_escapes_stay_inside_the_redacted_span(self, error: str) -> None:
        out = redact_error_for_user(ErrorCategory.DATA, error)
        assert "Jane" not in out
        assert out == "invalid value: [REDACTED]"

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ("operator does not exist: jane_op", "operator does not exist: [REDACTED]"),
            ("more than one operator named +", "more than one operator named [REDACTED]"),
        ],
    )
    def test_an_unquoted_operator_lookup_value_is_redacted(self, error: str, expected: str) -> None:
        # PostgreSQL's regoper input echoes the stored value unquoted.
        assert redact_error_for_user(ErrorCategory.DATA, error) == expected

    def test_a_genuine_operator_error_keeps_its_types(self) -> None:
        error = "operator does not exist: integer + boolean"
        assert redact_error_for_user(ErrorCategory.TYPE_MISMATCH, error) == error

    def test_backtick_identifiers_stay(self) -> None:
        out = redact_error_for_user(ErrorCategory.DATA, "Data too long for column `notes`")
        assert out == "Data too long for column `notes`"

    @pytest.mark.parametrize(
        "category",
        [
            ErrorCategory.UNKNOWN_NAME,
            ErrorCategory.AMBIGUOUS_NAME,
            ErrorCategory.GROUPING,
            ErrorCategory.TYPE_MISMATCH,
            ErrorCategory.PERMISSION,
            ErrorCategory.SYNTAX,
        ],
    )
    def test_name_quoting_categories_are_unchanged(self, category: ErrorCategory) -> None:
        error = 'column "amont" does not exist\nHINT: Perhaps you meant "amount".'
        assert redact_error_for_user(category, error) == error

    @pytest.mark.parametrize(
        "category",
        [
            ErrorCategory.DATA,
            ErrorCategory.OTHER,
            ErrorCategory.DIVISION_BY_ZERO,
            ErrorCategory.CARDINALITY,
            ErrorCategory.UNSUPPORTED,
            ErrorCategory.TIMEOUT,
        ],
    )
    def test_value_quoting_categories_are_redacted(self, category: ErrorCategory) -> None:
        out = redact_error_for_user(category, "failed on 'Jane'")
        assert "Jane" not in out

    def test_empty_result_falls_back_to_the_category_sentence(self) -> None:
        assert (
            redact_error_for_user(ErrorCategory.DATA, "") == _CATEGORY_SENTENCE[ErrorCategory.DATA]
        )
        assert (
            redact_error_for_user(ErrorCategory.OTHER, "\nDETAIL: x")
            == (_CATEGORY_SENTENCE[ErrorCategory.OTHER])
        )
