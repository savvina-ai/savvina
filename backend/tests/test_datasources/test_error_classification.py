# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Each adapter maps its engine's documented error codes onto one closed category."""

from __future__ import annotations

import aiomysql
import asyncpg.exceptions as asyncpg_exc
import pytest

from app.datasources.adapters.mysql import MySQLDataSource
from app.datasources.adapters.postgresql import PostgreSQLDataSource
from app.datasources.base import BaseDataSource
from app.datasources.errors import (
    ErrorCategory,
    category_for_sqlstate,
    classify_builtin,
    classify_chain,
    exception_chain,
    refine_undefined_function,
)


class TestCategoryForSqlstate:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("42703", ErrorCategory.UNKNOWN_NAME),  # undefined_column
            ("42P01", ErrorCategory.UNKNOWN_NAME),  # undefined_table
            ("42883", ErrorCategory.UNKNOWN_NAME),  # undefined_function
            ("3F000", ErrorCategory.UNKNOWN_NAME),  # invalid_schema_name
            ("42702", ErrorCategory.AMBIGUOUS_NAME),  # ambiguous_column
            ("42601", ErrorCategory.SYNTAX),  # syntax_error
            ("42803", ErrorCategory.GROUPING),  # grouping_error
            ("42804", ErrorCategory.TYPE_MISMATCH),  # datatype_mismatch
            ("42846", ErrorCategory.TYPE_MISMATCH),  # cannot_coerce
            ("22012", ErrorCategory.DIVISION_BY_ZERO),
            ("22P02", ErrorCategory.DATA),  # invalid_text_representation
            ("22003", ErrorCategory.DATA),  # numeric_value_out_of_range
            ("21000", ErrorCategory.CARDINALITY),
            ("42501", ErrorCategory.PERMISSION),  # insufficient_privilege
            ("28000", ErrorCategory.PERMISSION),  # invalid_authorization_specification
            ("0A000", ErrorCategory.UNSUPPORTED),
            ("25006", ErrorCategory.UNSUPPORTED),  # read_only_sql_transaction
            ("57014", ErrorCategory.TIMEOUT),  # query_canceled
            ("XX000", ErrorCategory.OTHER),
            ("4270", ErrorCategory.OTHER),  # wrong length
            ("", ErrorCategory.OTHER),
            (None, ErrorCategory.OTHER),
        ],
    )
    def test_documented_states_map(self, state: str | None, expected: ErrorCategory) -> None:
        assert category_for_sqlstate(state) is expected

    def test_lowercase_state_is_accepted(self) -> None:
        assert category_for_sqlstate("42p01") is ErrorCategory.UNKNOWN_NAME


class TestExceptionChain:
    def test_walks_cause_without_looping(self) -> None:
        root = ValueError("root")
        try:
            try:
                raise root
            except ValueError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            chain = exception_chain(wrapped)
        assert chain[0].args == ("wrapped",)
        assert chain[1] is root
        assert len(chain) == 2

    def test_a_cycle_is_walked_once(self) -> None:
        first, second = ValueError("first"), ValueError("second")
        first.__context__ = second
        second.__context__ = first
        assert exception_chain(first) == [first, second]

    def test_walks_context_when_there_is_no_cause(self) -> None:
        try:
            try:
                raise ValueError("root")
            except ValueError:
                raise RuntimeError("during handling")  # noqa: B904 — implicit context on purpose
        except RuntimeError as wrapped:
            chain = exception_chain(wrapped)
        assert isinstance(chain[1], ValueError)


