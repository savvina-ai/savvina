# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Keep identifiers the privacy settings hide out of the semantic prompt.

The stored semantic model may annotate tables and columns the privacy settings
hide (generation keeps them so un-hiding loses no work). The structural sections
(tables, columns, relationships, common joins by table) are narrowed by
``schema_pruning._filter_semantic_to_schema``. The free-text sections cannot be
narrowed structurally, so ``without_hidden_identifiers`` drops what names a
hidden identifier:

- business metrics, segments, derived columns and common joins that mention one
  anywhere except their fixed-vocabulary fields (``_FIXED_VOCABULARY``). The
  common-joins scrub is precautionary: the prompt formatter does not render them
  today;
- ``notes`` entries that mention one;
- per table, ``default_filters`` entries that mention one, and ``base_sql`` when
  it mentions one.

A name is hidden when the privacy settings hide it: see ``_hidden_names``. A
table or column that is merely gone (dropped or renamed in the database) is not
hidden: the structural narrowing already takes it out of the tables and
relationships, and a metric or note that still mentions it is stale, not secret,
so it stays until the next generation rewrites it. Without privacy settings
nothing is hidden. Matching is by whole identifier (Unicode word characters),
case-insensitive, over dotted chains, with SQL quoting (``"x"``, `` `x` ``,
``[x]``) read as a separator.

A dotted reference through an excluded schema (``hr.payroll`` with ``hr``
excluded) is a mention too, whether or not the model annotated the table, but
only when written without whitespace at the dot (a schema name is often an
ordinary word: "Total sales. Includes refunds"), and not when the schema name is
also a visible table's bare name (``hr.id`` with ``public.hr`` visible).

Each dropped item is logged at debug level with the hidden identifier that
triggered it; that log is server-side only and never reaches the prompt.

Limit: the descriptions of *visible* tables and columns are prose and are not
scanned; a hand-written description of a visible column that mentions a hidden
one reaches the prompt.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import replace
import logging
import re
from typing import Any

from ..datasources.models import DataSourceSchema, PrivacySettings, table_key
from ..semantic.generator import policy_hides_column, policy_hides_table
from ..semantic.models import SemanticModel
from .schema_pruning import _schema_signature

logger = logging.getLogger(__name__)

# A dotted identifier chain in casefolded, quote-blanked text: ``s . t . c``.
_CHAIN = re.compile(r"\w+(?:\s*\.\s*\w+)*")
# A name the chain lookup can find; any other name is matched as a substring.
_PLAIN = re.compile(r"\w+(?:\.\w+)*")
_QUOTES = re.compile(r"[`\"\[\]]")
# Quoting that touches a dot, folded into the dot: ``"hr"."payroll"`` -> ``"hr.payroll"``.
_QUOTED_DOT = re.compile(r"[`\"\]]\.[`\"\[]|[`\"\]]\.|\.[`\"\[]")
# A dotted chain with no whitespace at any dot, for the schema-prefix rule: prose
# such as "Total sales. Includes refunds" is not a reference to schema ``sales``.
_TIGHT_CHAIN = re.compile(r"\w+(?:\.\w+)+")
# Item fields drawn from a fixed vocabulary, never from a column name: a hidden
# column named ``count`` must not drop a metric whose aggregation is ``count``.
# An exclusion list, so a field added later is scanned (fails safe).
_FIXED_VOCABULARY = frozenset({"metric_type", "aggregation", "format_hint", "calculation"})


class _HiddenMatcher:
    """Finds a hidden name in a text, in time linear in the text's length.

    Built once per name set. Plain names (Unicode word characters and dots) are
    looked up as every run of up to ``span`` consecutive parts of each dotted
    chain in the text; any other name is a substring check.

    A *tight* chain (no whitespace at a dot; quoting that touches a dot is folded
    into it, so ``"hr"."payroll"`` and ``[hr].[payroll]`` count) in which a hidden
    schema is followed by another part (``hr.payroll``, also catalog-qualified
    ``db.hr.payroll``) is a mention of that schema. Whitespace is not allowed there
    because a schema name is often an ordinary word ("Total sales. Includes ...").
    """

    __slots__ = ("_odd", "_plain", "_schemas", "_span")

    def __init__(self, names: Iterable[str], schemas: Iterable[str] = ()) -> None:
        folded = {n.casefold() for n in names if n}
        self._plain = frozenset(n for n in folded if _PLAIN.fullmatch(n))
        self._odd = tuple(sorted(folded - self._plain))
        self._span = max((n.count(".") + 1 for n in self._plain), default=1)
        self._schemas = frozenset(s.casefold() for s in schemas if s)

    def __bool__(self) -> bool:
        return bool(self._plain or self._odd or self._schemas)

    def first_mention(self, text: str) -> str | None:
        """The first hidden name ``text`` mentions, or None."""
        low = text.casefold()
        if self._schemas:
            for m in _TIGHT_CHAIN.finditer(_QUOTED_DOT.sub(".", low)):
                for part in m.group().split(".")[:-1]:
                    if part in self._schemas:
                        return part
        # Quoting is a separator, not nothing: ``ARRAY[ssn]`` must not read as ``arrayssn``.
        bare = _QUOTES.sub(" ", low)
        for m in _CHAIN.finditer(bare):
            parts = [p.strip() for p in m.group().split(".")]
            n = len(parts)
            for i in range(n):
                for j in range(i + 1, min(i + self._span, n) + 1):
                    candidate = ".".join(parts[i:j])
                    if candidate in self._plain:
                        return candidate
        for o in self._odd:
            if o in low or o in bare:
                return o
        return None


