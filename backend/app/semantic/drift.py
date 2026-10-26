# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""One schema-vs-model diff, shared by the drift check and the drift apply.

``GET /semantic/drift`` reports what this finds and ``POST /semantic/apply-drift``
acts on it, so the check never names a table or column change the apply would not
act on. (It also names what the apply leaves alone on purpose — a table the role
cannot read — as information, not as a change.)
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, computed_field

from ..datasources.models import table_key

if TYPE_CHECKING:
    from ..datasources.models import DataSourceSchema, PrivacySettings
    from .models import RelationshipEdge, SemanticModel


def policy_hides_table(key: str, privacy: PrivacySettings | None, *, bare_ok: bool = False) -> bool:
    """Whether ``privacy`` explains why table ``key`` is absent.

    ``_apply_privacy_to_schema`` removes excluded schemas, tables and
    ``excluded_columns`` from the schema the generation routes see, so an absent
    table can be hidden rather than gone. A stored key is ``table_key(...)``
    (``datasources.models.table_key``) and either part may itself contain a dot, so
    every split is tried rather than one guessed.

    ``bare_ok=True`` is for a metric's ``related_tables`` entry, which
    ``detect_drift`` accepts unqualified: such a name is hidden when
    ``excluded_tables`` lists it bare or under any schema (the same leniency
    ``detect_drift`` gives a bare live name; a schema or table name containing a
    dot can make that match over-broad, which only suppresses a warning). Without
    ``bare_ok`` a bare key is never treated as hidden, so pruning still drops
    such anomalies.

    A bare name carries no schema, so ``excluded_schemas`` cannot be applied to it
    here; ``detect_drift`` resolves that case through the stored model's qualified
    keys.
    """
    if privacy is None:
        return False
    if bare_ok and "." not in key:
        return any(entry.rpartition(".")[2] == key for entry in privacy.excluded_tables)
    return any(
        privacy.is_table_excluded(key[:i], key[i + 1 :]) for i, ch in enumerate(key) if ch == "."
    )


def policy_hides_column(key: str, column: str, privacy: PrivacySettings | None) -> bool:
    """Whether ``privacy`` explains why ``column`` is absent from table ``key``.

    Delegates to ``PrivacySettings.hides_column`` (an exact match on the live name)
    with ``column`` as stored or quote-stripped: a blob-era stored key ``'"ssn"'``
    names the same column as ``ssn``, while a real name may itself contain a quote
    character (``o'brien_flag``). The one rule for every caller — ``compute_drift``,
    the prune, ``drop_dangling_edits`` and ``edges_in_schema`` — so what the check
    calls hidden is never dropped by the apply, nor named in its summary.
    """
    return privacy is not None and (
        privacy.hides_column(key, column) or privacy.hides_column(key, strip_ident_quotes(column))
    )


# The quoting characters an LLM (or a blob-era store) may put around an identifier.
_IDENT_QUOTES = "\"`'"


def strip_ident_quotes(name: str) -> str:
    """Strip the SQL quoting an LLM (or a blob-era store) put around an identifier.

    ``"public"."Orders"`` → ``public.Orders``, ```sales`.`orders``` → ``sales.orders``,
    ``'status'`` → ``status``. Whitespace and quoting characters (``"``, backtick, ``'``)
    are stripped from around each dotted part and never from inside one, so a real
    name that contains a quote (``o'brien_flag``) is kept as it is. The one
    normaliser: ``SemanticModelGenerator._strip_quotes`` delegates here, so the drift
    check, ``resolve_real_key`` (the generator's near-miss remap and the prune's
    near-miss warning) and ``names_any_table`` read a name alike.
    """
    return ".".join(part.strip().strip(_IDENT_QUOTES) for part in name.strip().split("."))


def resolve_real_key(name: str, real_keys: list[str], *, bare_ok: bool) -> str | None:
    """Map a near-miss ``name`` onto exactly one entry of ``real_keys``, else ``None``.

    Tried in order, the first rule that matches anything decides (ambiguity at that
    rule means ``None``): quote-stripped exact match, case-insensitive match, and —
    when ``bare_ok`` and the name has no dot — a case-insensitive match against the
    part after the last dot of each real key (a bare table name).

    The one resolver: ``SemanticModelGenerator._resolve_real_key`` delegates here, so
    the generator's near-miss remap, the prune's near-miss warning, ``names_any_table``
    and the drift check's respelled columns (``compute_drift``) match a name alike.
    Table names resolve with ``bare_ok``, column names without.
    """
    stripped = strip_ident_quotes(name)
    if stripped in real_keys:
        return stripped
    lowered = stripped.lower()
    matches = [k for k in real_keys if k.lower() == lowered]
    if not matches and bare_ok and "." not in stripped:
        matches = [k for k in real_keys if k.rsplit(".", 1)[-1].lower() == lowered]
    return matches[0] if len(matches) == 1 else None