class TestClassifyChain:
    def test_first_non_other_answer_wins(self) -> None:
        def per_error(err: BaseException) -> ErrorCategory | None:
            return ErrorCategory.UNKNOWN_NAME if isinstance(err, ValueError) else None

        try:
            try:
                raise ValueError("root")
            except ValueError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            assert classify_chain(wrapped, per_error) is ErrorCategory.UNKNOWN_NAME

    def test_other_is_treated_like_no_answer(self) -> None:
        calls: list[int] = []

        def per_error(_err: BaseException) -> ErrorCategory | None:
            calls.append(1)
            return ErrorCategory.OTHER

        try:
            try:
                raise ValueError("root")
            except ValueError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            assert classify_chain(wrapped, per_error) is ErrorCategory.OTHER
        assert len(calls) == 2

    def test_no_match_anywhere_is_other(self) -> None:
        assert classify_chain(ValueError("x"), lambda _err: None) is ErrorCategory.OTHER

    def test_a_raising_per_error_never_escapes(self) -> None:
        def per_error(_err: BaseException) -> ErrorCategory | None:
            raise TypeError("hostile lookup")

        assert classify_chain(ValueError("x"), per_error) is ErrorCategory.OTHER


class TestRefineUndefinedFunction:
    def test_operator_message_is_a_type_mismatch(self) -> None:
        assert (
            refine_undefined_function("operator does not exist: integer + text")
            is ErrorCategory.TYPE_MISMATCH
        )

    def test_function_message_stays_unknown_name(self) -> None:
        assert (
            refine_undefined_function("function date_format(timestamp, unknown) does not exist")
            is ErrorCategory.UNKNOWN_NAME
        )


class TestClassifyBuiltin:
    def test_direct_timeout(self) -> None:
        assert classify_builtin(TimeoutError()) is ErrorCategory.TIMEOUT

    def test_timeout_one_link_down(self) -> None:
        try:
            try:
                raise TimeoutError()
            except TimeoutError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            assert classify_builtin(wrapped) is ErrorCategory.TIMEOUT

    def test_timeout_two_links_down_is_not_a_query_timeout(self) -> None:
        try:
            try:
                try:
                    raise TimeoutError()
                except TimeoutError as exc:
                    raise OSError("socket") from exc
            except OSError as exc:
                raise RuntimeError("pool") from exc
        except RuntimeError as wrapped:
            assert classify_builtin(wrapped) is ErrorCategory.OTHER

    def test_anything_else_is_other(self) -> None:
        assert classify_builtin(ValueError("x")) is ErrorCategory.OTHER


class TestBaseDefault:
    def test_the_base_class_answers_other(self) -> None:
        result = BaseDataSource.classify_error(object(), ValueError("x"))  # type: ignore[arg-type]
        assert result is ErrorCategory.OTHER


def _pg(
    state: str,
    message: str,
    *,
    position: str | None = None,
    context: str | None = None,
    internal_query: str | None = None,
    internal_position: str | None = None,
) -> asyncpg_exc.PostgresError:
    """An asyncpg error built the way the driver builds one from the server's fields."""
    fields = {"C": state, "M": message}
    for key, value in (
        ("P", position),
        ("W", context),
        ("q", internal_query),
        ("p", internal_position),
    ):
        if value is not None:
            fields[key] = value
    return asyncpg_exc.PostgresError.new(fields)


