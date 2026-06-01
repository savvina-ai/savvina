# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

import re
from typing import ClassVar

from ..models import ValidationResult
from .base_sql_validator import BaseSQLValidator


class PostgreSQLValidator(BaseSQLValidator):
    """PostgreSQL-specific query validator extending the shared base."""

    PG_DANGEROUS_FUNCTIONS: ClassVar[list[str]] = [
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_export",
        "lo_import",
        "dblink",
        "dblink_exec",
    ]

    PG_DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"COPY\s+.*\s+TO",
        r"SET\s+ROLE",
        r"SET\s+SESSION",
    ]

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run base validation then add PostgreSQL-specific checks."""
        # 1. Run base validation
        result = super().validate(query, default_limit)
        if not result.is_valid:
            return result

        query_upper = query.upper()

        # 2. Check for dangerous PG functions
        for func in self.PG_DANGEROUS_FUNCTIONS:
            if re.search(rf"\b{func.upper()}\b", query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Query uses a blocked PostgreSQL function: {func}",
                )

        # 3. Check for PG-specific dangerous patterns
        for pattern in self.PG_DANGEROUS_PATTERNS:
            if re.search(pattern, query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a blocked PostgreSQL pattern",
                )

        return result
