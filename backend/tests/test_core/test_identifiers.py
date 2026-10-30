# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Identifier normalisation shared by the result mask and the error feedback."""

import pytest

from app.core.identifiers import ident_leaf, normalize_ident_path, strip_ident_quotes, unquote_ident


class TestUnquoteIdent:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ('"salary"', "salary"),
            ("`salary`", "salary"),
            ("[salary]", "salary"),
            ('"pay""rate"', 'pay"rate'),
            ("`pay``rate`", "pay`rate"),
            ("[a]]b]", "a]b"),
            ("salary", "salary"),
            ('"unterminated', '"unterminated'),
            ("public.orders", "public.orders"),
            ("x", "x"),
            ("", ""),
        ],
    )
    def test_one_surrounding_pair_is_removed(self, token: str, expected: str) -> None:
        assert unquote_ident(token) == expected


class TestStripIdentQuotes:
    def test_every_quote_character_goes(self) -> None:
        assert strip_ident_quotes('`public`."orders"') == "public.orders"


class TestPaths:
    def test_normalize_trims_each_segment(self) -> None:
        assert normalize_ident_path(" public . customers ") == "public.customers"

    def test_leaf_is_the_last_segment(self) -> None:
        assert ident_leaf("public.customers.email") == "email"
        assert ident_leaf("email") == "email"
