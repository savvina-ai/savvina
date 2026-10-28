# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""_executable: the statement the database receives, or a rejection before it runs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.datasources.errors import ErrorCategory
from app.datasources.models import DataSourceSchema, PrivacySettings, QueryResult, ValidationResult
from app.providers.base import LLMResponse
from app.services.error_feedback import redact_error_for_user
from app.services.execution import (
    RowFilterRejectedError,
    _classify,
    _executable,
    _execute_auto_query,
)
from app.services.row_filter import _MIGRATION_MESSAGE
from app.services.row_filter_rewrite import apply_row_filters

_F = "tenant_id = 7"
_FILTERS = {"public.users": _F}


class TestExecutable:
    def test_no_privacy_passes_through(self) -> None:
        sql = "SELECT id FROM users"
        assert _executable(sql, None, "postgresql") is sql

    def test_no_filters_passes_through(self) -> None:
        sql = "SELECT id FROM users"
        assert _executable(sql, PrivacySettings(), "postgresql") is sql

    def test_filters_call_the_rewriter(self) -> None:
        out = _executable(
            "SELECT id FROM users", PrivacySettings(row_filters=_FILTERS), "postgresql"
        )
        assert "WHERE (tenant_id = 7)" in out
        assert out == apply_row_filters("SELECT id FROM users", _FILTERS, "postgresql")

    def test_legacy_filter_without_a_map_is_the_migration_state(self) -> None:
        privacy = PrivacySettings(row_filter_sql=_F)
        assert privacy.row_filter_needs_migration
        with pytest.raises(RowFilterRejectedError) as excinfo:
            _executable("SELECT id FROM users", privacy, "postgresql")
        assert str(excinfo.value) == _MIGRATION_MESSAGE
        assert excinfo.value.llm_hint is None

    def test_a_map_ends_the_migration_state_even_with_a_legacy_value(self) -> None:
        privacy = PrivacySettings(row_filter_sql=_F, row_filters=_FILTERS)
        assert not privacy.row_filter_needs_migration
        assert "WHERE (tenant_id = 7)" in _executable("SELECT id FROM users", privacy, "postgresql")

    def test_from_dict_loads_the_map_and_the_legacy_value(self) -> None:
        privacy = PrivacySettings.from_dict({"row_filters": _FILTERS, "row_filter_sql": _F})
        assert privacy.row_filters == _FILTERS
        assert privacy.row_filter_sql == _F
        assert PrivacySettings.from_dict({}).row_filters == {}


class TestMessages:
    def test_migration_message_survives_the_syntax_redaction(self) -> None:
        assert "'" not in _MIGRATION_MESSAGE and '"' not in _MIGRATION_MESSAGE
        assert redact_error_for_user(ErrorCategory.SYNTAX, _MIGRATION_MESSAGE) == _MIGRATION_MESSAGE

    def test_rejection_is_a_syntax_error(self) -> None:
        exc = RowFilterRejectedError("x")
        adapter = MagicMock()
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
        assert _classify(adapter, exc) is ErrorCategory.SYNTAX

    def test_user_error_text_is_verbatim_for_a_rejection(self) -> None:
        from app.services.execution import _user_error_text

        adapter = MagicMock()
        adapter.classify_error = MagicMock(return_value=ErrorCategory.OTHER)
        exc = RowFilterRejectedError("The admin's filter rejected it.")
        assert _user_error_text(adapter, exc) == "The admin's filter rejected it."

    def test_verbatim_flag_holds_even_if_the_category_were_redacting(self) -> None:
        from app.services.execution import _user_error_text

        exc = RowFilterRejectedError("The admin's filter rejected it.")
        with patch("app.services.execution._classify", return_value=ErrorCategory.DATA):
            shown = _user_error_text(MagicMock(), exc)
        assert shown == "The admin's filter rejected it."