class TestPostgreSQLClassifyError:
    def setup_method(self) -> None:
        self.adapter = PostgreSQLDataSource()

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_pg("42703", 'column "x" does not exist', position="8"), ErrorCategory.UNKNOWN_NAME),
            (_pg("42601", 'syntax error at or near "FROM"', position="13"), ErrorCategory.SYNTAX),
            (
                asyncpg_exc.InvalidTextRepresentationError(
                    'invalid input syntax for type integer: "Jane Doe"'
                ),
                ErrorCategory.DATA,
            ),
            (asyncpg_exc.InsufficientPrivilegeError("permission denied"), ErrorCategory.PERMISSION),
            (asyncpg_exc.QueryCanceledError("canceling statement"), ErrorCategory.TIMEOUT),
            (
                asyncpg_exc.ReadOnlySQLTransactionError("cannot execute SELECT INTO"),
                ErrorCategory.UNSUPPORTED,
            ),
        ],
    )
    def test_sqlstate_drives_the_category(self, exc: Exception, expected: ErrorCategory) -> None:
        assert self.adapter.classify_error(exc) is expected

    def test_the_driver_exposes_the_position_as_a_string_attribute(self) -> None:
        # asyncpg 0.30 maps the server's 'P' field onto ``.position`` (a str, or None).
        exc = _pg("42703", 'column "x" does not exist', position="8")
        assert exc.position == "8"
        assert asyncpg_exc.UndefinedColumnError("x").position is None

    def test_undefined_operator_is_a_type_mismatch(self) -> None:
        exc = _pg("42883", "operator does not exist: integer + text", position="10")
        assert self.adapter.classify_error(exc) is ErrorCategory.TYPE_MISMATCH

    def test_undefined_function_stays_unknown_name(self) -> None:
        exc = _pg("42883", "function date_format(timestamp) does not exist", position="8")
        assert self.adapter.classify_error(exc) is ErrorCategory.UNKNOWN_NAME

    def test_wrapped_driver_error_is_found_down_the_chain(self) -> None:
        try:
            try:
                raise _pg("42P01", 'relation "t" does not exist', position="15")
            except asyncpg_exc.PostgresError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            assert self.adapter.classify_error(wrapped) is ErrorCategory.UNKNOWN_NAME

    def test_foreign_exception_with_a_sqlstate_attribute_is_ignored(self) -> None:
        class ImpostorError(Exception):
            sqlstate = "42703"

        assert self.adapter.classify_error(ImpostorError("x")) is ErrorCategory.OTHER

    def test_plain_exception_is_other(self) -> None:
        assert self.adapter.classify_error(ValueError("x")) is ErrorCategory.OTHER


class TestPostgreSQLValueQuotingErrors:
    """A name-quoting SQLSTATE counts as a name error only with a position in the query.

    PostgreSQL reports the same SQLSTATEs for a value it read from a table and failed to
    look up or parse (``ssn::regclass`` -> ``relation "123456789" does not exist``). Those
    errors come from execution and carry no position; a genuine parse or analysis error
    points into the query.
    """

    def setup_method(self) -> None:
        self.adapter = PostgreSQLDataSource()

    @pytest.mark.parametrize(
        ("state", "message", "with_position"),
        [
            ("42703", 'column "nope" does not exist', ErrorCategory.UNKNOWN_NAME),
            ("42P01", 'relation "nope" does not exist', ErrorCategory.UNKNOWN_NAME),
            ("42883", "function nofunc(integer) does not exist", ErrorCategory.UNKNOWN_NAME),
            ("42883", "operator does not exist: integer + boolean", ErrorCategory.TYPE_MISMATCH),
            ("42704", 'type "nosuchtype" does not exist', ErrorCategory.UNKNOWN_NAME),
            ("3F000", 'schema "nosch" does not exist', ErrorCategory.UNKNOWN_NAME),
            ("3D000", 'database "nodb" does not exist', ErrorCategory.UNKNOWN_NAME),
            ("42702", 'column reference "id" is ambiguous', ErrorCategory.AMBIGUOUS_NAME),
            ("42725", "function amb(integer) is not unique", ErrorCategory.AMBIGUOUS_NAME),
            ("42P09", 'table reference "x" is ambiguous', ErrorCategory.AMBIGUOUS_NAME),
            ("42601", "syntax error at end of input", ErrorCategory.SYNTAX),
            ("42803", 'column "u.name" must appear in the GROUP BY clause', ErrorCategory.GROUPING),
            ("42804", "CASE types text and integer cannot be matched", ErrorCategory.TYPE_MISMATCH),
            ("42846", "cannot cast type integer to date", ErrorCategory.TYPE_MISMATCH),
            ("42P18", "cannot determine type of empty array", ErrorCategory.TYPE_MISMATCH),
        ],
    )
    def test_with_a_position_it_is_a_name_error_without_one_it_is_data(
        self, state: str, message: str, with_position: ErrorCategory
    ) -> None:
        assert self.adapter.classify_error(_pg(state, message, position="8")) is with_position
        assert self.adapter.classify_error(_pg(state, message)) is ErrorCategory.DATA
        assert self.adapter.classify_error(_pg(state, message, position="")) is ErrorCategory.DATA

    def test_a_value_parsed_as_a_type_name_is_data_despite_its_position(self) -> None:
        # regtype input runs the SQL parser on the stored value: the position points into
        # the value, and the server names the value in the error context.
        exc = _pg(
            "42601",
            'syntax error at or near "Doe"',
            position="6",
            context='invalid type name "Jane Doe"',
        )
        assert self.adapter.classify_error(exc) is ErrorCategory.DATA

    def test_an_error_inside_a_query_the_server_ran_for_a_function_is_data(self) -> None:
        # query_to_xml(stored_text): the failing SQL is the stored value itself.
        exc = _pg(
            "42601",
            'syntax error at or near "Jane"',
            internal_query="Jane Doe",
            internal_position="1",
        )
        assert self.adapter.classify_error(exc) is ErrorCategory.DATA

    @pytest.mark.parametrize("state", ["42501", "28000"])
    def test_a_permission_error_has_no_position_and_stays_a_permission_error(
        self, state: str
    ) -> None:
        # Measured on PostgreSQL 16: "permission denied for table hidden" carries no position.
        assert self.adapter.classify_error(_pg(state, "permission denied for table hidden")) is (
            ErrorCategory.PERMISSION
        )

    def test_a_permission_error_with_an_error_context_is_data(self) -> None:
        exc = _pg("42501", "permission denied for table t", context="SQL function f")
        assert self.adapter.classify_error(exc) is ErrorCategory.DATA

    def test_a_data_error_stays_data_with_or_without_a_position(self) -> None:
        msg = 'invalid input syntax for type integer: "Jane"'
        assert self.adapter.classify_error(_pg("22P02", msg)) is ErrorCategory.DATA
        assert self.adapter.classify_error(_pg("22P02", msg, position="8")) is ErrorCategory.DATA


