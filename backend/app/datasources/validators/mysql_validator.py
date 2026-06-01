# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""MySQL / MariaDB-specific query validator."""

import re
from typing import ClassVar

from ..models import ValidationResult
from .base_sql_validator import BaseSQLValidator


class MySQLValidator(BaseSQLValidator):
    """MySQL/MariaDB-specific query validator extending the shared base."""

    MYSQL_DANGEROUS_FUNCTIONS: ClassVar[list[str]] = [
        "SLEEP",
        "BENCHMARK",
        "LOAD_FILE",
        "GET_LOCK",
        "RELEASE_LOCK",
        "MASTER_POS_WAIT",
        "SOURCE_POS_WAIT",
    ]

    MYSQL_DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"LOAD\s+DATA",
        r"SET\s+GLOBAL",
        r"SET\s+SESSION",
        r"FLUSH\s+",
    ]

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run base validation then add MySQL-specific checks."""
        result = super().validate(query, default_limit)
        if not result.is_valid:
            return result

        query_upper = query.upper()

        for func in self.MYSQL_DANGEROUS_FUNCTIONS:
            if re.search(rf"\b{func}\b", query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Query uses a blocked MySQL function: {func}",
                )

        for pattern in self.MYSQL_DANGEROUS_PATTERNS:
            if re.search(pattern, query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a blocked MySQL pattern",
                )

        return result
