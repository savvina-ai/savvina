# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for SemanticModelGenerator — focused on build_time_expressions()."""

import pytest

from app.semantic.generator import SemanticModelGenerator


@pytest.fixture
def gen() -> SemanticModelGenerator:
    return SemanticModelGenerator()


# ── PostgreSQL ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dialect", ["sql", "postgresql"])
def test_pg_compatible_dialects(gen: SemanticModelGenerator, dialect: str) -> None:
    exprs = gen.build_time_expressions(dialect)
    assert exprs["today"] == "CURRENT_DATE"
    assert "DATE_TRUNC" in exprs["this_month"]
    assert "INTERVAL '1 month'" in exprs["last_month"]
    assert len(exprs) == 15


def test_pg_returns_copy(gen: SemanticModelGenerator) -> None:
    # Mutations to the returned dict must not affect the module-level constant
    a = gen.build_time_expressions("postgresql")
    a["today"] = "mutated"
    b = gen.build_time_expressions("postgresql")
    assert b["today"] == "CURRENT_DATE"


# ── MySQL ──────────────────────────────────────────────────────────────────────


def test_mysql(gen: SemanticModelGenerator) -> None:
    exprs = gen.build_time_expressions("mysql")
    assert exprs["today"] == "CURDATE()"
    assert "DATE_SUB" in exprs["yesterday"]
    assert "DATE_FORMAT" in exprs["this_month"]
    assert len(exprs) == 15


# ── Unknown dialect ────────────────────────────────────────────────────────────


def test_unknown_dialect_returns_empty(gen: SemanticModelGenerator) -> None:
    assert gen.build_time_expressions("unknown_db") == {}
    assert gen.build_time_expressions("") == {}
