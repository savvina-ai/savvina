# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

import re
from typing import ClassVar

import sqlparse.sql as sql_nodes
import sqlparse.tokens as sql_tokens

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
        # A value stored with set_config is read back with current_setting under another
        # column in the same statement, past result masking.
        "set_config",
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

    def _token_error(self, statements: tuple[sql_nodes.Statement, ...]) -> str | None:
        """Refuse a block comment nested in another.

        PostgreSQL nests block comments and sqlparse does not, so from the inner */ on the
        two disagree on what is code: the server reads /* a /* b */ SELECT 1 */ SET … as a
        comment and a SET, sqlparse as a SELECT. Every token check (the first word, INTO)
        relies on sqlparse's reading, so a query whose comments nest is refused: a block
        comment, as sqlparse reads it, that holds a second /*. Inside a string literal or a
        quoted name the characters are not a comment and pass.
        """
        nested = any(
            token.ttype in sql_tokens.Comment.Multiline and "/*" in token.value[2:]
            for statement in statements
            for token in statement.flatten()
        )
        if nested:
            return "Query nests a block comment inside another, which is not allowed"
        return None
