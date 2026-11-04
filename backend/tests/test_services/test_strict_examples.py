# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""``_strict_allowed``: few-shot examples the strict sensitive-column check lets into the
prompt, with the verdict memoised by (query, privacy settings, dialect)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.cache.example_library import ExampleEntry
from app.datasources.models import PrivacySettings
from app.services import pipeline
from app.services.pipeline import _strict_allowed

_STRICT = PrivacySettings(strict_sensitive_columns=True, sensitive_column_patterns=["email"])
_OK = "SELECT id FROM users"
_BAD = "SELECT id FROM users WHERE email = 'x'"


def _entry(example_id: str, query: str) -> ExampleEntry:
    return ExampleEntry(id=example_id, question="q", query=query, query_dialect="PostgreSQL")


@pytest.fixture(autouse=True)
def _fresh_memo():
    pipeline._STRICT_VERDICTS.clear()
    yield
    pipeline._STRICT_VERDICTS.clear()


class TestStrictAllowed:
    def test_off_returns_the_examples_unchecked(self):
        found = [_entry("a", _BAD)]
        with patch.object(pipeline, "check_strict_sensitive") as check:
            assert _strict_allowed(found, PrivacySettings(), "PostgreSQL") is found
            assert _strict_allowed(found, None, "PostgreSQL") is found
        check.assert_not_called()

    def test_an_example_the_check_refuses_is_left_out(self):
        ok, bad = _entry("a", _OK), _entry("b", _BAD)
        assert _strict_allowed([ok, bad], _STRICT, "PostgreSQL") == [ok]

    def test_a_verdict_is_computed_once_per_query_and_settings(self):
        found = [_entry("a", _OK), _entry("b", _BAD)]
        with patch.object(
            pipeline, "check_strict_sensitive", wraps=pipeline.check_strict_sensitive
        ) as check:
            first = _strict_allowed(found, _STRICT, "PostgreSQL")
            second = _strict_allowed(found, _STRICT, "PostgreSQL")
        assert [e.id for e in first] == [e.id for e in second] == ["a"]
        assert check.call_count == 2

    def test_a_change_to_the_settings_starts_afresh(self):
        found = [_entry("b", _BAD)]
        assert _strict_allowed(found, _STRICT, "PostgreSQL") == []
        relaxed = PrivacySettings(strict_sensitive_columns=True, sensitive_column_patterns=["ssn"])
        assert _strict_allowed(found, relaxed, "PostgreSQL") == found

    def test_the_dialect_is_part_of_the_key(self):
        found = [_entry("a", _OK)]
        _strict_allowed(found, _STRICT, "PostgreSQL")
        _strict_allowed(found, _STRICT, "MySQL")
        assert len(pipeline._STRICT_VERDICTS) == 2

    def test_a_crash_in_the_check_drops_the_example_and_is_not_memoised(self):
        found = [_entry("a", _OK)]
        with patch.object(pipeline, "check_strict_sensitive", side_effect=RuntimeError("boom")):
            assert _strict_allowed(found, _STRICT, "PostgreSQL") == []
        assert pipeline._STRICT_VERDICTS == {}
        assert _strict_allowed(found, _STRICT, "PostgreSQL") == found

    def test_the_memo_is_bounded(self):
        with patch.object(pipeline, "_STRICT_VERDICTS_MAX", 2):
            for i in range(5):
                query = f"SELECT {i} FROM users"  # noqa: S608 — test query, never executed
                _strict_allowed([_entry(str(i), query)], _STRICT, "PostgreSQL")
        assert len(pipeline._STRICT_VERDICTS) <= 2
