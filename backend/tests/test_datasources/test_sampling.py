# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""An ENUM/SET member list is a column's value domain and follows the sample-values decision."""

import time

import pytest

from app.datasources.sampling import withhold_enum_labels


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("enum('pending','shipped','delivered')", "enum(…)"),
        ("ENUM('a','b')", "ENUM(…)"),
        ("set('read','write')", "set(…)"),
        ("enum('it''s','a\\'b')", "enum(…)"),  # escaped quotes inside a member
        ("enum('a','b') NOT NULL", "enum(…) NOT NULL"),
        ("varchar(255)", "varchar(255)"),
        ("int", "int"),
        ("", ""),
    ],
)
def test_member_lists_are_replaced(native_type: str, expected: str) -> None:
    assert withhold_enum_labels(native_type) == expected


def test_unbalanced_declaration_fails_closed() -> None:
    # No closing paren (a truncated cache row): nothing after the opener may render.
    assert withhold_enum_labels("enum('a','b'") == "enum(…)"


def test_pathological_quote_run_returns_quickly() -> None:
    # 24 pairs: microseconds with the possessive pattern, about 3 s with a greedy one
    # (it costs ~3.5x more per extra pair; 0.25 s at 22). Large enough to fail fast on a
    # regression, small enough that the regression does not hang the suite.
    start = time.perf_counter()
    withhold_enum_labels("enum(" + "''" * 24)
    assert time.perf_counter() - start < 1.0

    start = time.perf_counter()
    withhold_enum_labels("enum(" + "''" * 500)
    assert time.perf_counter() - start < 1.0
