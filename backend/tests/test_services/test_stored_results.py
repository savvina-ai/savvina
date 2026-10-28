# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Stored results are re-masked on read with the connection's current privacy settings."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from app.datasources.models import PrivacySettings
from app.services import result_masking, stored_results
from app.services.stored_results import (
    StoredResultMasks,
    StoredResultPrivacy,
    load_result_masks,
    served_query,
    stored_result_privacy,
)

if TYPE_CHECKING:
    import pytest

_R = "[REDACTED]"


def _results(columns: list[str], rows: list[list]) -> dict:
    return {
        "columns": columns,
        "column_types": ["text"] * len(columns),
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "execution_time_ms": 1.0,
        "bytes_scanned": None,
    }


_DEFAULT = StoredResultPrivacy(PrivacySettings())


class TestMask:
    def test_unmasked_email_column_comes_back_masked(self) -> None:
        stored = _results(["id", "email"], [[1, "a@x.io"], [2, "b@x.io"]])
        out = _DEFAULT.mask(stored, "SELECT id, email FROM users")
        assert out["rows"] == [[1, _R], [2, _R]]
        assert stored["rows"] == [[1, "a@x.io"], [2, "b@x.io"]]  # stored dict untouched

    def test_shape_is_unchanged(self) -> None:
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        stored["extra"] = "kept"
        out = _DEFAULT.mask(stored, "SELECT id, email FROM users")
        assert set(out) == set(stored)
        assert {k: v for k, v in out.items() if k != "rows"} == {
            k: v for k, v in stored.items() if k != "rows"
        }

    def test_alias_is_classified_from_the_sql(self) -> None:
        stored = _results(["id", "contact"], [[1, "a@x.io"]])
        out = _DEFAULT.mask(stored, "SELECT id, email AS contact FROM users")
        assert out["rows"] == [[1, _R]]

    def test_wrapped_sql_is_classified_through_the_wrap(self) -> None:
        stored = _results(["id", "contact", "tenant_id"], [[1, "a@x.io", 7]])
        wrapped = (
            "SELECT * FROM (SELECT id, email AS contact, tenant_id FROM users)"
            " AS _sift_q WHERE tenant_id = 7"
        )
        out = _DEFAULT.mask(stored, wrapped)
        assert out["rows"] == [[1, _R, 7]]

    def test_masking_is_idempotent(self) -> None:
        stored = _results(["id", "email"], [[1, _R]])
        out = _DEFAULT.mask(stored, "SELECT id, email FROM users")
        assert out["rows"] == [[1, _R]]

    def test_current_settings_apply(self) -> None:
        privacy = PrivacySettings(sensitive_column_patterns=["nickname"])
        stored = _results(["email", "nickname"], [["a@x.io", "Al"]])
        out = StoredResultPrivacy(privacy).mask(stored, "SELECT email, nickname FROM users")
        assert out["rows"] == [["a@x.io", _R]]

    def test_excluded_column_is_masked(self) -> None:
        privacy = PrivacySettings(excluded_columns=["public.users.notes"])
        stored = _results(["id", "notes"], [[1, "secret"]])
        out = StoredResultPrivacy(privacy).mask(stored, "SELECT id, notes FROM users")
        assert out["rows"] == [[1, _R]]

    def test_no_sql_masks_by_name(self) -> None:
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        assert _DEFAULT.mask(stored, None)["rows"] == [[1, _R]]

    def test_unclassifiable_sql_naming_a_sensitive_column_masks_every_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(result_masking, "_classify_output_items", boom)
        stored = _results(["x", "id"], [["a@x.io", 1]])
        assert _DEFAULT.mask(stored, "SELECT email AS x, id FROM users")["rows"] == [[_R, _R]]
        # Not naming one: name matching alone.
        stored = _results(["x", "id"], [["Al", 1]])
        assert _DEFAULT.mask(stored, "SELECT name AS x, id FROM users")["rows"] == [["Al", 1]]

    def test_nothing_sensitive_returns_the_stored_rows(self) -> None:
        stored = _results(["id", "name"], [[1, "Al"]])
        assert _DEFAULT.mask(stored, "SELECT id, name FROM users")["rows"] == [[1, "Al"]]

    def test_none_and_empty_pass_through(self) -> None:
        assert _DEFAULT.mask(None, "SELECT 1") is None
        assert _DEFAULT.mask({}, "SELECT 1") == {}

    def test_malformed_rows_fail_closed(self) -> None:
        stored = {"columns": ["id"], "rows": "not-a-list"}
        assert _DEFAULT.mask(stored, "SELECT id FROM t")["rows"] == []
        stored = {"columns": "id", "rows": [[1, 2]]}
        assert _DEFAULT.mask(stored, "SELECT id FROM t")["rows"] == [[_R, _R]]
        stored = {"columns": ["id", "email"], "rows": [{"id": 1}]}
        assert _DEFAULT.mask(stored, "SELECT id FROM t")["rows"] == [[_R, _R]]


