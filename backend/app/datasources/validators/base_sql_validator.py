# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

import re
from typing import ClassVar

import sqlparse

from ..models import ValidationResult


class BaseSQLValidator:
    """Shared SQL safety validation logic for all SQL-based adapters."""

    BLOCKED_STATEMENT_TYPES: ClassVar[set[str]] = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "MERGE",
        "REPLACE",
        "CALL",
        "EXECUTE",
        "EXEC",
    }

    DANGEROUS_PATTERNS: ClassVar[list[str]] = [
        r"INTO\s+OUTFILE",
        r"INTO\s+DUMPFILE",
    ]

    MULTI_STATEMENT_PATTERN: ClassVar[str] = r";\s*\w"

    def validate(self, query: str, default_limit: int = 1000) -> ValidationResult:
        """Run shared validation: parse, check type, check patterns, add LIMIT.

        Steps:
        1. Parse with sqlparse (type detection only — not statement count)
        2. Check statement type is SELECT or WITH (CTE)
        3. Scan for BLOCKED_STATEMENT_TYPES as whole words in query text
        4. Regex scan for multi-statement injection and DANGEROUS_PATTERNS
        5. Add LIMIT if missing
        6. Return ValidationResult
        """
        query = query.strip()
        if not query:
            return ValidationResult(is_valid=False, error_message="Empty query")

        # Normalise missing whitespace after keywords (e.g. "SELECTday" → "SELECT day")
        query = re.sub(r"(?i)^(SELECT|WITH|INSERT|UPDATE|DELETE)(?=[^\s(])", r"\1 ", query)

        # 1. Parse — use sqlparse only for statement-type detection, not statement count.
        # Counting parsed statements to reject "multiple statements" is unreliable:
        # sqlparse misclassifies subqueries inside JOIN (...) as separate statements.
        # Multi-statement injection is caught accurately by the `;\s*\w` pattern below.
        parsed = sqlparse.parse(query)
        non_empty = [s for s in parsed if str(s).strip()]

        if not non_empty:
            return ValidationResult(is_valid=False, error_message="Empty query")

        stmt = non_empty[0]
        stmt_type = stmt.get_type()  # Returns 'SELECT', 'INSERT', etc., or None
        query_upper = query.upper().strip()

        # 3. Check statement type — allow SELECT and CTEs (WITH)
        is_select = stmt_type == "SELECT"
        is_cte = query_upper.startswith("WITH")
        # sqlparse 0.5.3 returns 'UNKNOWN' (not None) for statements it cannot classify,
        # including parenthesized SELECT (first token is '(' not a DML keyword). Allow these —
        # blocked DML keywords are still caught by the keyword scan in step 4.
        is_compound = stmt_type in (None, "UNKNOWN") and bool(re.search(r"\bSELECT\b", query_upper))

        if not is_select and not is_cte and not is_compound:
            return ValidationResult(
                is_valid=False,
                error_message=(
                    f"Only SELECT queries and CTEs (WITH) are allowed. "
                    f"Got: {stmt_type or 'UNKNOWN'}"
                ),
            )

        # 4. Scan for blocked keywords as whole words
        #    We scan the full text to catch attempts to embed DML inside comments
        #    or disguise them. Accept that quoted identifiers named after keywords
        #    (e.g., "delete") will also be rejected — this is an intentional
        #    safety-first tradeoff for an MVP.
        for blocked in self.BLOCKED_STATEMENT_TYPES:
            if re.search(rf"\b{blocked}\b", query_upper):
                return ValidationResult(
                    is_valid=False,
                    error_message=f"Query contains blocked keyword: {blocked}",
                )

        # 5. Regex scan for dangerous patterns
        if re.search(self.MULTI_STATEMENT_PATTERN, query, re.IGNORECASE):
            return ValidationResult(
                is_valid=False,
                error_message="Multiple statements are not allowed",
            )
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                return ValidationResult(
                    is_valid=False,
                    error_message="Query contains a dangerous pattern",
                )

        # 6. Add LIMIT if missing
        sanitized = query
        if not re.search(r"\bLIMIT\b", query, re.IGNORECASE):
            sanitized = query.rstrip(";").rstrip()
            sanitized = f"{sanitized}\nLIMIT {default_limit}"

        return ValidationResult(is_valid=True, sanitized_query=sanitized)