def _strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings_in(v)


def _casefolded(privacy: PrivacySettings) -> PrivacySettings:
    """``privacy`` with its exclusion lists casefolded, for the ``policy_hides_*`` rules.

    The policy compares exact strings, but this module matches casefolded, so the
    stored keys and columns are casefolded before the check and the exclusions must
    be too. The effect is that a case-only difference between a stored spelling and
    an exclusion still hides it: more is hidden than the exact rule says, never less.
    """
    return replace(
        privacy,
        excluded_schemas=[s.casefold() for s in privacy.excluded_schemas],
        excluded_tables=[t.casefold() for t in privacy.excluded_tables],
        excluded_columns=[c.casefold() for c in privacy.excluded_columns],
    )


def _hidden_names(
    semantic: SemanticModel,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> set[str]:
    """The casefolded identifiers ``privacy`` hides, in the forms a text may use.

    ``schema`` is the privacy-filtered *full* schema (not the relevance-narrowed
    one), so an absence is hidden or gone, never merely irrelevant to the question.
    Only the policy hides: a stored table or column absent from ``schema`` that the
    policy does not explain is gone (dropped or renamed), not secret, and
    contributes nothing. With ``privacy`` None the result is empty.

    - A stored table absent from the schema that ``policy_hides_table`` explains
      (its schema or the table excluded; a bare stored key, an anomaly, is read
      leniently with ``bare_ok``) contributes its key, its bare name when no visible
      table shares it, and each of its stored columns as below.
    - A stored column absent from its visible table that ``policy_hides_column``
      explains contributes ``schema.table.column``, ``table.column`` when no
      visible table of that bare name has the column, and its bare name when no
      visible column anywhere shares it: a name visible elsewhere is not a secret.
    - ``privacy.excluded_columns`` and ``excluded_tables`` seed the same forms, so
      an excluded column the model never annotated is caught too. References to an
      excluded schema's unannotated tables are caught by the matcher's schema
      prefixes (``_hidden_schemas``), not by a name here.

    The policy is checked casefolded (``_casefolded``), like everything here.
    """
    if privacy is None:
        return set()
    policy = _casefolded(privacy)
    live = {table_key(t).casefold(): {c.name.casefold() for c in t.columns} for t in schema.tables}
    live_bare = {t.name.casefold() for t in schema.tables}
    live_columns = {c.name.casefold() for t in schema.tables for c in t.columns}
    live_qualified = {
        f"{t.name.casefold()}.{c.name.casefold()}" for t in schema.tables for c in t.columns
    }
    names: set[str] = set()

    def add_table(key: str) -> None:
        names.add(key)
        bare = key.rpartition(".")[2]
        if bare not in live_bare:
            names.add(bare)

    def add_column(key: str, column: str) -> None:
        names.add(f"{key}.{column}")
        qualified = f"{key.rpartition('.')[2]}.{column}"
        if qualified not in live_qualified:
            names.add(qualified)
        if column not in live_columns:
            names.add(column)

    for stored_key, table in semantic.tables.items():
        key = stored_key.casefold()
        visible_columns = live.get(key)
        if visible_columns is None:
            if policy_hides_table(key, policy, bare_ok=True):
                add_table(key)
                for column in table.columns:
                    add_column(key, column.casefold())
            continue
        for column in table.columns:
            folded = column.casefold()
            if folded not in visible_columns and policy_hides_column(key, folded, policy):
                add_column(key, folded)

    for key in policy.excluded_tables:
        if key and key not in live:
            add_table(key)
    for entry in policy.excluded_columns:
        key, _, column = entry.rpartition(".")
        if key and column and column not in live.get(key, ()):
            add_column(key, column)
    return names


def _hidden_schemas(privacy: PrivacySettings | None, schema: DataSourceSchema) -> frozenset[str]:
    """The casefolded excluded schema names the matcher treats as chain prefixes.

    A name that is also the bare name of a visible table is exempt (a name visible
    elsewhere is not a secret): with ``public.hr`` visible, ``hr.id`` is that table.
    """
    if privacy is None or not privacy.excluded_schemas:
        return frozenset()
    live_bare = {t.name.casefold() for t in schema.tables}
    return frozenset(s.casefold() for s in privacy.excluded_schemas if s) - live_bare


def _keep(item: Any, matcher: _HiddenMatcher, kind: str) -> bool:
    for text in _strings_in(item.model_dump(mode="json", exclude=_FIXED_VOCABULARY)):
        hit = matcher.first_mention(text)
        if hit is not None:
            logger.debug(
                "Dropped %s %r from the chat prompt: it names hidden identifier %r",
                kind,
                getattr(item, "name", None) or getattr(item, "description", ""),
                hit,
            )
            return False
    return True


def _scrub(semantic: SemanticModel, matcher: _HiddenMatcher) -> SemanticModel:
    """``semantic`` without what mentions a hidden name; ``semantic`` itself if nothing does."""
    metrics = [m for m in semantic.business_metrics if _keep(m, matcher, "business metric")]
    segments = [s for s in semantic.segments if _keep(s, matcher, "segment")]
    derived = [d for d in semantic.derived_columns if _keep(d, matcher, "derived column")]
    joins = [j for j in semantic.common_joins if _keep(j, matcher, "common join")]
    notes = []
    for note in semantic.notes:
        hit = matcher.first_mention(note)
        if hit is None:
            notes.append(note)
        else:
            logger.debug("Dropped a note from the chat prompt: it names %r", hit)

    tables = {}
    tables_changed = False
    for key, table in semantic.tables.items():
        filters = []
        for f in table.default_filters:
            hit = matcher.first_mention(f)
            if hit is None:
                filters.append(f)
            else:
                logger.debug("Dropped a default filter of %s: it names %r", key, hit)
        base_sql = table.base_sql
        if base_sql is not None:
            hit = matcher.first_mention(base_sql)
            if hit is not None:
                logger.debug("Dropped the base SQL of %s: it names %r", key, hit)
                base_sql = None
        if len(filters) != len(table.default_filters) or base_sql is not table.base_sql:
            table = table.model_copy(update={"default_filters": filters, "base_sql": base_sql})
            tables_changed = True
        tables[key] = table

    update: dict[str, Any] = {}
    for field, kept, before in (
        ("business_metrics", metrics, semantic.business_metrics),
        ("segments", segments, semantic.segments),
        ("derived_columns", derived, semantic.derived_columns),
        ("common_joins", joins, semantic.common_joins),
        ("notes", notes, semantic.notes),
    ):
        if len(kept) != len(before):
            update[field] = kept
    if tables_changed:
        update["tables"] = tables
    return semantic.model_copy(update=update) if update else semantic


_CLEANED_CACHE_SIZE = 64
# ``(id(raw), schema signature, privacy exclusions) -> (raw, cleaned)``. The raw model
# is held so the id cannot alias a later object: a hit requires ``entry[0] is raw``.
# Each entry pins a model, so a superseded model version lingers until evicted
# (bounded by the cap).
_cleaned_cache: OrderedDict[tuple, tuple[SemanticModel, SemanticModel]] = OrderedDict()


def clear_hidden_identifier_cache() -> None:
    """Drop the ``without_hidden_identifiers`` memo. For tests."""
    _cleaned_cache.clear()


def _privacy_signature(privacy: PrivacySettings) -> tuple:
    return (
        tuple(privacy.excluded_schemas),
        tuple(privacy.excluded_tables),
        tuple(privacy.excluded_columns),
    )


def without_hidden_identifiers(
    raw: SemanticModel,
    full_schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> SemanticModel:
    """``raw`` without the free-text items naming an identifier ``privacy`` hides.

    ``full_schema`` is the privacy-filtered schema *before* relevance narrowing.
    Returns ``raw`` itself when ``privacy`` is None, when nothing is hidden or
    when nothing mentions a hidden name. Memoised on ``(the model instance, the
    schema's tables and columns, the privacy exclusions)``; the result is shared
    across turns like ``raw``, so callers must not mutate it. Event-loop thread
    only; not safe under ``to_thread``.
    """
    if privacy is None:
        return raw
    key = (id(raw), _schema_signature(full_schema), _privacy_signature(privacy))
    hit = _cleaned_cache.get(key)
    if hit is not None and hit[0] is raw:
        _cleaned_cache.move_to_end(key)
        return hit[1]
    matcher = _HiddenMatcher(
        _hidden_names(raw, full_schema, privacy), _hidden_schemas(privacy, full_schema)
    )
    cleaned = _scrub(raw, matcher) if matcher else raw
    _cleaned_cache[key] = (raw, cleaned)
    _cleaned_cache.move_to_end(key)
    while len(_cleaned_cache) > _CLEANED_CACHE_SIZE:
        _cleaned_cache.popitem(last=False)
    return cleaned