# Each reproducer: the SQL the model wrote and the engine message PostgreSQL 16 returned
# for it (no position). The value is what must not reach the user or the model.
_PG_VALUE_REPRODUCERS = [
    (
        "SELECT to_tsquery(name) AS jane FROM u",
        "42601",
        'syntax error in tsquery: "Jane Doe 123-45-6789"',
        "Jane",
    ),
    (
        "SELECT name::tsquery AS jane FROM u",
        "42601",
        'syntax error in tsquery: "Jane Doe 123-45-6789"',
        "Jane",
    ),
    (
        "SELECT name::jsonpath FROM u",
        "42601",
        'syntax error at or near " " of jsonpath input',
        '" "',
    ),
    (
        "SELECT ssn::regclass FROM v",
        "42P01",
        'relation "123456789" does not exist',
        "123456789",
    ),
    (
        "SELECT fn::regproc AS jane_fn FROM v",
        "42883",
        'function "jane_fn" does not exist',
        "jane_fn",
    ),
    (
        "SELECT opname::regoper AS jane_op FROM v",
        "42883",
        "operator does not exist: jane_op",
        "jane_op",
    ),
    (
        "SELECT opname::regoper FROM v",
        "42725",
        "more than one operator named +",
        "+",
    ),
    (
        "SELECT current_setting(cfg) AS jane_cfg FROM v",
        "42704",
        'unrecognized configuration parameter "jane_cfg"',
        "jane_cfg",
    ),
    (
        "SELECT cfg::regconfig AS jane_cfg FROM v",
        "42704",
        'text search configuration "jane_cfg" does not exist',
        "jane_cfg",
    ),
    (
        "SELECT rolename::regrole AS alice_secret FROM v",
        "42704",
        'role "alice_secret" does not exist',
        "alice_secret",
    ),
    (
        "SELECT has_table_privilege(sch || '.x', 'SELECT') AS jane_schema FROM v",
        "3F000",
        'schema "jane_schema" does not exist',
        "jane_schema",
    ),
]


