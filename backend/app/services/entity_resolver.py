# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Named entity extraction and resolution against schema sample values."""

from __future__ import annotations

import re

from ..datasources.models import DataSourceSchema

# Content inside single or double quotes (at least 2 chars).
_RE_QUOTED = re.compile(r"""["']([^"']{2,})["']""")

# Consecutive capitalized words — at least two tokens starting with uppercase.
_RE_CAPITALIZED = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")

# Common words that should not be treated as named entities.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "Select",
        "From",
        "Where",
        "Group",
        "Order",
        "Having",
        "Join",
        "Show",
        "List",
        "Find",
        "Get",
        "What",
        "Which",
        "Who",
        "The",
        "Top",
        "Last",
        "First",
        "All",
        "Any",
        "Each",
        "Every",
        "How",
        "Many",
    }
)


def extract_entity_candidates(question: str) -> list[str]:
    """Return named entity candidates extracted from a natural language question.

    Extraction order (highest confidence first):
    1. Content of quoted strings (single or double quotes, min 2 chars).
    2. Capitalized multi-word runs that are not stop words.

    Duplicates (case-insensitive) are removed, keeping the first occurrence.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    for m in _RE_QUOTED.finditer(question):
        val = m.group(1).strip()
        if val and val.lower() not in seen:
            seen.add(val.lower())
            candidates.append(val)

    for m in _RE_CAPITALIZED.finditer(question):
        val = m.group(1).strip()
        # Skip if the whole phrase is a known stop word, OR if every word in the
        # phrase is a stop word (e.g. "How Many", "First All").
        words = val.split()
        if val in _STOP_WORDS or all(w in _STOP_WORDS for w in words):
            continue
        if val.lower() not in seen:
            seen.add(val.lower())
            candidates.append(val)

    return candidates


def resolve_entities(
    candidates: list[str],
    schema: DataSourceSchema,
) -> list[str]:
    """Match entity candidates against column sample_values already loaded in schema.

    For each candidate, scans every column's sample_values list for a
    case-insensitive substring match. Returns human-readable resolution notes
    suitable for injecting into the LLM prompt.

    No DB queries are performed — only uses sample_values already in memory.
    Returns an empty list if no matches are found.
    """
    notes: list[str] = []

    for candidate in candidates:
        candidate_lower = candidate.lower()
        matches: list[str] = []

        for table in schema.tables:
            table_label = (
                table.name
                if table.schema_name in ("public", "", None)
                else f"{table.schema_name}.{table.name}"
            )
            for col in table.columns:
                if not col.sample_values:
                    continue
                for sv in col.sample_values:
                    sv_str = str(sv)
                    if candidate_lower in sv_str.lower() or sv_str.lower() in candidate_lower:
                        matches.append(f"{table_label}.{col.name} = '{sv_str}'")
                        break  # one match per column is enough

        if matches:
            notes.append(f"Entity '{candidate}' may match: {'; '.join(matches[:3])}")

    return notes
