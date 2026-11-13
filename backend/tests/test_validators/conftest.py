# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared fixtures for the SQL validator tests."""

from collections.abc import Callable
import time

import pytest
import sqlparse

from app.datasources.validators.base_sql_validator import BaseSQLValidator


@pytest.fixture
def validate_seconds_without_sqlparse(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[BaseSQLValidator, str], float]:
    """Time ``validator.validate(query)`` with sqlparse's parse taken out of the measurement.

    The "validates quickly" tests guard the validator's own text scans against going
    quadratic. On their inputs sqlparse is most of the wall time — a run of ~10 000 tokens,
    just under its own DoS cap, costs it about half a second idle and well over a second on
    a loaded machine — so timing it too made those tests fail on machine load rather than on
    our code. ``sqlparse.parse`` is memoised, ``validate`` is run once untimed to fill the
    cache, and the second run is timed: everything the validator does is still measured,
    end to end, except the one third-party parse, which its token cap bounds anyway.
    """
    real_parse = sqlparse.parse
    cache: dict[str, tuple] = {}

    def memoised_parse(sql: str, *args: object, **kwargs: object) -> tuple:
        if sql not in cache:
            cache[sql] = real_parse(sql, *args, **kwargs)
        return cache[sql]

    monkeypatch.setattr(sqlparse, "parse", memoised_parse)

    def measure(validator: BaseSQLValidator, query: str) -> float:
        validator.validate(query)
        # sqlparse raises past its 10 000-token cap and validate() then refuses the query at
        # its first step, so a timing over such an input measures none of the validator's
        # own scans — the test would pass whatever they cost.
        assert cache, (
            "sqlparse.parse never returned for this input (over its token cap, or no longer "
            "called): validate() stopped before its own scans, so nothing of ours is timed"
        )
        start = time.monotonic()
        validator.validate(query)
        return time.monotonic() - start

    return measure