class TestStoredResultPrivacy:
    def test_mysql_reads_double_quotes_as_strings(self) -> None:
        ctx = stored_result_privacy({"sensitive_column_patterns": ["email"]}, "mysql")
        assert ctx.double_quote_is_string_literal is True
        # In MySQL "email" is a string literal, not the column.
        stored = _results(["id", "label"], [[1, "email"]])
        out = ctx.mask(stored, 'SELECT id, "email" AS label FROM users')
        assert out["rows"] == [[1, "email"]]

    def test_postgres_reads_double_quotes_as_identifiers(self) -> None:
        ctx = stored_result_privacy(None, "postgresql")
        assert ctx.double_quote_is_string_literal is False
        stored = _results(["id", "label"], [[1, "a@x.io"]])
        assert ctx.mask(stored, 'SELECT id, "email" AS label FROM users')["rows"] == [[1, _R]]

    def test_no_stored_settings_uses_defaults(self) -> None:
        assert stored_result_privacy(None, "postgresql").privacy == PrivacySettings()
        assert stored_result_privacy({}, "postgresql").privacy == PrivacySettings()

    def test_unknown_or_missing_source_type_overmasks(self) -> None:
        assert stored_result_privacy(None, None).double_quote_is_string_literal is False
        assert stored_result_privacy(None, "nope").double_quote_is_string_literal is False

    def test_unregistered_source_type_masks_every_column(self) -> None:
        # get_datasource_class raises ValueError: the dialect, and so the lexing, is unknown.
        ctx = stored_result_privacy({"sensitive_column_patterns": ["email"]}, "nope")
        stored = _results(["id", "name"], [[1, "Al"], [2, "Bo"]])
        assert ctx.mask(stored, "SELECT id, name FROM users")["rows"] == [[_R, _R], [_R, _R]]


def _db_returning(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestLoadResultMasks:
    async def test_one_query_for_every_session(self) -> None:
        db = _db_returning(
            [
                ("s1", {"sensitive_column_patterns": ["nickname"]}, "postgresql"),
                ("s2", None, "mysql"),
            ]
        )
        masks = await load_result_masks(db, ["s1", "s2", "s1"])
        assert db.execute.await_count == 1
        stored = _results(["email", "nickname"], [["a@x.io", "Al"]])
        sql = "SELECT email, nickname FROM users"
        assert masks.mask("s1", stored, sql)["rows"] == [["a@x.io", _R]]
        assert masks.mask("s2", stored, sql)["rows"] == [[_R, "Al"]]

    async def test_missing_connection_masks_with_defaults(self) -> None:
        db = _db_returning([("s1", None, None)])
        masks = await load_result_masks(db, ["s1"])
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        assert masks.mask("s1", stored, "SELECT id, email FROM u")["rows"] == [[1, _R]]
        # A session the query did not return at all is masked with defaults too.
        assert masks.mask("gone", stored, "SELECT id, email FROM u")["rows"] == [[1, _R]]

    async def test_no_sessions_no_query(self) -> None:
        db = _db_returning([])
        masks = await load_result_masks(db, [])
        assert db.execute.await_count == 0
        stored = _results(["email"], [["a@x.io"]])
        assert masks.mask("x", stored, None)["rows"] == [[_R]]


class TestServeOncePerRequest:
    """A request re-masks many messages: the plan is built once per (session, query), each
    stored query is unwrapped once, and the batch runs off the event loop."""

    _SQL = "SELECT id, email FROM users"

    @staticmethod
    def _masks() -> StoredResultMasks:
        return StoredResultMasks({"s1": StoredResultPrivacy(PrivacySettings())})

    def test_serve_returns_the_masked_results_and_the_served_query(self) -> None:
        wrapped = f"SELECT * FROM (\n{self._SQL}\n) AS _sift_q WHERE tenant_id = 7 LIMIT 5"  # noqa: S608
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        masked, served = self._masks().serve("s1", stored, wrapped)
        assert masked["rows"] == [[1, _R]]
        assert served == f"{self._SQL} LIMIT 5"
        assert served == served_query(wrapped)

    def test_serve_matches_mask_and_served_query(self) -> None:
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        masks = self._masks()
        assert masks.serve("s1", stored, self._SQL) == (
            masks.mask("s1", stored, self._SQL),
            served_query(self._SQL),
        )
        assert masks.serve("s1", None, None) == (None, None)

    def test_one_plan_per_session_and_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str | None] = []
        real = stored_results._plan_result_mask

        def counting(*args: object, **kwargs: object) -> object:
            calls.append(args[1])  # type: ignore[arg-type]
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(stored_results, "_plan_result_mask", counting)
        masks = StoredResultMasks(
            {"s1": StoredResultPrivacy(PrivacySettings()), "s2": StoredResultPrivacy()}
        )
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        for _ in range(3):
            assert masks.mask("s1", stored, self._SQL)["rows"] == [[1, _R]]
        masks.mask("s1", stored, "SELECT email FROM users")
        masks.mask("s2", stored, self._SQL)
        assert len(calls) == 3

    def test_each_stored_query_is_unwrapped_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        real = stored_results.unwrap_row_filter

        def counting(sql: str) -> str:
            calls.append(sql)
            return real(sql)

        monkeypatch.setattr(stored_results, "unwrap_row_filter", counting)
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        masks = self._masks()
        masks.serve("s1", stored, self._SQL)
        masks.serve("s1", stored, self._SQL)
        assert calls == [self._SQL]

    async def test_serve_all_runs_off_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        threads: list[int] = []
        real = stored_results._plan_result_mask

        def recording(*args: object, **kwargs: object) -> object:
            threads.append(threading.get_ident())
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(stored_results, "_plan_result_mask", recording)
        stored = _results(["id", "email"], [[1, "a@x.io"]])
        out = await self._masks().serve_all(
            [("s1", stored, self._SQL), ("gone", None, None), ("s1", stored, self._SQL)]
        )
        assert out == [
            ({**stored, "rows": [[1, _R]]}, self._SQL),
            (None, None),
            ({**stored, "rows": [[1, _R]]}, self._SQL),
        ]
        assert threads
        assert threading.get_ident() not in threads

    async def test_serve_all_of_nothing_is_empty(self) -> None:
        assert await self._masks().serve_all([]) == []
