# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Classifies the intent of a natural language query using keyword patterns."""

# OVERHEAD: app-only — pure regex matching, zero database or LLM calls

from __future__ import annotations

from enum import StrEnum
import re
from typing import ClassVar


class QueryIntent(StrEnum):
    AGGREGATION = "aggregation"  # totals, averages, sums, counts
    LOOKUP = "lookup"  # find specific records by criteria
    COMPARISON = "comparison"  # X vs Y, this period vs last
    TREND = "trend"  # over time, by month, weekly
    RANKING = "ranking"  # top N, bottom N, highest, lowest
    SEGMENTATION = "segmentation"  # breakdown by dimension, group by
    COUNT = "count"  # how many, count of
    EXISTENCE = "existence"  # does X exist, is there any
    UNKNOWN = "unknown"


class IntentClassifier:
    """
    Classifies the intent of a natural language query using keyword patterns.

    Runs entirely locally — no LLM, no database. ~0ms overhead.
    """

    _PATTERNS: ClassVar[dict[QueryIntent, list[str]]] = {
        QueryIntent.TREND: [
            r"\b(trend|over time|by month|monthly|weekly|by week|by day|daily"
            r"|by year|yearly|annual|timeline|progression|growth|decline"
            r"|change over|history of|historical)\b"
        ],
        QueryIntent.RANKING: [
            r"\b(top \d+|bottom \d+|highest|lowest|best|worst|most|least"
            r"|largest|smallest|biggest|first \d+|last \d+|rank)\b"
        ],
        QueryIntent.COMPARISON: [
            r"\b(vs\.?|versus|compared to|compare|difference between"
            r"|this .+ vs|last .+ vs|against|relative to)\b"
        ],
        QueryIntent.AGGREGATION: [
            r"\b(total|sum|average|avg|mean|revenue|sales|amount"
            r"|spend|expenditure|aggregate|overall|combined)\b"
        ],
        QueryIntent.COUNT: [
            r"\b(how many|count|number of|quantity of|volume of"
            r"|how much|tally)\b"
        ],
        QueryIntent.SEGMENTATION: [
            r"\b(by (country|region|category|department|type|status|channel"
            r"|segment|group)|breakdown|split|distribution|per |each )\b"
        ],
        QueryIntent.EXISTENCE: [r"\b(any|exist|is there|are there|have any|do we have)\b"],
        QueryIntent.LOOKUP: [
            r"\b(show me|find|get|fetch|list|display|what is|who is"
            r"|which|where is|details of|information about)\b"
        ],
    }

    # Priority order — TREND before AGGREGATION, RANKING before LOOKUP
    _PRIORITY: ClassVar[list[QueryIntent]] = [
        QueryIntent.TREND,
        QueryIntent.RANKING,
        QueryIntent.COMPARISON,
        QueryIntent.COUNT,
        QueryIntent.SEGMENTATION,
        QueryIntent.AGGREGATION,
        QueryIntent.EXISTENCE,
        QueryIntent.LOOKUP,
    ]

    def classify(self, question: str) -> QueryIntent:
        """Return the most likely intent for the given question."""
        q_lower = question.lower()
        for intent in self._PRIORITY:
            for pattern in self._PATTERNS.get(intent, []):
                if re.search(pattern, q_lower, re.IGNORECASE):
                    return intent
        return QueryIntent.UNKNOWN

    def get_intent_prompt_hint(self, intent: QueryIntent) -> str:
        """Return a short SQL pattern hint to append to the system prompt."""
        hints: dict[QueryIntent, str] = {
            QueryIntent.TREND: (
                "This is a TREND query. Group by time period using dialect-appropriate date "
                "functions (e.g. DATE_FORMAT for MySQL, DATE_TRUNC for PostgreSQL). "
                "Include the time column in SELECT and GROUP BY. ORDER BY time ASC. "
                "The time expression in GROUP BY must exactly match what you put in SELECT — "
                "e.g. if you SELECT DATE_TRUNC('month', col) you must GROUP BY "
                "DATE_TRUNC('month', col), not just col."
            ),
            QueryIntent.RANKING: (
                "This is a RANKING query. Use ORDER BY with DESC/ASC and LIMIT. "
                "Consider RANK() or ROW_NUMBER() window functions for dense rankings. "
                "Always backtick-quote the result alias: RANK() OVER (...) AS `rank`, "
                "ROW_NUMBER() OVER (...) AS `row_num` — RANK/ROW_NUMBER/DENSE_RANK are "
                "reserved keywords and cause syntax errors without backticks."
            ),
            QueryIntent.COMPARISON: (
                "This is a COMPARISON query. Use CTEs or subqueries to compute each "
                "value separately, then compare in the outer query. "
                "Each CTE must SELECT every column that downstream CTEs or the outer SELECT "
                "will reference — check CTE output columns before writing the next CTE."
            ),
            QueryIntent.AGGREGATION: (
                "This is an AGGREGATION query. Use SUM(), AVG(), COUNT() as appropriate. "
                "Apply any relevant WHERE filters before aggregating. "
                "Use HAVING (not WHERE) to filter on aggregated values — "
                "e.g. HAVING SUM(amount) > 1000."
            ),
            QueryIntent.COUNT: (
                "This is a COUNT query. Use COUNT(*) or COUNT(DISTINCT col). "
                "Apply filters first to count the right subset. "
                "COUNT(col) excludes NULLs; use COUNT(*) to count all rows including "
                "those with NULL in other columns."
            ),
            QueryIntent.SEGMENTATION: (
                "This is a SEGMENTATION query. GROUP BY the dimension column. "
                "Include both the dimension and the metric in SELECT. "
                "Add ORDER BY metric DESC to rank segments by size. "
                "Use HAVING to filter out negligibly small groups if needed."
            ),
            QueryIntent.EXISTENCE: (
                "This is an EXISTENCE query. Use EXISTS() or COUNT(*) > 0. "
                "If the user wants a yes/no answer, return a single row with a label: "
                "SELECT CASE WHEN COUNT(*) > 0 THEN 'Yes' ELSE 'No' END AS result."
            ),
            QueryIntent.LOOKUP: (
                "This is a LOOKUP query. Use WHERE to filter to specific records. "
                "SELECT the fields the user is asking about. "
                "If the question spans multiple entities (e.g. 'orders with customer name'), "
                "check the schema for a JOIN path rather than assuming the column is on "
                "the primary table."
            ),
        }
        return hints.get(intent, "")
