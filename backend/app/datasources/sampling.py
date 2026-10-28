# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Value-withholding helpers shared by introspection and the cached-schema privacy filter."""

from __future__ import annotations

import re

# A MySQL ENUM/SET declaration. Its member list is the column's value domain — the same
# thing introspection reads as sample values — so it follows the same decision.
#
# Both repetitions are possessive (`*+`): a malformed run of quotes with no closing paren
# gives a greedy form exponentially many partitions to try before giving up (classic
# ReDoS). Possessive quantifiers never backtrack, so the match is linear in the input.
_ENUM_DECL_RE = re.compile(r"\b(enum|set)\s*\((?:'(?:[^'\\]|\\.|'')*+'|[^()'])*+\)", re.IGNORECASE)

# Only the opening `name(` — used after the substitution above to find a declaration it
# did not consume: an unbalanced or truncated one.
_ENUM_START_RE = re.compile(r"\b(enum|set)\s*\(", re.IGNORECASE)


def withhold_enum_labels(native_type: str) -> str:
    """Replace the member list of every ENUM/SET in *native_type* with ``…``.

    Fails closed on an unbalanced declaration: ``enum('a','b'`` (no closing paren) withholds
    from the opening paren to the end of the string rather than letting the partial member
    list render verbatim.
    """
    if not native_type:
        return native_type
    replaced = _ENUM_DECL_RE.sub(lambda m: f"{m.group(1)}(…)", native_type)
    match = _ENUM_START_RE.search(replaced)
    while match is not None:
        if replaced[match.end() : match.end() + 2] == "…)":
            match = _ENUM_START_RE.search(replaced, match.end())
            continue
        replaced = replaced[: match.end() - 1] + "(…)"
        break
    return replaced