class TestRejectionGoesThroughCorrection:
    async def test_rejected_statement_never_runs_and_the_correction_runs_rewritten(self) -> None:
        good = "SELECT id, tenant_id FROM users"
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(
            return_value=QueryResult(
                columns=["id", "tenant_id"],
                column_types=["int", "int"],
                rows=[[1, 7]],
                row_count=1,
                truncated=False,
                execution_time_ms=1.0,
            )
        )
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = MagicMock()
        provider.max_output_tokens = 4096
        provider.generate_response = AsyncMock(
            return_value=LLMResponse(
                query=good,
                explanation="fixed",
                raw_response=f"QUERY:\n```sql\n{good}\n```\nEXPLANATION: fixed",
                model="m",
            )
        )
        settings = MagicMock(default_row_limit=100, default_query_timeout=30)

        exe = await _execute_auto_query(
            generated_query="SELECT * FROM users WHERE (",
            explanation="e",
            adapter=adapter,
            config_dict={},
            connected=True,
            cache_hit=False,
            provider=provider,
            configured_model="m",
            configured_max_tokens=1024,
            system_prompt="",
            history=[],
            message="q",
            schema=DataSourceSchema(source_type="postgresql"),
            options={},
            settings=settings,
            privacy=PrivacySettings(row_filters=_FILTERS),
        )

        executed = [c.args[0] for c in adapter.execute_query.await_args_list]
        assert executed == [apply_row_filters(good, _FILTERS, "postgresql")]
        assert exe.status == "executed"
        assert exe.generated_query == good
        prompt = provider.generate_response.call_args.kwargs["user_message"]
        assert "syntax error" in prompt
        assert "The row-filter rewriter could not parse the query:" in prompt


class TestRejectionStateIsResetBetweenAttempts:
    async def test_engine_error_after_a_rejection_is_redacted_and_carries_no_old_hint(self) -> None:
        adapter = MagicMock()
        adapter.query_dialect = "postgresql"
        adapter.execute_query = AsyncMock(side_effect=RuntimeError("bad value 'secret' in row"))
        adapter.classify_error = MagicMock(return_value=ErrorCategory.DATA)
        adapter.validate_query = MagicMock(
            return_value=ValidationResult(is_valid=True, error_message=None)
        )
        provider = MagicMock()
        provider.max_output_tokens = 4096
        provider.generate_response = AsyncMock(
            side_effect=[
                LLMResponse(
                    query=q,
                    explanation="fixed",
                    raw_response=f"QUERY:\n```sql\n{q}\n```\nEXPLANATION: fixed",
                    model="m",
                )
                for q in ("SELECT id FROM public.users", "SELECT id FROM public.users LIMIT 5")
            ]
        )
        settings = MagicMock(default_row_limit=100, default_query_timeout=30)
        rejection = RowFilterRejectedError("first", llm_hint="HINT-A")
        calls = {"n": 0}

        def _first_rejected(sql: str, filters: dict[str, str], dialect: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise rejection
            return apply_row_filters(sql, filters, dialect)

        with patch("app.services.row_filter.apply_row_filters", side_effect=_first_rejected):
            exe = await _execute_auto_query(
                generated_query="SELECT id FROM users",
                explanation="e",
                adapter=adapter,
                config_dict={},
                connected=True,
                cache_hit=False,
                provider=provider,
                configured_model="m",
                configured_max_tokens=1024,
                system_prompt="",
                history=[],
                message="q",
                schema=DataSourceSchema(source_type="postgresql"),
                options={},
                settings=settings,
                privacy=PrivacySettings(row_filters=_FILTERS),
            )

        prompts = [c.kwargs["user_message"] for c in provider.generate_response.call_args_list]
        assert len(prompts) == 2
        assert "HINT-A" in prompts[0]
        assert "HINT-A" not in prompts[1]
        assert exe.status == "error"
        assert exe.error is not None
        assert "secret" not in exe.error


class TestRejectedErrorHint:
    def test_hint_defaults_to_none_and_is_kept_when_given(self) -> None:
        from app.services.row_filter_errors import RowFilterRejectedError as Err

        assert Err("plain").llm_hint is None
        exc = Err("plain", llm_hint="Qualify it.")
        assert str(exc) == "plain"
        assert exc.llm_hint == "Qualify it."

    def test_row_filter_module_re_exports_the_same_classes(self) -> None:
        from app.services import row_filter, row_filter_errors

        assert row_filter.RowFilterRejectedError is row_filter_errors.RowFilterRejectedError
        assert row_filter.RowFilterInvalidError is row_filter_errors.RowFilterInvalidError
