# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Normalisation for SQL identifiers, shared by the result mask and the LLM error feedback.

``app.core`` is a leaf package: nothing here may import ``app.datasources`` or
``app.services``, because ``app.datasources.__init__`` auto-imports every adapter.
"""

from __future__ import annotations


def strip_ident_quotes(value: str) -> str:
    """Remove backtick, double-quote and single-quote characters from a dotted identifier.

    ``` `public`.`orders` ``` and ``"public"."orders"`` both become ``public.orders``. The
    single quote is a string delimiter rather than an identifier quote; it is stripped too
    because an LLM sometimes quotes a name with it. ``[ ]`` is deliberately not handled:
    this runs over whole fragments, where ``[`` may be a subscript.
    """
    return value.replace("`", "").replace('"', "").replace("'", "")


_IDENT_DELIMITERS: dict[str, str] = {'"': '"', "`": "`", "[": "]"}


def unquote_ident(token: str) -> str:
    """Strip one surrounding pair of identifier delimiters from a single token.

    ``"salary"``, ``` `salary` ``` and ``[salary]`` all become ``salary``; a doubled
    closing delimiter inside is un-doubled (``"a""b"`` → ``a"b``), which is the SQL-standard
    and MySQL escape. Anything that is not one matched pair — a bare name, a dotted path, an
    unterminated quote — comes back as is.
    """
    if len(token) < 2:
        return token
    closing = _IDENT_DELIMITERS.get(token[0])
    if closing is None or token[-1] != closing:
        return token
    return token[1:-1].replace(closing * 2, closing)


def normalize_ident_path(value: str) -> str:
    """Strip whitespace around every dotted segment of *value*."""
    return ".".join(segment.strip() for segment in value.split("."))


def ident_leaf(value: str) -> str:
    """Return the last dotted segment — the bare table or column name."""
    return normalize_ident_path(value).rpartition(".")[2]
