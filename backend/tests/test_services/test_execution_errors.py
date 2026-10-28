# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for _classify() and _exec_error_text() in services.execution."""

from __future__ import annotations

import gc
from unittest.mock import AsyncMock, MagicMock
import warnings

from app.datasources.errors import ErrorCategory
from app.services.execution import _classify, _exec_error_text


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