def respelled_targets(
    gone: Collection[str],
    stored_columns: Collection[str],
    live_columns: Collection[str],
    seen: Collection[str],
) -> set[str]:
    """The live columns held only under a near-miss key in *gone*, a removed key of the
    table: the columns whose sole annotation the prune drops.

    A key resolves (``resolve_real_key``, against every live column, so an ambiguous
    spelling names nothing) to a live column the table does not store under its real
    name (*stored_columns*). A removed key in *seen*, the table's baseline, was a live
    column once (the baseline only records live names), so the database dropped it: it
    is not a respelling. The one rule behind ``compute_drift``'s ``respelled_columns``
    and the finalisation's baseline write.
    """
    live_sorted = sorted(live_columns)
    return {
        target
        for col in gone
        if col not in seen
        and (target := resolve_real_key(col, live_sorted, bare_ok=False)) is not None
        and target not in stored_columns
    }


@dataclass
class SchemaDrift:
    """What the live schema has that the stored model lacks, and vice versa.

    ``removed_columns`` maps a table key to the stored model's column keys
    **verbatim** — a key stored as ``"status"`` stays ``"status"`` — so each entry
    names the stored row exactly. The membership rule is ``_prune_table``'s: a key is
    present on an exact match with a live column or when the policy hides it, so a
    near-miss spelling of a live column (quoted, or differing only in letter case) is
    reported (and removed) verbatim.
    ``detect_drift`` words each entry for display. The apply removes columns through
    ``prune_to_schema``, not through this map.

    ``added_columns`` maps a table key present in both the live schema and the stored
    model to the live column names that are in neither the stored table nor the
    ``known_columns`` baseline (the live columns the model has already seen: one the LLM
    omitted or the user deleted is known, so it is not new), plus the exception below
    for a column held only under a near-miss spelling. A table with no baseline entry
    reports none: nothing says what it has seen. A live column the policy hides is
    never reported (the schema the routes pass is already filtered; the check does not
    rely on it).

    The exception: a live column the model holds only under a near-miss spelling the
    apply removes (``'"status"'`` or ``Status`` with no ``status`` stored; the spelling
    must match that one live column by ``resolve_real_key``, which is quote stripping,
    then letter case, and never an ambiguous match) is new again, so the apply
    re-annotates it instead of dropping its only annotation. Several such keys
    resolving to one column are one added name. A removed key the baseline knows was
    itself a live column (every baseline writer records live names), so its removal is
    a genuine drop, not a respelling: a database that had ``Status`` and ``status`` and
    loses ``Status`` does not make ``status`` new. The columns of a new table are not
    here: the whole table is new.

    ``respelled_columns`` is the subset of ``added_columns`` that is there only by that
    exception, per table (a table with none has no entry), so ``detect_drift`` can word
    the near-miss key and its column as one respelling instead of a drop and a new
    column. A column that is also new by the baseline rule (the baseline never saw it:
    the database renamed ``Status`` to ``status``) is genuinely new, not respelled, and
    its near-miss key is just a removal. It adds nothing to ``is_empty``: every entry is
    in ``added_columns`` already.

    ``dropped_columns`` is the subset of ``removed_columns`` the table's baseline entry
    contains, per table (only a table with a baseline entry can have one; a table with
    none has no entry). A removed key the baseline knows was a live column (every
    baseline writer records live names), so its removal is a genuine drop, and
    ``detect_drift`` words it as one even when it resembles a column still stored. It
    adds nothing to ``is_empty``: every entry is in ``removed_columns`` already.

    ``unreadable_table_keys`` are stored tables the live schema lists in
    ``unreadable_tables``: they exist, but the connection's role cannot read them.
    They are reported, never removed, and do not count as drift to apply —
    the fix is a grant, not a model edit.
    """

    new_table_keys: set[str] = field(default_factory=set)
    removed_table_keys: set[str] = field(default_factory=set)
    removed_columns: dict[str, set[str]] = field(default_factory=dict)
    added_columns: dict[str, set[str]] = field(default_factory=dict)
    respelled_columns: dict[str, set[str]] = field(default_factory=dict)
    dropped_columns: dict[str, set[str]] = field(default_factory=dict)
    unreadable_table_keys: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing for apply-drift to do."""
        return not (
            self.new_table_keys
            or self.removed_table_keys
            or self.removed_columns
            or self.added_columns
        )


def compute_drift(
    schema: DataSourceSchema,
    stored_model: SemanticModel,
    privacy: PrivacySettings | None = None,
    known_columns: Mapping[str, Collection[str]] | None = None,
) -> SchemaDrift:
    """Diff *schema* (the live, privacy-filtered introspection) against *stored_model*.

    **An absence the policy explains is not drift.** The adapters and
    ``_apply_privacy_to_schema`` drop an excluded schema, table or column from the
    schema outright, so an admin's exclusion is absent from it exactly like a
    database drop. The store keeps hidden annotations on purpose (so un-excluding
    restores them without a regenerate), so a table ``policy_hides_table`` explains
    and a column ``policy_hides_column`` explains are never reported as removed.

    **Nor is one the role cannot read** — see ``SchemaDrift.unreadable_table_keys``.
    ``unreadable_tables`` never overlaps ``schema.tables`` (the adapter lists exactly
    the relations it could not read), so such a table is never *new* either.

    ``known_columns`` is the store's baseline (``store.load_known_columns``, passed
    through ``_effective_known_columns`` so a legacy table is baselined): it decides
    which live columns are *new* (``SchemaDrift.added_columns``, and of those the
    ``respelled_columns``) and which removed keys are genuine drops
    (``dropped_columns``); nothing else reads it. ``None`` tracks nothing, so no column
    is reported added and no removed key dropped.
    """
    live_tables = {table_key(t): t for t in schema.tables}
    live_keys = set(live_tables)
    stored_keys = set(stored_model.tables)
    unreadable = set(schema.unreadable_tables)
    known = known_columns or {}

    removed_table_keys: set[str] = set()
    unreadable_table_keys: set[str] = set()
    for key in stored_keys - live_keys:
        if policy_hides_table(key, privacy):
            continue
        if key in unreadable:
            unreadable_table_keys.add(key)
            continue
        removed_table_keys.add(key)

    removed_columns: dict[str, set[str]] = {}
    added_columns: dict[str, set[str]] = {}
    respelled_columns: dict[str, set[str]] = {}
    dropped_columns: dict[str, set[str]] = {}
    for key in stored_keys & live_keys:
        live_cols = {c.name for c in live_tables[key].columns}
        stored_cols = set(stored_model.tables[key].columns)
        gone = {
            col
            for col in stored_cols
            # ``_prune_table.kept``'s rule, so the apply drops nothing the check did not
            # name: present on an exact match (a real name may contain a quote character)
            # or when the policy hides it. A near-miss spelling of a live column
            # (``'"status"'``, a blob-era key, or ``Status``) is reported, verbatim, and
            # removed by the apply; below, the baseline decides whether that removal is a
            # drop and whether its target is new again.
            if col not in live_cols and not policy_hides_column(key, col, privacy)
        }
        if gone:
            removed_columns[key] = gone
        # Only a table the baseline tracks can have new or dropped columns: without an
        # entry there is no telling a column the model never saw from one it chose not
        # to hold, or a dropped column from a near-miss key.
        seen = known.get(key)
        if seen is not None:
            seen_set = set(seen)
            # A removed key the baseline knows was a real column the database dropped.
            if dropped := gone & seen_set:
                dropped_columns[key] = dropped
            # A live column held only under a near-miss spelling the apply removes loses
            # its sole annotation, so it counts as new even though the baseline holds it
            # (``respelled_targets``). A removed key the baseline holds makes nothing new
            # here: its near-miss twin is new only if the baseline never saw it
            # (``unseen`` below) or another near-miss key the baseline does not know
            # respells it.
            respelled = respelled_targets(gone, stored_cols, live_cols, seen_set)
            unseen = live_cols - stored_cols - seen_set
            fresh = {
                col for col in unseen | respelled if not policy_hides_column(key, col, privacy)
            }
            if fresh:
                added_columns[key] = fresh
            # Added only as a respelling: one the baseline never saw is new in its own
            # right, whatever near-miss key the model also held.
            if only_respelled := fresh - unseen:
                respelled_columns[key] = only_respelled

    return SchemaDrift(
        new_table_keys=live_keys - stored_keys,
        removed_table_keys=removed_table_keys,
        removed_columns=removed_columns,
        added_columns=added_columns,
        respelled_columns=respelled_columns,
        dropped_columns=dropped_columns,
        unreadable_table_keys=unreadable_table_keys,
    )


# The lists whose non-emptiness means the model changed. ``warnings``,
# ``tables_not_annotated`` and ``column_tables_failed`` describe the apply, not a change
# to the model.
_STRUCTURAL_FIELDS = (
    "tables_added",
    "tables_removed",
    "columns_added",
    "columns_removed",
    "metrics_added",
    "metrics_removed",
    "joins_added",
    "joins_removed",
    "derived_columns_added",
    "derived_columns_removed",
    "segments_added",
    "segments_removed",
    "relationships_added",
    "relationships_removed",
)


class DriftApplySummary(BaseModel):
    """What one apply-drift changed, by name. Returned with the model; a real apply's
    is also logged as a ``drift_apply`` history row (``store.record_drift_apply``).

    Every list is sorted. ``changed`` is computed from the structural lists (not from
    ``warnings``, ``tables_not_annotated`` or ``column_tables_failed``), so a summary
    with ``changed: false`` can still carry skipped tables and warnings.
    ``columns_removed`` lists only tables that survived (a removed table's columns go
    with it) and its names are quote-stripped for display, except a stripped name that
    is still a column of the table, which keeps its stored spelling.
    ``columns_added`` lists only tables that exist before and after, like
    ``columns_removed``, under the names the model stores them by (live names, so
    never quote-stripped): a blob-era ``"status"`` key swapped for the bare ``status``
    column is listed in both, as the stored spelling and the live one.
    Relationships are labelled ``from_table.from_column → to_table.to_column``.
    ``tables_not_annotated`` are new tables the LLM returned nothing for: they are
    still missing from the model, and the next drift check reports them again.
    ``column_tables_failed`` is to ``columns_added`` what ``tables_not_annotated`` is to
    ``tables_added``: the retry-order note naming the tables whose new columns the apply
    did not get (the field's comment says which). One difference: columns the LLM
    answered for a table but omitted are recorded as seen (``update_known_columns``) and
    warned about, not retried.
    ``warnings`` are, on a real apply, the model's ``generation_warnings`` from it;
    on a preview, ``drop_dangling_edits``' notes on the hand edits it would trim and the
    warnings naming the new tables and columns it would defer or cut.
    """

    # A preview (``apply-drift?dry_run=true``): nothing was written. ``tables_added``
    # are the tables, and ``columns_added`` the new columns, the apply would send to the
    # LLM. Relationships appear for the new columns, between held tables: the catalog
    # edges, missing from the model, on the genuinely new columns (not respellings) the
    # apply would send. A relationship to a new table, and the LLM's items over the new
    # tables, appear only on apply. ``warnings`` are the hand-edit notes and the
    # warnings naming the new tables and columns the apply would defer or cut.
    dry_run: bool = False
    tables_added: list[str] = Field(default_factory=list)
    tables_removed: list[str] = Field(default_factory=list)
    # Surviving table key → the columns the apply added to it (a new table is named in
    # ``tables_added`` instead; its columns are not listed).
    columns_added: dict[str, list[str]] = Field(default_factory=dict)
    columns_removed: dict[str, list[str]] = Field(default_factory=dict)
    metrics_added: list[str] = Field(default_factory=list)
    metrics_removed: list[str] = Field(default_factory=list)
    joins_added: list[str] = Field(default_factory=list)
    joins_removed: list[str] = Field(default_factory=list)
    derived_columns_added: list[str] = Field(default_factory=list)
    derived_columns_removed: list[str] = Field(default_factory=list)
    segments_added: list[str] = Field(default_factory=list)
    segments_removed: list[str] = Field(default_factory=list)
    relationships_added: list[str] = Field(default_factory=list)
    relationships_removed: list[str] = Field(default_factory=list)
    tables_not_annotated: list[str] = Field(default_factory=list)
    # Tables whose new columns this apply did not get: their batch failed (an error or a
    # timeout) or returned nothing for them, or an earlier apply's mark was kept because
    # this one did not reach them. They still lack those columns, the next drift check
    # reports them again, and the next apply sends them last
    # (``_column_tables_in_send_order``).
    column_tables_failed: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def changed(self) -> bool:
        """Whether the model differs before and after: a structural list is non-empty.
        Computed, so no caller can set it and it cannot disagree with the lists; a
        ``changed`` key on input (a logged summary) is ignored and recomputed."""
        return any(getattr(self, name) for name in _STRUCTURAL_FIELDS)


def _added_removed(
    before: Iterable[str], after: Iterable[str], taken_out: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    """``(added, removed)`` by identity. An identity in ``taken_out`` left ``before``
    even if ``after`` has one by the same name: that one is a replacement, so it is
    reported as both removed and added."""
    b, a, out = set(before), set(after), set(taken_out)
    return sorted((a - b) | (out & a)), sorted((b - a) | (out & b))


def _edge_label(edge: RelationshipEdge) -> str:
    return f"{edge.from_table}.{edge.from_column} → {edge.to_table}.{edge.to_column}"


def summarize_model_change(
    before: SemanticModel,
    after: SemanticModel,
    *,
    tables_not_annotated: Sequence[str] = (),
    column_tables_failed: Sequence[str] = (),
    warnings: Sequence[str] = (),
    removed_items: Mapping[str, Iterable[str]] | None = None,
) -> DriftApplySummary:
    """Name everything that left or arrived between *before* and *after*.

    A diff of the two models, not of the drift that caused it, so cascades — a
    metric over a removed table, a relationship rebuilt from the catalog, a
    near-miss column spelling the prune dropped — are reported as what they are.
    Global items are identified the way the store keys them: metrics, derived
    columns and segments by name, joins by ``join_pattern``.

    ``removed_items`` (keyed by the store's kinds: ``metric``, ``common_join``,
    ``derived_column``, ``segment``) names the items the caller took out of
    *before*. A diff by name alone would read a removed item and a new one under the
    same name as "unchanged"; with it, such a pair is reported as removed and added.
    """
    taken_out = removed_items or {}
    tables_added, tables_removed = _added_removed(before.tables, after.tables)
    columns_added: dict[str, list[str]] = {}
    columns_removed: dict[str, list[str]] = {}
    for key in sorted(set(before.tables) & set(after.tables)):
        stored = set(before.tables[key].columns)
        live = set(after.tables[key].columns)
        fresh = live - stored
        if fresh:
            columns_added[key] = sorted(fresh)
        gone = stored - live
        if gone:
            # Quote-stripped for display, unless that is the name of a column that
            # survives (a blob-era ``"id"`` key dropped beside ``id``): then the stored
            # key is the only honest label.
            # A set: a quoted and a bare spelling of one dropped column read the same.
            columns_removed[key] = sorted(
                {c if strip_ident_quotes(c) in live else strip_ident_quotes(c) for c in gone}
            )
    metrics_added, metrics_removed = _added_removed(
        (m.name for m in before.business_metrics),
        (m.name for m in after.business_metrics),
        taken_out.get("metric", ()),
    )
    joins_added, joins_removed = _added_removed(
        (j.join_pattern for j in before.common_joins),
        (j.join_pattern for j in after.common_joins),
        taken_out.get("common_join", ()),
    )
    derived_added, derived_removed = _added_removed(
        (d.name for d in before.derived_columns),
        (d.name for d in after.derived_columns),
        taken_out.get("derived_column", ()),
    )
    segments_added, segments_removed = _added_removed(
        (s.name for s in before.segments),
        (s.name for s in after.segments),
        taken_out.get("segment", ()),
    )
    rels_added, rels_removed = _added_removed(
        map(_edge_label, before.relationships), map(_edge_label, after.relationships)
    )
    summary = DriftApplySummary(
        tables_added=tables_added,
        tables_removed=tables_removed,
        columns_added=columns_added,
        columns_removed=columns_removed,
        metrics_added=metrics_added,
        metrics_removed=metrics_removed,
        joins_added=joins_added,
        joins_removed=joins_removed,
        derived_columns_added=derived_added,
        derived_columns_removed=derived_removed,
        segments_added=segments_added,
        segments_removed=segments_removed,
        relationships_added=rels_added,
        relationships_removed=rels_removed,
        tables_not_annotated=sorted(tables_not_annotated),
        column_tables_failed=sorted(column_tables_failed),
        warnings=list(warnings),
    )
    return summary
