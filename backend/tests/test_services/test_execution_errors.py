# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for the error helpers in services.execution: _classify(), _exec_error_text(),
the rejection helpers _is_rejection() and _llm_hint(), and the logging of
_user_facing_error()."""

from __future__ import annotations

import gc
import logging
from unittest.mock import AsyncMock, MagicMock
import warnings

import pytest

from app.datasources.errors import ErrorCategory
from app.services.execution import (
    _classify,
    _correctable,
    _exec_error_text,
    _is_rejection,
    _llm_hint,
    _user_facing_error,
)
from app.services.row_filter_errors import (
    QueryRejectedError,
    RowFilterRejectedError,
    SensitiveColumnRejectedError,
)


class _Hook:
    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises

    def classify_error(self, exc: BaseException) -> object:
        if self._raises:
            raise self._raises
        return self._result


class TestClassify:
    def test_real_category_from_the_hook_is_returned_unchanged(self) -> None:
        adapter = _Hook(ErrorCategory.SYNTAX)
        assert _classify(adapter, ValueError("x")) is ErrorCategory.SYNTAX

    def test_hook_category_wins_over_the_builtin_timeout_fallback(self) -> None:
        adapter = _Hook(ErrorCategory.PERMISSION)
        assert _classify(adapter, TimeoutError()) is ErrorCategory.PERMISSION

    def test_hook_raising_reads_other(self) -> None:
        adapter = _Hook(raises=RuntimeError("boom"))
        assert _classify(adapter, ValueError("x")) is ErrorCategory.OTHER

    def test_hook_raising_still_gets_the_builtin_timeout_fallback(self) -> None:
        adapter = _Hook(raises=RuntimeError("boom"))
        assert _classify(adapter, TimeoutError()) is ErrorCategory.TIMEOUT

    def test_non_enum_result_reads_other(self) -> None:
        adapter = MagicMock()
        adapter.classify_error.return_value = MagicMock()
        assert _classify(adapter, ValueError("x")) is ErrorCategory.OTHER

    def test_an_adapter_answering_rejected_reads_other(self) -> None:
        # REJECTED is reserved for QueryRejectedError, whose text is ours and reaches the
        # LLM and the user unredacted; an engine error must never be read that way.
        adapter = MagicMock()
        adapter.classify_error.return_value = ErrorCategory.REJECTED
        assert _classify(adapter, ValueError("x")) is ErrorCategory.OTHER

    def test_an_adapter_answering_rejected_still_gets_the_builtin_timeout_fallback(
        self,
    ) -> None:
        adapter = _Hook(ErrorCategory.REJECTED)
        assert _classify(adapter, TimeoutError()) is ErrorCategory.TIMEOUT

    def test_adapter_without_the_hook_reads_other(self) -> None:
        assert _classify(object(), ValueError("x")) is ErrorCategory.OTHER

    def test_async_mock_adapter_reads_other_without_a_never_awaited_warning(self) -> None:
        adapter = AsyncMock()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _classify(adapter, ValueError("x"))
            gc.collect()
        assert result is ErrorCategory.OTHER
        assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


class TestExecErrorText:
    def test_non_empty_text_is_returned_as_is(self) -> None:
        assert _exec_error_text(ValueError("bad thing")) == "bad thing"

    def test_empty_timeout_gets_a_timeout_message(self) -> None:
        assert _exec_error_text(TimeoutError()) == "Query timed out"

    def test_other_empty_exception_names_its_type(self) -> None:
        assert _exec_error_text(ValueError()) == "Query failed (ValueError)"


class TestUserFacingErrorLogging:
    def test_the_log_holds_the_redacted_text_only(self, caplog) -> None:
        raw = 'invalid input syntax for type integer: "secret-value"\nDETAIL: more secret-value'
        with caplog.at_level(logging.DEBUG, logger="app.services.execution"):
            shown = _user_facing_error(raw, ErrorCategory.DATA)
        assert "secret-value" not in shown
        assert "secret-value" not in caplog.text
        assert "[REDACTED]" in caplog.text
        assert "Execution failed (data): " in caplog.text

    def test_a_verbatim_rejection_is_logged_as_shown(self, caplog) -> None:
        with caplog.at_level(logging.DEBUG, logger="app.services.execution"):
            _user_facing_error(
                "Row filter 'x' needs migration\nmore", ErrorCategory.DATA, verbatim=True
            )
        assert "Row filter 'x' needs migration\nmore" in caplog.text


class TestRejectionBase:
    @pytest.mark.parametrize(
        ("exc", "expected_hint"),
        [
            pytest.param(RowFilterRejectedError("row", llm_hint="h1"), "h1", id="row_filter"),
            pytest.param(
                SensitiveColumnRejectedError("sensitive", llm_hint="h2"), "h2", id="sensitive"
            ),
        ],
    )
    def test_every_rejection_is_verbatim_and_rejected_with_its_hint(
        self, exc, expected_hint
    ) -> None:
        assert isinstance(exc, QueryRejectedError)
        assert _is_rejection(exc) is True
        assert _llm_hint(exc) == expected_hint
        assert _classify(object(), exc) is ErrorCategory.REJECTED

    def test_a_plain_error_is_not_a_rejection(self) -> None:
        assert _is_rejection(ValueError("x")) is False
        assert _llm_hint(ValueError("x")) is None


class TestCorrectable:
    @pytest.mark.parametrize(
        ("rejected", "hint", "expected"),
        [
            pytest.param(False, None, True, id="engine-error"),
            pytest.param(True, "Qualify it.", True, id="rejection-with-hint"),
            pytest.param(True, None, False, id="rejection-without-hint"),
            pytest.param(True, "", False, id="rejection-with-empty-hint"),
        ],
    )
    def test_only_a_rejection_without_a_hint_ends_the_loop(
        self, rejected: bool, hint: str | None, expected: bool
    ) -> None:
        assert _correctable(rejected, hint) is expected
