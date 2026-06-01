# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Unit tests for IntentClassifier — pure regex, zero I/O."""

from __future__ import annotations

import pytest

from app.services.intent_classifier import IntentClassifier, QueryIntent


@pytest.fixture
def clf() -> IntentClassifier:
    return IntentClassifier()


class TestIntentClassification:
    def setup_method(self) -> None:
        self.clf = IntentClassifier()

    def test_trend_intent(self) -> None:
        assert self.clf.classify("Show revenue over time") == QueryIntent.TREND

    def test_trend_monthly(self) -> None:
        assert self.clf.classify("Sales by month for 2024") == QueryIntent.TREND

    def test_ranking_intent(self) -> None:
        assert self.clf.classify("Top 10 customers by revenue") == QueryIntent.RANKING

    def test_ranking_highest(self) -> None:
        assert self.clf.classify("Which product has the highest margin?") == QueryIntent.RANKING

    def test_comparison_intent(self) -> None:
        assert self.clf.classify("Revenue vs last quarter") == QueryIntent.COMPARISON

    def test_comparison_versus(self) -> None:
        assert self.clf.classify("Compare this month versus last month") == QueryIntent.COMPARISON

    def test_count_intent(self) -> None:
        assert self.clf.classify("How many orders were placed today?") == QueryIntent.COUNT

    def test_count_number_of(self) -> None:
        assert self.clf.classify("Number of active users") == QueryIntent.COUNT

    def test_aggregation_intent(self) -> None:
        assert self.clf.classify("Total revenue this year") == QueryIntent.AGGREGATION

    def test_aggregation_average(self) -> None:
        # "by region" would trigger SEGMENTATION; use a pure aggregation phrase
        assert self.clf.classify("What is the average order value?") == QueryIntent.AGGREGATION

    def test_segmentation_intent(self) -> None:
        assert self.clf.classify("Breakdown of sales by region") == QueryIntent.SEGMENTATION

    def test_segmentation_by_category(self) -> None:
        assert self.clf.classify("Revenue by category") == QueryIntent.SEGMENTATION

    def test_existence_intent(self) -> None:
        assert self.clf.classify("Are there any overdue invoices?") == QueryIntent.EXISTENCE

    def test_existence_is_there(self) -> None:
        assert self.clf.classify("Is there any stock below reorder level?") == QueryIntent.EXISTENCE

    def test_lookup_intent(self) -> None:
        assert self.clf.classify("Show me all orders from customer 42") == QueryIntent.LOOKUP

    def test_lookup_find(self) -> None:
        assert self.clf.classify("Find the invoice for order 1234") == QueryIntent.LOOKUP

    def test_unknown_intent(self) -> None:
        assert self.clf.classify("zzz xyzzy completely unrecognised") == QueryIntent.UNKNOWN

    def test_case_insensitive(self) -> None:
        assert self.clf.classify("TREND OVER TIME") == QueryIntent.TREND
        assert self.clf.classify("TOP 5 products") == QueryIntent.RANKING

    def test_trend_priority_over_aggregation(self) -> None:
        # "total" matches AGGREGATION, "over time" matches TREND — TREND wins (higher priority)
        assert self.clf.classify("total revenue over time") == QueryIntent.TREND

    def test_ranking_priority_over_lookup(self) -> None:
        # "show me" matches LOOKUP, "top 5" matches RANKING — RANKING wins (higher priority)
        assert self.clf.classify("show me the top 5 customers") == QueryIntent.RANKING

    def test_count_priority_over_aggregation(self) -> None:
        # "how many" (COUNT) is checked before AGGREGATION in priority order
        assert self.clf.classify("how many total orders") == QueryIntent.COUNT


class TestGetIntentPromptHint:
    def setup_method(self) -> None:
        self.clf = IntentClassifier()

    def test_returns_str_for_all_defined_intents(self) -> None:
        defined = [
            QueryIntent.TREND,
            QueryIntent.RANKING,
            QueryIntent.COMPARISON,
            QueryIntent.AGGREGATION,
            QueryIntent.COUNT,
            QueryIntent.SEGMENTATION,
            QueryIntent.EXISTENCE,
            QueryIntent.LOOKUP,
        ]
        for intent in defined:
            hint = self.clf.get_intent_prompt_hint(intent)
            assert isinstance(hint, str), f"Expected str for {intent}"
            assert len(hint) > 0, f"Expected non-empty hint for {intent}"

    def test_unknown_intent_returns_empty_string(self) -> None:
        assert self.clf.get_intent_prompt_hint(QueryIntent.UNKNOWN) == ""