class TestPostgreSQLValueReproducers:
    """End to end: the adapter's category, then the user's text and the model's text."""

    @pytest.mark.parametrize(("sql", "state", "message", "value"), _PG_VALUE_REPRODUCERS)
    def test_the_value_reaches_neither_the_user_nor_the_model(
        self, sql: str, state: str, message: str, value: str
    ) -> None:
        from app.services.error_feedback import describe_error_for_llm
        from app.services.execution import _user_error_text

        adapter = PostgreSQLDataSource()
        exc = _pg(state, message)
        category = adapter.classify_error(exc)
        assert category is ErrorCategory.DATA
        assert value not in _user_error_text(adapter, exc)
        assert value not in describe_error_for_llm(category, message, sql=sql, schema=None)


class TestMySQLClassifyError:
    def setup_method(self) -> None:
        self.adapter = MySQLDataSource()

    @pytest.mark.parametrize(
        ("errno", "expected"),
        [
            (1054, ErrorCategory.UNKNOWN_NAME),  # ER_BAD_FIELD_ERROR
            (1146, ErrorCategory.UNKNOWN_NAME),  # ER_NO_SUCH_TABLE
            (1052, ErrorCategory.AMBIGUOUS_NAME),  # ER_NON_UNIQ_ERROR
            (1064, ErrorCategory.SYNTAX),  # ER_PARSE_ERROR
            (1055, ErrorCategory.GROUPING),  # ER_WRONG_FIELD_WITH_GROUP
            (1366, ErrorCategory.DATA),  # ER_TRUNCATED_WRONG_VALUE_FOR_FIELD
            (1365, ErrorCategory.DIVISION_BY_ZERO),
            (1242, ErrorCategory.CARDINALITY),  # ER_SUBQUERY_NO_1_ROW
            (1142, ErrorCategory.PERMISSION),  # ER_TABLEACCESS_DENIED_ERROR
            (1792, ErrorCategory.UNSUPPORTED),  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION
            (3024, ErrorCategory.TIMEOUT),  # ER_QUERY_TIMEOUT
            (1969, ErrorCategory.TIMEOUT),  # MariaDB ER_STATEMENT_TIMEOUT
            (1583, ErrorCategory.UNKNOWN_NAME),  # ER_WRONG_PARAMETERS_TO_NATIVE_FCT
            (1066, ErrorCategory.AMBIGUOUS_NAME),  # ER_NONUNIQ_TABLE
            (1248, ErrorCategory.SYNTAX),  # ER_DERIVED_MUST_HAVE_ALIAS
            (1222, ErrorCategory.SYNTAX),  # ER_WRONG_NUMBER_OF_COLUMNS_IN_SELECT
            (3065, ErrorCategory.GROUPING),  # ER_FIELD_IN_ORDER_NOT_SELECT
            (1227, ErrorCategory.PERMISSION),  # ER_SPECIFIC_ACCESS_DENIED_ERROR
            (1290, ErrorCategory.UNSUPPORTED),  # ER_OPTION_PREVENTS_STATEMENT
            (1317, ErrorCategory.OTHER),  # ER_QUERY_INTERRUPTED: also KILL QUERY, not mapped
            (9999, ErrorCategory.OTHER),
        ],
    )
    def test_errno_drives_the_category(self, errno: int, expected: ErrorCategory) -> None:
        exc = aiomysql.OperationalError(errno, "engine text that may quote 'Jane Doe'")
        assert self.adapter.classify_error(exc) is expected

    def test_programming_error_uses_the_same_map(self) -> None:
        exc = aiomysql.ProgrammingError(1064, "You have an error in your SQL syntax")
        assert self.adapter.classify_error(exc) is ErrorCategory.SYNTAX

    def test_foreign_exception_with_an_int_first_arg_is_ignored(self) -> None:
        assert self.adapter.classify_error(ValueError(1054, "x")) is ErrorCategory.OTHER

    def test_malformed_args_never_raise(self) -> None:
        assert self.adapter.classify_error(aiomysql.OperationalError()) is ErrorCategory.OTHER
