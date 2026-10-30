# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""_executable: the statement the database receives, or a rejection before it runs."""

from __future__ import annotations

import hashlib
import hmac
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.datasources.errors import ErrorCategory
from app.datasources.models import DataSourceSchema, PrivacySettings, QueryResult, ValidationResult
from app.providers.base import LLMResponse
from app.services.error_feedback import redact_error_for_user
from app.services.execution import (
    _classify,
    _execute_auto_query,
    _is_rejection,
)
from app.services.row_filter import (
    _MIGRATION_MESSAGE,
    RowFilterRejectedError,
    _executable,
    row_filter_fingerprint,
)
from app.services.row_filter_errors import QueryRejectedError, SensitiveColumnRejectedError
from app.services.row_filter_rewrite import apply_row_filters

_F = "tenant_id = 7"
_FILTERS = {"public.users": _F}
_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


# MySQL's default modes: "…" is a string, a backslash escapes. Both keywords are required.
_MODES = {"double_quote_is_string_literal": True, "backslash_is_escape": True}
_NO_BACKSLASH = {"double_quote_is_string_literal": True, "backslash_is_escape": False}
_ANSI = {"double_quote_is_string_literal": False, "backslash_is_escape": True}


class TestExecutableKeywords:
    def test_both_mode_keywords_are_required(self) -> None:
        import inspect

        params = inspect.signature(_executable).parameters
        for name in ("double_quote_is_string_literal", "backslash_is_escape"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[name].default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            _executable("SELECT 1", PrivacySettings(), "MySQL")  # type: ignore[call-arg]


class TestNoBackslashEscapes:
    """Under NO_BACKSLASH_ESCAPES the server ends a string at a backslash the parsers behind
    the privacy checks read as an escape, so the two disagree on where strings end."""

    # Masking parsed 'a\' AS k, email AS x FROM users -- ' as one string and masked nothing.
    _MASK_BYPASS = "SELECT 'a\\' AS k, email AS x FROM users -- '"
    # Strict mode read the WHERE on email as part of a string and let it through.
    _STRICT_BYPASS = "SELECT id, 'a\\' FROM users WHERE email LIKE '%gmail%' -- '"

    @pytest.mark.parametrize("sql", [_MASK_BYPASS, _STRICT_BYPASS])
    @pytest.mark.parametrize(
        "privacy",
        [
            None,
            PrivacySettings(),
            PrivacySettings(strict_sensitive_columns=True, sensitive_column_patterns=["email"]),
            PrivacySettings(row_filters={"shop.users": _F}),
        ],
    )
    def test_mysql_refuses_a_backslash_whatever_the_settings(
        self, sql: str, privacy: PrivacySettings | None
    ) -> None:
        with pytest.raises(QueryRejectedError) as info:
            _executable(sql, privacy, "MySQL", **_NO_BACKSLASH)
        assert "NO_BACKSLASH_ESCAPES" in str(info.value)
        assert info.value.llm_hint == "Do not use a backslash anywhere in the query."
        assert _is_rejection(info.value)
        # Shown verbatim: no apostrophe or quote character.
        assert not set("'\"`") & set(str(info.value) + info.value.llm_hint)

    def test_the_refusal_comes_before_the_strict_check(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=True, sensitive_column_patterns=["email"]
        )
        with (
            patch("app.services.row_filter.check_strict_sensitive") as strict,
            pytest.raises(QueryRejectedError),
        ):
            _executable(self._MASK_BYPASS, privacy, "MySQL", **_NO_BACKSLASH)
        strict.assert_not_called()

    def test_mysql_without_a_backslash_runs(self) -> None:
        sql = "SELECT 'a' AS k FROM users"
        assert _executable(sql, PrivacySettings(), "MySQL", **_NO_BACKSLASH) is sql

    def test_mysql_default_mode_lets_a_backslash_through(self) -> None:
        sql = "SELECT 'a\\'' AS k FROM users"
        assert _executable(sql, PrivacySettings(), "MySQL", **_MODES) is sql

    def test_postgresql_is_never_affected_by_the_flag(self) -> None:
        sql = "SELECT 'a\\' AS k, email AS x FROM users"
        assert _executable(sql, PrivacySettings(), "postgresql", **_NO_BACKSLASH) is sql


class TestExecutable:
    def test_mysql_under_ansi_quotes_refuses_double_quotes_on_the_sqlglot_paths(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=True, sensitive_column_patterns=["email"]
        )
        sql = "SELECT id FROM u WHERE \"email\" = 'x'"
        with pytest.raises(QueryRejectedError) as info:
            _executable(sql, privacy, "MySQL", **_ANSI)
        assert "ANSI_QUOTES" in str(info.value)
        assert "backticks" in info.value.llm_hint
        # Under MySQL's default reading "email" is a string and the strict check lets it through.
        assert _executable(sql, privacy, "MySQL", **_MODES) == sql

    def test_mysql_under_ansi_quotes_with_filters_refuses_double_quotes(self) -> None:
        privacy = PrivacySettings(row_filters={"shop.users": "tenant_id = 7"})
        with pytest.raises(QueryRejectedError):
            _executable('SELECT id FROM "users"', privacy, "MySQL", **_ANSI)

    def test_without_strict_mode_or_filters_double_quotes_pass_through(self) -> None:
        sql = 'SELECT "email" AS contact FROM u'
        assert _executable(sql, PrivacySettings(), "MySQL", **_ANSI) is sql

    def test_postgresql_is_never_affected_by_the_flag(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=True, sensitive_column_patterns=["email"]
        )
        sql = 'SELECT "id" FROM u'
        assert _executable(sql, privacy, "postgresql", **_ANSI) == sql

    def test_no_privacy_passes_through(self) -> None:
        sql = "SELECT id FROM users"
        assert _executable(sql, None, "postgresql", **_MODES) is sql

    def test_no_filters_passes_through(self) -> None:
        sql = "SELECT id FROM users"
        assert _executable(sql, PrivacySettings(), "postgresql", **_MODES) is sql

    def test_strict_mode_with_filters_parses_the_query_once(self) -> None:
        # The strict check parses; the rewriter must take that tree, not parse again.
        privacy = PrivacySettings(
            strict_sensitive_columns=True, row_filters=_FILTERS, sensitive_column_patterns=["email"]
        )
        with patch("app.services.row_filter_rewrite._parse_statement") as parse:
            out = _executable("SELECT email FROM users", privacy, "postgresql", **_MODES)
        assert "WHERE (tenant_id = 7)" in out
        parse.assert_not_called()

    @pytest.mark.parametrize(
        ("dialect", "key", "quote"),
        [("postgresql", "public.users", '"'), ("mysql", "shop.users", "`")],
    )
    @pytest.mark.parametrize(
        "template",
        [
            "WITH c AS (SELECT id FROM {u}) SELECT id FROM c",
            "SELECT a.id FROM {u} a JOIN {u} b ON a.id = b.id",
            "SELECT id FROM orders WHERE id IN (SELECT id FROM {u})",
            "SELECT id FROM {u} ORDER BY id LIMIT 10 OFFSET 5",
            "SELECT id, name FROM {u};",
            "SELECT id FROM {u} UNION ALL SELECT id FROM {u}",
        ],
    )
    def test_the_handed_over_tree_rewrites_as_a_fresh_parse(
        self, dialect: str, key: str, quote: str, template: str
    ) -> None:
        schema, table = key.split(".")
        sql = template.format(u=f"{quote}{schema}{quote}.{quote}{table}{quote}")
        filters = {key: _F}
        privacy = PrivacySettings(
            strict_sensitive_columns=True, row_filters=filters, sensitive_column_patterns=["email"]
        )
        out = _executable(sql, privacy, dialect, **_MODES)
        assert out == apply_row_filters(sql, filters, dialect)
        assert "tenant_id = 7" in out

    def test_without_strict_mode_the_rewriter_parses(self) -> None:
        from app.services import row_filter_rewrite

        with patch.object(
            row_filter_rewrite, "_parse_statement", wraps=row_filter_rewrite._parse_statement
        ) as parse:
            _executable(
                "SELECT id FROM users",
                PrivacySettings(row_filters=_FILTERS),
                "postgresql",
                **_MODES,
            )
        assert parse.call_count == 1

    def test_filters_call_the_rewriter(self) -> None:
        out = _executable(
            "SELECT id FROM users", PrivacySettings(row_filters=_FILTERS), "postgresql", **_MODES
        )
        assert "WHERE (tenant_id = 7)" in out
        assert out == apply_row_filters("SELECT id FROM users", _FILTERS, "postgresql")

    def test_strict_mode_rejects_before_the_rewrite(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=True, row_filters=_FILTERS, sensitive_column_patterns=["email"]
        )
        with pytest.raises(SensitiveColumnRejectedError) as info:
            _executable("SELECT id FROM users WHERE email = 'x'", privacy, "postgresql", **_MODES)
        assert _is_rejection(info.value)

    def test_strict_mode_passes_a_plain_projection_and_still_rewrites(self) -> None:
        privacy = PrivacySettings(
            strict_sensitive_columns=True, row_filters=_FILTERS, sensitive_column_patterns=["email"]
        )
        out = _executable("SELECT email FROM users", privacy, "postgresql", **_MODES)
        assert "WHERE (tenant_id = 7)" in out

    def test_strict_mode_checks_the_clean_query_not_the_rewritten_one(self) -> None:
        # The admin's filter names a sensitive-looking column in its WHERE; run after the
        # rewrite, the guard would reject the statement the admin's own filter produced.
        privacy = PrivacySettings(
            strict_sensitive_columns=True,
            sensitive_column_patterns=["email"],
            row_filters={"public.users": "owner_email = 'a@b.c'"},
        )
        out = _executable("SELECT id FROM users", privacy, "postgresql", **_MODES)
        assert "owner_email = 'a@b.c'" in out

    def test_the_migration_state_wins_over_strict(self) -> None:
        privacy = PrivacySettings(strict_sensitive_columns=True, row_filter_sql=_F)
        with pytest.raises(RowFilterRejectedError):
            _executable("SELECT id FROM users WHERE email = 'x'", privacy, "postgresql", **_MODES)

    def test_legacy_filter_without_a_map_is_the_migration_state(self) -> None:
        privacy = PrivacySettings(row_filter_sql=_F)
        assert privacy.row_filter_needs_migration
        with pytest.raises(RowFilterRejectedError) as excinfo:
            _executable("SELECT id FROM users", privacy, "postgresql", **_MODES)
        assert str(excinfo.value) == _MIGRATION_MESSAGE
        assert excinfo.value.llm_hint is None

    def test_a_map_ends_the_migration_state_even_with_a_legacy_value(self) -> None:
        privacy = PrivacySettings(row_filter_sql=_F, row_filters=_FILTERS)
        assert not privacy.row_filter_needs_migration
        assert "WHERE (tenant_id = 7)" in _executable(
            "SELECT id FROM users", privacy, "postgresql", **_MODES
        )

    def test_from_dict_loads_the_map_and_the_legacy_value(self) -> None:
        privacy = PrivacySettings.from_dict({"row_filters": _FILTERS, "row_filter_sql": _F})
        assert privacy.row_filters == _FILTERS
        assert privacy.row_filter_sql == _F
        assert PrivacySettings.from_dict({}).row_filters == {}


class TestMessages:
    def test_migration_message_is_kept_whole_as_a_rejection(self) -> None:
        assert "'" not in _MIGRATION_MESSAGE and '"' not in _MIGRATION_MESSAGE
        assert (
            redact_error_for_user(ErrorCategory.REJECTED, _MIGRATION_MESSAGE) == _MIGRATION_MESSAGE
        )

    def test_rejection_is_classified_rejected(self) -> None:
        exc = RowFilterRejectedError("x")
        adapter = MagicMock()
        adapter.classify_error = MagicMock(return_value=ErrorCategory.UNKNOWN_NAME)
        assert _classify(adapter, exc) is ErrorCategory.REJECTED

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
        assert "syntax error" not in prompt
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

        def _first_rejected(
            sql: str, filters: dict[str, str], dialect: str, **kwargs: object
        ) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise rejection
            return apply_row_filters(sql, filters, dialect, **kwargs)

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


class TestRowFilterFingerprint:
    def test_empty_map_is_the_empty_string(self) -> None:
        assert row_filter_fingerprint({}, _KEY) == ""

    def test_sixteen_hex_characters(self) -> None:
        fp = row_filter_fingerprint({"public.orders": "tenant_id = 7"}, _KEY)
        assert len(fp) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", fp)

    def test_stable_and_independent_of_key_order(self) -> None:
        a = row_filter_fingerprint({"public.a": "x = 1", "public.b": "y = 2"}, _KEY)
        b = row_filter_fingerprint({"public.b": "y = 2", "public.a": "x = 1"}, _KEY)
        assert a == b == row_filter_fingerprint({"public.a": "x = 1", "public.b": "y = 2"}, _KEY)

    def test_a_different_condition_or_table_changes_it(self) -> None:
        base = row_filter_fingerprint({"public.orders": "tenant_id = 7"}, _KEY)
        assert row_filter_fingerprint({"public.orders": "tenant_id = 8"}, _KEY) != base
        assert row_filter_fingerprint({"public.items": "tenant_id = 7"}, _KEY) != base

    def test_a_different_secret_changes_it(self) -> None:
        filters = {"public.orders": "tenant_id = 7"}
        assert row_filter_fingerprint(filters, _KEY) != row_filter_fingerprint(filters, "other")

    def test_canonical_form_and_domain_label_are_pinned(self) -> None:
        canonical = b'{"public.a":"x = 1","public.b":"y = 2"}'
        message = b"savvina.row_filter_fingerprint.v1\x00" + canonical
        expected = hmac.new(_KEY.encode(), message, hashlib.sha256).hexdigest()[:16]
        assert row_filter_fingerprint({"public.b": "y = 2", "public.a": "x = 1"}, _KEY) == expected

    def test_differs_from_a_plain_hmac_of_the_map(self) -> None:
        # The encryption key is shared with other uses; the label keeps this MAC from
        # colliding with any other HMAC-SHA256 computed over the same JSON under it.
        canonical = b'{"public.a":"x = 1"}'
        plain = hmac.new(_KEY.encode(), canonical, hashlib.sha256).hexdigest()[:16]
        assert row_filter_fingerprint({"public.a": "x = 1"}, _KEY) != plain

    def test_non_ascii_keys_and_conditions(self) -> None:
        fp = row_filter_fingerprint({"public.ü": "name = 'Ω'"}, _KEY)
        assert re.fullmatch(r"[0-9a-f]{16}", fp)
        assert fp != row_filter_fingerprint({"public.u": "name = 'Ω'"}, _KEY)
