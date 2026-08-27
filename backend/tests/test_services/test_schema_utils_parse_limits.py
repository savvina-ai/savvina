# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Table extraction must degrade, not raise, when sqlparse refuses to parse."""

from __future__ import annotations

from app.services.schema_utils import _check_query_complexity, _extract_tables_from_sql


class TestExtractTablesFailsSoft:
    """sqlparse >= 0.5.5 raises SQLParseError when its DoS guards trip.

    Both callers are advisory — the audit log's ``tables_accessed`` and the
    large-table full-scan check — and both run inside a chat turn on SQL that
    ``validate_query()`` already accepted. Raising here would fail the turn over
    metadata nobody asked for, so extraction returns "no tables known" instead.
    """

    def test_normal_query_still_extracts(self):
        assert _extract_tables_from_sql("SELECT id FROM public.orders") == ["orders"]

    def test_grouping_depth_limit_returns_empty(self):
        query = "SELECT " + "(" * 300 + "1" + ")" * 300 + " FROM orders"
        assert _extract_tables_from_sql(query) == []

    def test_token_limit_returns_empty(self):
        ids = ",".join(str(i) for i in range(20000))
        query = f"SELECT * FROM orders WHERE x IN ({ids})"  # noqa: S608
        assert _extract_tables_from_sql(query) == []

    def test_complexity_check_survives_unparseable_sql(self):
        query = "SELECT " + "(" * 300 + "1" + ")" * 300 + " FROM orders"
        assert _check_query_complexity(query) is None
