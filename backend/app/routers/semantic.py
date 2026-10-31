# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model router — get, generate, update, and delete per-connection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping, Sequence
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select, update

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..datasources.models import DataSourceSchema
    from ..semantic.drift import SchemaDrift
    from ..semantic.models import RelationshipEdge

from ..auth.dependencies import get_current_active_user
from ..auth.limiter import limiter
from ..config import get_settings
from ..database import get_db
from ..datasources.models import PrivacySettings, table_key
from ..models.semantic_suggestion import SemanticSuggestion
from ..models.user import User
from ..models.user_schema_cache import UserSchemaCache
from ..providers._factory import resolve_provider_config
from ..providers.base import PROVIDER_UNAVAILABLE_MARK
from ..schemas.pagination import PaginatedResponse
from ..schemas.semantic import (
    ApplyDriftResponse,
    DriftApplyHistoryEntry,
    DriftReport,
    GenerateBatchRequest,
    GenerateBatchResponse,
    GenerateInitResponse,
    SemanticModelUpdate,
    SemanticSuggestionResponse,
)
from ..semantic import store as semantic_store
from ..semantic.drift import (
    DriftApplySummary,
    compute_drift,
    matching_real_keys,
    policy_hides_column,
    policy_hides_table,
    strip_ident_quotes,
    summarize_model_change,
)
from ..semantic.generator import (
    SemanticModelGenerator,
    drop_dangling_edits,
    edges_in_schema,
    names_any_table,
    prune_to_schema,
    rename_columns,
)
from ..semantic.models import (
    GENERATION_BATCH_SIZE,
    BusinessMetric,
    ColumnSemantic,
    CommonJoin,
    DerivedColumn,
    GenerationStatus,
    Segment,
    SemanticModel,
    TableSemantic,
    ValueMapping,
)
from ..services.schema_utils import (
    SchemaRefreshError,
    _apply_privacy_to_schema,
    _schema_from_dict,
    get_or_refresh_schema,
    refresh_user_schema_cache,
)
from ._utils import (
    _invalidate_connection_caches,
    cached_json_response,
    get_connection_or_404,
    lock_and_reread_connection,
)

_GENERATION_TIMEOUT_SECONDS = 180

router = APIRouter(prefix="/connections", tags=["semantic"])
logger = logging.getLogger(__name__)


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/{connection_id}/semantic", response_model=SemanticModel)
async def get_semantic_model(
    request: Request,
    connection_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await get_connection_or_404(connection_id, db)
    model = await semantic_store.load_model(db, connection_id)
    if model is None:
        # Deliberately 404, not an empty model: SemanticModelPage derives
        # "has a model" from whether this call errors.
        raise HTTPException(
            status_code=404,
            detail="No semantic model — call /generate/init, /generate/batch, /generate/globals",
        )
    return cached_json_response(model.model_dump(mode="json"), request)


@router.put("/{connection_id}/semantic", response_model=SemanticModel)
async def update_semantic_model(
    connection_id: str,
    body: SemanticModelUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Partially update the semantic model.

    All merge semantics (null = not supplied, [] = explicit clear, per-field
    provenance) live in ``semantic/store.apply_user_patch``; this route only
    maps the store's exceptions onto HTTP status codes.
    """
    # Hold the connection row lock for the whole write: column rows carry no
    # optimistic version predicate, so the lock is what serialises concurrent writers.
    await lock_and_reread_connection(connection_id, db)
    try:
        new_model = await semantic_store.apply_user_patch(
            db, connection_id, body, updated_by=current_user.id
        )
    except semantic_store.SemanticStoreConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # pydantic's ValidationError is a ValueError; the routers map it to 400.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    return new_model


@router.delete("/{connection_id}/semantic", status_code=status.HTTP_204_NO_CONTENT)
async def delete_semantic_model(
    connection_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    # Take the connection row lock (it also covers the 404) so a delete serialises
    # with locked PUT / suggestion-apply writers instead of racing their rows.
    await lock_and_reread_connection(connection_id, db)
    # Rows and the legacy blob both go; the store owns that pairing.
    await semantic_store.delete_model(db, connection_id, changed_by=current_user.id)
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()


# ── Drift detection ─────────────────────────────────────────────────────────────


def _effective_known_columns(
    known: Mapping[str, Collection[str]],
    stored_tables: Mapping[str, TableSemantic],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> dict[str, frozenset[str]]:
    """The baseline ``compute_drift`` reads: *known*, with the entry of each stored table
    the live schema holds completed to what a baseline write records for it
    (``_baseline_entries``): a table with no entry gets its live columns, and every entry
    gains the real names of the table's stored keys the policy hides and its stored live
    columns.

    A table with no entry (a model from before the baseline existed, or a table a user
    added) is baselined at first sight: whatever it holds now is "seen", so no live column
    is reported new on that account, and a column the model holds only under a near-miss
    key is renamed to its live name (``compute_drift``'s ``renamed_columns``), now or,
    for a hidden one, once the policy shows it. A column added before that first sight is
    not reported either; a Regenerate annotates it. A table whose entry predates the
    recording of hidden names gains them the same way, so un-hiding such a column renames
    its near-miss key too; ``check_drift`` persists what this completed, so the name is
    on record by the time the policy stops hiding it.
    """
    effective = {key: frozenset(names) for key, names in known.items()}
    stored_columns = {key: table.columns for key, table in stored_tables.items()}
    for key, names in _baseline_entries(known, stored_columns, schema, privacy).items():
        effective[key] = frozenset(names)
    return effective


@router.get("/{connection_id}/semantic/drift", response_model=DriftReport)
# A live introspection per call, like POST .../schema/refresh: the same limit.
@limiter.limit("5/minute")
async def check_drift(
    request: Request,
    connection_id: str,
    current_admin: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DriftReport:
    """Check for schema drift since the semantic model was last generated.

    Compares against a fresh introspection, so a table dropped a minute ago shows up
    without a separate schema refresh, and stores it as the user's schema cache. When
    the data source cannot be reached the check falls back to that cache and says so
    in the report; with no cache either it fails. On a live check, outside a run, the
    baseline entry of each table the model holds and the live schema shows is completed at
    this check: a table with none gets its live columns, and an older entry gains the
    stored names it lacks (the real name of a hidden stored key, a column annotated by
    hand). A short write under the connection row lock, after the schema refresh is
    committed. So a GET can answer 409 (the connection was deleted), and
    can wait on a save in progress, answering 503 with ``Retry-After`` once the database's
    lock timeout passes (``db_errors.py``).

    OVERHEAD: catalog-read — the introspection reads catalog views and pg_stats only,
    then diffs structure against the stored semantic model. No statistical re-queries.
    """
    conn = await get_connection_or_404(connection_id, db)
    stored_model = await semantic_store.load_model(db, connection_id)
    if stored_model is None:
        raise HTTPException(status_code=404, detail="No semantic model — generate one first")

    schema_source: Literal["live", "cache"] = "live"
    schema_cached_at: str | None = None
    refresh_error: str | None = None
    try:
        schema = await refresh_user_schema_cache(
            conn, current_admin.id, db, get_settings().encryption_key
        )
    except SchemaRefreshError as exc:
        logger.warning(
            "check_drift: live introspection failed, using the cached schema: "
            "connection=%s error=%s",
            connection_id,
            exc,
        )
        user_schema_cache = (
            await db.execute(
                select(UserSchemaCache).where(
                    UserSchemaCache.connection_id == connection_id,
                    UserSchemaCache.user_id == current_admin.id,
                )
            )
        ).scalar_one_or_none()
        if not user_schema_cache or not user_schema_cache.schema_cache:
            raise HTTPException(
                status_code=400,
                detail=f"Could not reach the data source to check drift: {exc}",
            ) from exc
        schema = _schema_from_dict(user_schema_cache.schema_cache)
        schema_source = "cache"
        cached_at = user_schema_cache.schema_cached_at
        schema_cached_at = cached_at.isoformat() if cached_at else None
        refresh_error = str(exc)

    privacy = (
        PrivacySettings.from_dict(conn.privacy_settings)
        if conn.privacy_settings
        else PrivacySettings()
    )
    schema = _apply_privacy_to_schema(schema, privacy)

    known = await semantic_store.load_known_columns(db, connection_id)
    effective = _effective_known_columns(known, stored_model.tables, schema, privacy)
    if (
        _baseline_changes(known, effective)
        and schema_source == "live"
        and stored_model.generation_status != GenerationStatus.TABLES_PARTIAL
    ):
        # Persist what the read completed: the first-sight entry of a table with none, and
        # the names an entry from before they were recorded lacks (the real name of a
        # stored key the policy hides, a column annotated by hand), so the next check can
        # tell a new column from one the model already had, and an un-hidden column's
        # near-miss key is renamed rather than the column reported new. Bookkeeping under
        # the row lock: no version bump, no history. Not from a cached schema (it is not
        # what the model has seen), and not under a run in progress (its batches write
        # the baseline themselves).
        # The refreshed schema cache is committed first, in its own transaction, as
        # ``apply_drift`` does: the refresh always writes the user's schema-cache row and,
        # when the schema changed, deletes the query and embedding cache rows. Every writer
        # that takes this lock and then purges caches (``_invalidate_connection_caches``: a
        # save, a suggestion apply, a model delete, a generation batch, a finalisation, an
        # apply) deletes those same rows, so holding them while waiting for the lock can
        # deadlock. Safe: the session keeps its loaded objects on commit.
        await db.commit()
        await _lock_generation(connection_id, db, "check-drift")
        # ``stored_model`` was loaded before the introspection, so a Regenerate another tab
        # started meanwhile is only seen by re-reading the header under the lock; its
        # batches write the baseline themselves, so the write is skipped. The baseline is
        # re-read too: a batch or an apply may have moved it since the read above, and a
        # row is replaced whole, so what is written must complete the entry as the lock
        # sees it. The model is still the pre-lock read, and so is the report below.
        header = await semantic_store.load_header(db, connection_id)
        if header is not None and header.generation_status != GenerationStatus.TABLES_PARTIAL:
            locked = await semantic_store.load_known_columns(db, connection_id)
            changes = _baseline_changes(
                locked, _effective_known_columns(locked, stored_model.tables, schema, privacy)
            )
            if changes:
                await semantic_store.update_known_columns(db, connection_id, changes)
        # Commit now, with or without a write: releases the connection lock instead of
        # holding it through the diff and the response.
        await db.commit()
    # The store keeps what the policy hides; that absence from ``schema`` is not drift.
    # One diff for the warnings and the flag, the same diff apply-drift acts on.
    drift = compute_drift(schema, stored_model, privacy, effective)
    warnings = SemanticModelGenerator().detect_drift(schema, stored_model, privacy, drift=drift)
    # Persist the refreshed cache (a no-op on the fallback path, and after the baseline
    # commit above).
    await db.commit()

    return DriftReport(
        connection_id=connection_id,
        warnings=warnings,
        warning_count=len(warnings),
        checked_at=datetime.now(UTC).isoformat(),
        schema_source=schema_source,
        schema_cached_at=schema_cached_at,
        refresh_error=refresh_error,
        has_actionable_changes=not drift.is_empty,
    )


# ── Apply drift ─────────────────────────────────────────────────────────────────


def _without_removed_tables(
    model: SemanticModel, removed: set[str]
) -> tuple[list[BusinessMetric], list[CommonJoin], list[DerivedColumn], list[Segment]]:
    """The model's global items minus the ones naming a removed table by its stored key.

    Exact-key only (``schema.table``): a bare-name reference is not evidence enough
    to delete a hand-written metric, so it stays and ``detect_drift`` reports it.
    A segment is removed when every table it applies to is removed; otherwise the
    removed tables are dropped from its ``applicable_tables`` and it stays. A
    segment with no ``applicable_tables`` applies everywhere and is kept.
    """
    metrics = [m for m in model.business_metrics if not any(t in removed for t in m.related_tables)]
    joins = [j for j in model.common_joins if not any(t in removed for t in j.tables)]
    derived = [d for d in model.derived_columns if not any(t in removed for t in d.base_tables)]
    segments = [
        s.model_copy(
            update={"applicable_tables": [t for t in s.applicable_tables if t not in removed]}
        )
        if any(t in removed for t in s.applicable_tables)
        else s
        for s in model.segments
        if not (s.applicable_tables and all(t in removed for t in s.applicable_tables))
    ]
    return metrics, joins, derived, segments


def _live_columns(
    schema: DataSourceSchema, keys: Collection[str] | None = None
) -> dict[str, list[str]]:
    """Table key → the live column names of *schema* (of the tables in *keys* when given).
    The shape ``store.update_known_columns`` takes. The routes' schema is policy-filtered,
    so a hidden column is not here; ``_baseline_entries`` adds the hidden ones the model
    stores (``_held_column_names``)."""
    return {
        table_key(table): [c.name for c in table.columns]
        for table in schema.tables
        if keys is None or table_key(table) in keys
    }


def _held_column_names(
    key: str, live: Collection[str], stored: Collection[str], privacy: PrivacySettings | None
) -> set[str]:
    """The column names table *key*'s baseline must hold because the model stores them:
    each of its *stored* column keys that is a *live* column, and the real name
    (``strip_ident_quotes``) of each stored key the policy hides.

    ``_baseline_entries`` adds these to every entry it builds, for every reader and
    writer of the baseline, so an entry is a superset of the stored live columns and of
    the real names of the stored hidden keys. The schema the routes see is
    policy-filtered, so without the second part a hidden column would leave the entry.
    That is right for one the model never annotated: once un-hidden it is reported new.
    One it stores stays seen, so once un-hidden a near-miss key of it (a blob-era
    ``'"ssn"'``) is renamed to ``ssn`` rather than ``ssn`` being reported new.

    Only a key the policy itself keeps is covered: ``policy_hides_column`` matches the
    key or its quote-stripped form exactly, so a case-only ``SSN`` of a hidden ``ssn`` is
    not hidden, the prune drops it like any gone key, and it is not recorded here.
    """
    live_names = set(live)
    return {name for name in stored if name in live_names} | {
        strip_ident_quotes(name) for name in stored if policy_hides_column(key, name, privacy)
    }


def _baseline_entries(
    seen: Mapping[str, Collection[str]],
    stored_columns: Mapping[str, Collection[str]],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> dict[str, set[str]]:
    """The baseline entry of each table of *stored_columns* that the live *schema* holds:
    the names *seen* gives it (its live columns when *seen* has no entry for it) plus the
    names the model stores (``_held_column_names``: its stored live columns, and the real
    names of its stored keys the policy hides).

    The one builder behind every reader and writer of the baseline, so an entry is always
    a superset of what the table stores and no writer can leave the hidden names out:
    ``_effective_known_columns`` (the drift check, the apply and the finalisation read
    through it) passes the stored baseline as *seen*; a generation batch passes nothing
    (its tables have seen their live columns, so the stored live columns add nothing and
    only the hidden names count); the apply and the finalisation pass what each table had
    seen that is still live. A table absent from the live schema (hidden, unreadable or
    gone) gets no entry here: the caller keeps or prunes the one it has. Nothing is
    dropped: a name in *seen* that left the database stays, so a stored key the baseline
    knew is still a drop to ``compute_drift``; the writers intersect with the live
    columns themselves, before passing *seen*.
    """
    entries: dict[str, set[str]] = {}
    for key, live in _live_columns(schema, set(stored_columns)).items():
        base = seen.get(key)
        entries[key] = set(live if base is None else base) | _held_column_names(
            key, live, stored_columns[key], privacy
        )
    return entries


def _baseline_changes(
    known: Mapping[str, Collection[str]], wanted: Mapping[str, Collection[str]]
) -> dict[str, set[str]]:
    """The entries of *wanted* that differ from the baseline *known*, or that it lacks.

    ``update_known_columns`` reads and writes only the tables it is given (plus, with
    ``prune``, one delete of the rows of tables the model no longer holds), so the
    finalisation and the apply pass it just these: the cost of keeping the baseline
    follows what changed, not the size of the schema."""
    return {
        key: set(names)
        for key, names in wanted.items()
        if key not in known or set(known[key]) != set(names)
    }


def _with_new_columns(
    tables: dict[str, TableSemantic],
    new_columns: Mapping[str, Mapping[str, ColumnSemantic]],
) -> dict[str, TableSemantic]:
    """*tables* with each table's new columns added.

    Additive: a column the table already holds is never replaced, and a table that is
    not in *tables* gets nothing. The input is not mutated. ``apply_drift`` filters its
    annotations against the model re-read under the lock first (``kept_columns``), so a
    column the table already holds (a hand edit, or one another writer stored while the
    LLM ran) never reaches this from the apply; the skip here is a defence for any other
    caller.
    """
    out = dict(tables)
    for key, columns in new_columns.items():
        table = out.get(key)
        if table is None:
            continue
        fresh = {name: col for name, col in columns.items() if name not in table.columns}
        if fresh:
            out[key] = table.model_copy(update={"columns": {**table.columns, **fresh}})
    return out


def _drift_new_columns(
    drift: SchemaDrift, recorded: Mapping[str, Collection[str]]
) -> dict[str, set[str]]:
    """The columns whose catalog edges an apply may add: the columns *drift* reports as new
    (``added_columns``) that the apply *records* as seen, per table.

    A new column the apply does not record (its batch failed, or it was deferred or cut)
    stays new for the next apply, so its edge waits for the apply that records it: added
    once, so that a later deletion sticks. A renamed column (``renamed_columns``) is not
    new: the model held it already, under a near-miss key, so a catalog edge on it that
    the model lacks is one the user deleted, not one they never saw.
    """
    out: dict[str, set[str]] = {}
    for key, columns in drift.added_columns.items():
        settled = columns & set(recorded.get(key, ()))
        if settled:
            out[key] = settled
    return out


def _rebuild_relationships(
    current: Sequence[RelationshipEdge],
    catalog: list[RelationshipEdge],
    provenance: semantic_store.ProvenanceMap,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
    *,
    render_tables: set[str],
    keep_hidden_endpoints: bool,
    new_tables: set[str] | None,
    new_columns: Mapping[str, set[str]] | None = None,
) -> list[RelationshipEdge]:
    """The relationship list a rebuild saves — one rule for globals and apply-drift.

    Edges of ``current`` with a gone endpoint leave first (``edges_in_schema``; a
    hidden or unreadable endpoint counts as live). Marked edges are then carried over
    the rebuilt ``catalog`` edges bounded to ``render_tables``
    (``carry_edited_relationships``).

    ``new_tables`` — the apply's rule: a catalog edge the model does not have is added
    only when it touches one of these (newly annotated) tables. Deleting an unmarked FK
    edge leaves no mark, so its absence from a model that has had every chance to hold
    it is the user's deletion, and re-adding it on every apply would undo that — the
    rule the LLM items follow. Globals passes ``None`` and rebuilds every catalog edge,
    as a Regenerate should.

    ``new_columns`` (table key → column names) widens that rule: a catalog edge on one
    of those columns is added too; an edge on any other column of a held table still is
    not. Callers pass the new columns an apply records as seen, from
    ``_drift_applied_model`` (``_drift_new_columns``). It only matters when
    ``new_tables`` is not ``None``: ``new_tables=None`` already rebuilds every catalog
    edge.

    ``keep_hidden_endpoints`` — the apply's rule: an unmarked edge of ``current`` whose
    endpoint the policy hides or the role cannot read has no catalog twin (the catalog
    is built from the narrowed schema), yet its endpoint exists, so it is kept — the
    apply never removes what is hidden or unreadable. An edge alive in the strict
    schema is the catalog's to rebuild and leaves if the catalog no longer has it.
    Globals drops hidden-endpoint edges on purpose: they return from the FK once the
    endpoint is visible again (see "Finalisation" in semantic-model-generation.md).
    """
    key = semantic_store.relationship_key
    survivors = edges_in_schema(current, schema, privacy, keep_unreadable=True)
    rebuilt = semantic_store.carry_edited_relationships(
        survivors, catalog, provenance, render_tables=render_tables
    )
    if new_tables is not None:
        had = {key(e) for e in current}
        fresh_columns = new_columns or {}

        def on_new_column(e: RelationshipEdge) -> bool:
            return e.from_column in fresh_columns.get(e.from_table, ()) or (
                e.to_column in fresh_columns.get(e.to_table, ())
            )

        rebuilt = [
            e
            for e in rebuilt
            if key(e) in had
            or e.from_table in new_tables
            or e.to_table in new_tables
            or on_new_column(e)
        ]
    if keep_hidden_endpoints:
        strict = {key(e) for e in edges_in_schema(survivors, schema)}
        have = {key(e) for e in rebuilt}
        rebuilt += [e for e in survivors if (k := key(e)) not in strict and k not in have]
    return rebuilt


# One apply is one HTTP request, and the proxy gives it 900s (``frontend/nginx.conf``).
# Each LLM phase may take up to ``_GENERATION_TIMEOUT_SECONDS`` (180s): three annotation
# batches and the globals pass are four calls, 720s, which leaves room for the live
# introspection and the save. A batch is either new tables or tables with new columns:
# new tables take batches first, tables with new columns the ones left over. Whatever is
# past the budget is not sent; the next drift check names it again and the next apply
# annotates it.
_DRIFT_MAX_BATCHES = 3
_DRIFT_MAX_NEW_TABLES = _DRIFT_MAX_BATCHES * GENERATION_BATCH_SIZE
# A new-column batch is also bounded by how many columns it asks for. An annotated column
# is 150-250 tokens of JSON (more with value mappings), so 25 columns is at most about
# 6k tokens: inside the smallest provider output budget (8k) with room for the envelope.
# A reply cut off by ``max_output_tokens`` is invalid JSON and fails the whole batch, and
# a retry would send the same columns, so the cap has to hold on every provider. A table
# with more new columns gets the first ones (by name) now and the rest on a later apply
# (``_cut_columns_warning``). A later plan may derive this from
# ``provider.max_output_tokens``; that needs the provider resolved before the preview.
_DRIFT_MAX_COLUMNS_PER_BATCH = 25
# A throttle or an unavailable provider (``_run_llm_phase``): a column-only apply that no
# batch has answered yet lets these through instead of turning them into a warning, so the
# request fails with the provider's status and the user sees the throttle and can apply
# again, rather than getting a 200 that changed nothing.
_TRANSIENT_STATUSES = frozenset({429, 503})


def _deferred_tables_warning(deferred: list[str], *, preview: bool) -> str:
    """The warning naming the new tables past ``_DRIFT_MAX_NEW_TABLES`` (up to 5 of them)."""
    count = len(deferred)
    sample = ", ".join(deferred[:5]) + (", …" if count > 5 else "")
    noun, pronoun = ("table", "it") if count == 1 else ("tables", "them")
    if preview:
        what = f"{count} more new {noun} will not be annotated by this apply"
        then = f"Run Check Drift and apply again afterwards to annotate {pronoun}."
    else:
        verb = "was" if count == 1 else "were"
        what = f"{count} more new {noun} {verb} not annotated by this apply"
        then = f"Run Check Drift and apply again to annotate {pronoun}."
    return f"{what} (at most {_DRIFT_MAX_NEW_TABLES} per apply): {sample}. {then}"


def _deferred_columns_warning(deferred: list[str], *, preview: bool) -> str:
    """The warning naming the tables with new columns past the apply's batch budget (up to
    5 of them)."""
    count = len(deferred)
    sample = ", ".join(deferred[:5]) + (", …" if count > 5 else "")
    noun = "table" if count == 1 else "tables"
    verb, again = (
        ("will not be", "apply again afterwards") if preview else ("were not", "apply again")
    )
    return (
        f"The new columns of {count} more {noun} {verb} annotated by this apply "
        f"(at most {_DRIFT_MAX_BATCHES} LLM batches per apply): {sample}. "
        f"Run Check Drift and {again} to annotate them."
    )


def _cut_columns_warning(cut: Mapping[str, Collection[str]], *, preview: bool) -> str:
    """The warning for the new columns ``_DRIFT_MAX_COLUMNS_PER_BATCH`` left out of their
    batch (up to 5 of their tables named)."""
    tables = sorted(cut)
    sample = ", ".join(tables[:5]) + (", …" if len(tables) > 5 else "")
    verb, again = (
        ("will not be", "apply again afterwards") if preview else ("were not", "apply again")
    )
    count = sum(len(names) for names in cut.values())
    return (
        f"{count} more new column(s) on {len(tables)} table(s) {verb} annotated "
        f"by this apply (at most {_DRIFT_MAX_COLUMNS_PER_BATCH} per batch): "
        f"{sample}. Run Check Drift and {again} to annotate them."
    )


def _plan_column_batches(
    added: Mapping[str, Iterable[str]],
    order: Sequence[str],
    *,
    max_batches: int,
    ddl_sizes: Mapping[str, int],
    ddl_limit: int,
) -> tuple[list[dict[str, list[str]]], list[str], dict[str, list[str]]]:
    """Pack the tables of *order* (keys of *added*) into at most *max_batches* new-column
    batches.

    A batch holds at most ``GENERATION_BATCH_SIZE`` tables, ``_DRIFT_MAX_COLUMNS_PER_BATCH``
    columns, and tables whose DDL sizes (``ddl_sizes``, from ``table_ddl_sizes``) sum to at
    most *ddl_limit*: ``_schema_to_ddl`` leaves out of the prompt every table from the
    first one past ``_MAX_DDL_CHARS``, and a table the LLM never saw would be annotated
    blind. A table that alone breaks a cap still goes, by itself: over the column cap it
    is cut to its first columns by name; over the DDL limit its DDL is left out of the
    prompt, which still names the columns to annotate. Returns
    ``(batches, deferred, cut)``: each batch maps a table key to its sorted column names
    to ask for; ``deferred`` are the keys of *order* that did not fit the budget, in order;
    ``cut`` maps a placed table to the columns the column cap left out, sorted.
    """
    batches: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    size = chars = 0
    cut: dict[str, list[str]] = {}
    for i, key in enumerate(order):
        names = sorted(added[key])
        columns = names[:_DRIFT_MAX_COLUMNS_PER_BATCH]
        block = ddl_sizes.get(key, 0)
        if current and (
            size + len(columns) > _DRIFT_MAX_COLUMNS_PER_BATCH
            or chars + block > ddl_limit
            or len(current) >= GENERATION_BATCH_SIZE
        ):
            batches.append(current)
            current, size, chars = {}, 0, 0
        if len(batches) >= max_batches:
            return batches, list(order[i:]), cut
        current[key] = columns
        size += len(columns)
        chars += block
        if len(names) > len(columns):
            cut[key] = names[len(columns) :]
    if current:
        batches.append(current)
    return batches, [], cut


async def _last_apply_skips(db: AsyncSession, connection_id: str, field: str) -> set[str]:
    """The table keys the newest logged apply listed under *field* (a summary list of
    keys such as ``tables_not_annotated``). An entry whose summary is not a dict, or
    whose field is missing or not a list, names none; entries that are not strings are
    ignored."""
    for record in await semantic_store.list_drift_applies(db, connection_id, limit=1):
        names = record.summary.get(field) if isinstance(record.summary, dict) else None
        if isinstance(names, list):
            return {key for key in names if isinstance(key, str)}
    return set()


async def _new_tables_in_send_order(
    db: AsyncSession, connection_id: str, new_keys: set[str]
) -> list[str]:
    """``new_keys`` in the order the apply sends them: sorted, except that the tables
    the newest logged apply reported in ``tables_not_annotated`` go last.

    Only the first ``_DRIFT_MAX_NEW_TABLES`` are sent. In plain sorted order, tables
    the LLM keeps returning nothing for would hold the front of the list on every
    apply and starve the ones past the cap; moved last, they are retried once the
    others are in. The log is read only when the cap applies (``_last_apply_skips``:
    an unreadable entry orders nothing).
    """
    ordered = sorted(new_keys)
    if len(ordered) <= _DRIFT_MAX_NEW_TABLES:
        return ordered
    retry = await _last_apply_skips(db, connection_id, "tables_not_annotated")
    return [k for k in ordered if k not in retry] + [k for k in ordered if k in retry]


async def _column_tables_in_send_order(
    db: AsyncSession, connection_id: str, keys: set[str]
) -> tuple[list[str], set[str]]:
    """The tables with new columns in the order the apply sends them, and the ones the
    newest logged apply reported in ``column_tables_failed`` (those still in *keys*).

    Sorted, except that the failed ones go last: a table whose batch always fails (too
    wide for the provider, say) would otherwise hold the front of the list on every apply
    and, because a failed batch ends the phase, starve every table behind it. The failed
    set is also returned: an apply that does not send a table must carry its mark
    forward, or the next one would put it first again.
    """
    ordered = sorted(keys)
    if not ordered:
        return ordered, set()
    retry = await _last_apply_skips(db, connection_id, "column_tables_failed") & keys
    return [k for k in ordered if k not in retry] + [k for k in ordered if k in retry], retry


class _DriftAssembly(NamedTuple):
    """What ``_drift_applied_model`` returns.

    ``removed_items`` names, by the store's kinds (``metric``, ``common_join``,
    ``derived_column``, ``segment``), the items taken out because they named a
    removed table, for ``summarize_model_change(removed_items=...)``.
    ``llm_warnings`` are the two metric checks over the LLM metrics actually
    added (empty without new tables).
    """

    model: SemanticModel
    provenance: semantic_store.ProvenanceMap
    protected: semantic_store.ProtectedValues
    notes: list[str]
    removed_items: dict[str, set[str]]
    llm_warnings: list[str]


def _drift_applied_model(
    current_model: SemanticModel,
    snapshot: semantic_store.ProvenanceSnapshot,
    *,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
    drift: SchemaDrift,
    dialect: str,
    gen: SemanticModelGenerator,
    relationship_edges: list[RelationshipEdge],
    new_tables: dict[str, TableSemantic],
    new_columns: Mapping[str, Mapping[str, ColumnSemantic]] | None = None,
    recorded_columns: Mapping[str, Collection[str]],
    llm_items: tuple[list[BusinessMetric], list[DerivedColumn], list[Segment]],
    connection_id: str,
    log: bool = True,
) -> _DriftAssembly:
    """*current_model* with *drift* applied, and what ``save_model`` needs to store it.

    No session: the apply runs this under the row lock, with the drift recomputed
    against the re-read model, and saves the result; the dry run runs it on an
    unlocked read, with no new tables, no annotated columns and no LLM items, and only
    diffs the result, so a preview is computed by the code that applies it. The dry
    run passes ``log=False``: the hand-edit trims and near-miss drops it previews did
    not happen, so they must not be logged as if they had.

    The drift's ``renamed_columns`` are applied first (``rename_columns``): each near-miss
    key becomes its live column's name, with its annotation, its user-edit marks and
    protected values, its table's references and its relationship endpoints, so the
    hand-edit trims, the prune and the edge rebuild below see it under its live name.

    ``new_columns`` are the LLM annotations of new columns of tables the model already
    holds, added additively (``_with_new_columns``).

    ``recorded_columns`` (table key → column names) are the new columns this apply
    records as seen: the apply passes the columns its column phase settled (annotated,
    or omitted by an LLM that answered their table); the dry run passes the columns the
    apply would send. Catalog FK edges on existing tables follow them, not the
    annotations:

    - an edge on a new column (``drift``'s added columns) arrives with the apply that
      records the column, whether or not the LLM annotated it, so an omitted column
      still gets its edge;
    - a column whose batch failed, or that was deferred or cut, is not recorded, so
      its edge arrives with the apply that later records it, and a deletion after that
      sticks, since the column is no longer new;
    - a renamed column never brings back a deleted edge: the model held the column
      already, so a missing edge is the user's deletion.

    The dry run, given the columns it would send, shows these edges between held
    tables; an edge to a new table appears only on apply, like every relationship to
    one. It shows what the apply would add if every batch succeeds and the drift is
    unchanged, not a promise: the apply recomputes the drift under the lock.

    Returns a ``_DriftAssembly``; its ``notes`` are ``drop_dangling_edits``' notes on
    the hand edits it trimmed. The fields a generation sets (``generated_at``,
    ``generation_model``, ``is_user_reviewed``, ``generation_warnings``) are left to
    the caller.
    """
    tables, relationships, marks, protected = rename_columns(
        current_model.tables,
        current_model.relationships,
        snapshot.marks,
        snapshot.protected,
        drift.renamed_columns,
    )
    renamed = current_model.model_copy(update={"tables": tables, "relationships": relationships})
    provenance, protected, edit_notes = drop_dangling_edits(
        marks, protected, schema, privacy, log=log
    )

    # Removed tables and columns leave here; hidden and unreadable ones stay.
    pruned = prune_to_schema(renamed.tables, schema, privacy, log=log, keep_unreadable=True)
    pruned = _with_new_columns(pruned, new_columns or {})
    merged_tables = {**pruned, **new_tables}
    metrics, joins, derived_columns, segments = _without_removed_tables(
        renamed, drift.removed_table_keys
    )
    removed_items = {
        "metric": {m.name for m in renamed.business_metrics} - {m.name for m in metrics},
        "common_join": {j.join_pattern for j in renamed.common_joins}
        - {j.join_pattern for j in joins},
        "derived_column": {d.name for d in renamed.derived_columns}
        - {d.name for d in derived_columns},
        "segment": {s.name for s in renamed.segments} - {s.name for s in segments},
    }
    # A removed item's marks leave with it. ``save_model`` stamps marks by name, so a
    # mark left behind would land on a same-named item the LLM adds below: frozen
    # against regeneration and shown as hand-edited, though no one edited it.
    gone_globals = {
        f"{kind}:{name}"
        for kind in ("common_join", "derived_column", "segment")
        for name in removed_items[kind]
    }
    provenance = {
        **provenance,
        "metric": {
            k: v
            for k, v in provenance.get("metric", {}).items()
            if k not in removed_items["metric"]
        },
        "global_item": {
            k: v for k, v in provenance.get("global_item", {}).items() if k not in gone_globals
        },
    }
    llm_warnings: list[str] = []
    if new_tables:
        llm_metrics, llm_derived, llm_segments = llm_items
        # Only items over a newly annotated table are new. The globals pass sees every
        # table, so an item over unchanged tables alone is one the model has had a
        # chance to hold already: if it is missing, the user deleted it (or it is a
        # near-duplicate of one they kept), and adding it back on every apply would
        # undo that. A segment with no ``applicable_tables`` applies everywhere, so it
        # is not about the new tables either.
        added_keys = set(new_tables)
        known = sorted(set(merged_tables) | {table_key(t) for t in schema.tables})

        def over_new(refs: list[str]) -> bool:
            return names_any_table(refs, added_keys, known)

        llm_metrics = [m for m in llm_metrics if over_new(m.related_tables)]
        llm_derived = [d for d in llm_derived if over_new(d.base_tables)]
        llm_segments = [s for s in llm_segments if over_new(s.applicable_tables)]
        # Additive: an existing item (hand-written or generated) is never replaced.
        llm_metrics, llm_derived, llm_segments = _drop_unstorable_items(
            llm_metrics, llm_derived, llm_segments, connection_id
        )
        have_metrics = {m.name for m in metrics}
        have_derived = {d.name for d in derived_columns}
        have_segments = {s.name for s in segments}
        added_metrics = [m for m in llm_metrics if m.name not in have_metrics]
        metrics = metrics + added_metrics
        # The globals pass's metrics may include ones dropped above in favour of the
        # user's same-named one, so the checks run over what was added.
        llm_warnings = [
            *gen._validate_metrics_time_columns(added_metrics, schema),
            *gen._validate_metric_filter_values(added_metrics, merged_tables),
        ]
        derived_columns = derived_columns + [d for d in llm_derived if d.name not in have_derived]
        segments = segments + [s for s in llm_segments if s.name not in have_segments]

    relationships = _rebuild_relationships(
        renamed.relationships,
        relationship_edges,
        provenance,
        schema,
        privacy,
        render_tables=set(merged_tables),
        keep_hidden_endpoints=True,
        new_tables=set(new_tables),
        new_columns=_drift_new_columns(drift, recorded_columns),
    )

    model = renamed.model_copy(
        update={
            "tables": merged_tables,
            "business_metrics": metrics,
            "common_joins": joins,
            "derived_columns": derived_columns,
            "segments": segments,
            "relationships": relationships,
            **_rebuilt_fields(gen, schema, dialect),
        }
    )
    return _DriftAssembly(model, provenance, protected, edit_notes, removed_items, llm_warnings)


@router.post("/{connection_id}/semantic/apply-drift", response_model=ApplyDriftResponse)
@limiter.limit("5/minute")
async def apply_drift(
    request: Request,
    connection_id: str,
    provider: str = "claude",
    dry_run: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApplyDriftResponse:
    """Apply the schema drift ``GET .../semantic/drift`` reports to the stored model.

    - Removed tables: dropped, with every metric, join and derived column that names
      them by key, every segment that applies only to them (a segment that also
      applies to a surviving table just loses the removed ones from its list), and
      every relationship touching them.
    - Removed columns: dropped from their table, with the references to them
      (``prune_to_schema``'s rule, the same one a regenerate applies) and any hand
      edit naming them (``drop_dangling_edits``).
    - Renamed columns: a near-miss key (quoted, or in another letter case) of a live
      column the baseline has seen and the table does not store under its real name
      (``compute_drift``'s ``renamed_columns``) is renamed to that name, keeping its
      annotation, its hand edits, its table's references and its relationships
      (``rename_columns``). No LLM call: the model already holds the column.
    - New tables: annotated by the LLM (in batches, at most ``_DRIFT_MAX_NEW_TABLES``
      per apply so one request stays inside the proxy timeout; a warning names the
      rest, which the next apply takes), then — if it annotated any — one globals
      pass. Of its metrics, joins, derived columns and segments only those over a
      newly annotated table are taken (one over unchanged tables alone may be one the
      user deleted), merged *additively*: an existing item keeps its payload against
      a same-named new one. Its warnings are recomputed over the metrics added. A new
      table that the model re-read under the lock already holds (stored by a run
      that finished meanwhile) is left as stored and does not count as new.
      An item removed above is not "existing": a same-named LLM item replaces it,
      without its marks, and the summary reports it as removed and added.
    - New columns on a table the model holds: a live column neither stored nor in the
      baseline (``known_columns``: the live columns the model has seen). Annotated by the
      LLM alone (the table is sent as context; only the new columns are asked for and
      taken) and added to their table. Existing columns are never rewritten. They share
      the apply's batch budget (``_DRIFT_MAX_BATCHES``) with new tables (new tables
      first); a batch is also capped at ``_DRIFT_MAX_COLUMNS_PER_BATCH`` columns and at
      one prompt's DDL, and what does not fit waits for the next apply, named in a
      warning. A batch that answered for a table but left a column of it out records
      that column as seen (a warning names it); a batch that failed, or a table it
      returned nothing for, leaves its columns new, and the table goes last next time
      (``column_tables_failed``). A failure of this phase is a warning, not an error —
      the rest of the drift, and every batch answered before the failure, is still
      applied — except that a throttle or an unavailable provider (429, 503)
      propagates when there is nothing else to save: no new or removed table, no
      removed or renamed column and no batch answered yet. The provider is resolved up
      front only when there are new tables or nothing but new columns (an unresolvable
      one is a 400 then); beside removals or renames alone, failing to resolve it fails
      this phase like a failed call, and the removals and renames are applied. A new
      column does not trigger the globals pass, and an apply that annotates one marks
      the model unreviewed.
    - Relationships, time expressions, notes and the schema hash are recomputed. A
      catalog FK edge the model lacks is added only when it touches a newly annotated
      table or a new column that this apply records as seen, annotated or omitted (a
      renamed column is not new); any other missing edge is one the user deleted, and
      stays deleted.

    The baseline is written in the same transaction (``update_known_columns`` with
    ``prune``): each new table annotated gets its live columns, each held table keeps
    what it had seen plus the columns a batch answered for and every live column it
    stores, and loses the columns that left the live schema (dropped, or hidden by the
    policy), except that a hidden column it stores stays, under its real name
    (``_held_column_names``). A renamed column was seen already and is stored under its
    live name, so it stays seen.

    No LLM call when no table or column was added. The schema is always a **live**
    introspection: acting on a cached one could delete what still exists, so an
    unreachable source is a 400. What the privacy policy hides and what the
    connection's role cannot read are never treated as removed (``compute_drift``).
    A run in progress or interrupted (``tables_partial``) is a 409: pruning under it
    would race its batches.

    ``dry_run=true`` is the preview: the same assembly (``_drift_applied_model``) on the
    unlocked read the drift was computed from, no LLM call, nothing written but the
    refreshed schema cache (which every call commits). New tables are listed by name in
    ``tables_added`` — exactly the ones the apply would send, in its order
    (``_new_tables_in_send_order``) — and new columns in ``columns_added``: exactly the
    ones the apply would ask for (``_plan_column_batches``); renamed columns are in
    ``columns_renamed``, as on apply. ``relationships_added`` includes the catalog edges
    on the new columns it would send, between held tables; relationships to new tables
    and the LLM's items over them appear only on apply. The preview's ``warnings`` are
    the hand-edit notes and the warnings naming the new tables (past
    ``_DRIFT_MAX_NEW_TABLES``) and the new columns (past the batch budget or the column
    cap) the apply would defer or cut.

    Answers with the stored model (on a dry run, the model as it is) and
    ``summarize_model_change`` of the result against the model it was built from —
    every cascade named. Every apply that saves also logs that summary in its save's
    transaction (``semantic_store.record_drift_apply``); a dry run and an apply with no
    drift to act on (before or under the lock) write, purge and log nothing, and so does
    a column-only apply whose 429 or 503 propagates. An apply whose result equals the
    current model (the LLM returned nothing for the only new tables or columns, or a
    column batch failed) saves and purges nothing, but is logged when it skipped tables
    or marked ``column_tables_failed``, because the next apply reads that entry to retry
    them last; it commits the baseline when that moved (for example, columns an answered
    batch left out are seen now, a table had no entry yet, or a known column the model
    never stored left the live schema, dropped or hidden).
    Rate limited to 5/minute, like ``POST .../schema/refresh``: every call (a preview
    too) runs a live introspection. A preview and its apply are two calls.
    """
    user_id: str = current_user.id
    conn = await get_connection_or_404(connection_id, db)
    # With provenance in the same pass: the dry run previews this very read, so the
    # drift it diffs and the model it assembles cannot come from two different states.
    stored_model, stored_snapshot = await semantic_store.load_model_with_provenance(
        db, connection_id
    )
    if stored_model is None:
        raise HTTPException(status_code=404, detail="No semantic model — generate one first")
    header = await semantic_store.load_header(db, connection_id)
    if header is not None and header.generation_status == GenerationStatus.TABLES_PARTIAL:
        raise HTTPException(
            status_code=409,
            detail=(
                "A generation run is in progress or was interrupted — finish or restart it first"
            ),
        )

    try:
        schema = await refresh_user_schema_cache(conn, user_id, db, get_settings().encryption_key)
    except SchemaRefreshError as exc:
        raise HTTPException(
            status_code=400, detail=f"Could not reach the data source to apply drift: {exc}"
        ) from exc
    privacy = (
        PrivacySettings.from_dict(conn.privacy_settings)
        if conn.privacy_settings
        else PrivacySettings()
    )
    schema = _apply_privacy_to_schema(schema, privacy)

    # The baseline decides which live columns are new and which near-miss keys are
    # renamed. A table with no entry is baselined in memory here, so a legacy table has no
    # new columns. ``check_drift`` persists that baseline, and so does an apply that
    # writes.
    known = await semantic_store.load_known_columns(db, connection_id)
    drift = compute_drift(
        schema,
        stored_model,
        privacy,
        _effective_known_columns(known, stored_model.tables, schema, privacy),
    )
    # The refreshed schema cache (an upsert plus cache deletes, which take row locks) is
    # committed now rather than held open through the LLM calls: a rollback below must
    # not lose it, and concurrent chat cache writes must not wait on it. Safe because
    # the session keeps loaded objects on commit and the model is re-read under the lock.
    await db.commit()
    if drift.is_empty:
        return ApplyDriftResponse(
            semantic_model=stored_model, summary=DriftApplySummary(dry_run=dry_run)
        )

    gen = SemanticModelGenerator()
    # Strict (no ``privacy``), as in the generation routes: no edge on a column
    # outside the schema reaches a prompt or is saved as a fresh edge.
    relationship_edges = edges_in_schema(gen._build_relationship_graph(schema), schema)
    dialect = conn.source_type
    new_keys = await _new_tables_in_send_order(db, connection_id, drift.new_table_keys)
    to_annotate = new_keys[:_DRIFT_MAX_NEW_TABLES]
    deferred = new_keys[_DRIFT_MAX_NEW_TABLES:]
    # Batches the new tables use; tables with new columns take the rest of the budget.
    used_batches = -(-len(to_annotate) // GENERATION_BATCH_SIZE)  # ceiling division
    column_keys, failed_before = await _column_tables_in_send_order(
        db, connection_id, set(drift.added_columns)
    )
    column_batches, deferred_columns, cut_columns = _plan_column_batches(
        drift.added_columns,
        column_keys,
        max_batches=_DRIFT_MAX_BATCHES - used_batches,
        ddl_sizes=gen.table_ddl_sizes(schema, drift.added_columns),
        ddl_limit=gen._MAX_DDL_CHARS,
    )
    # With nothing else to save, a throttle or an unavailable provider fails the request
    # with its status (``_TRANSIENT_STATUSES``) unless an earlier column batch answered;
    # beside other changes it is a warning (see the column phase below).
    only_columns = not (
        drift.new_table_keys
        or drift.removed_table_keys
        or drift.removed_columns
        or drift.renamed_columns
    )

    if dry_run:
        # No lock: nothing is written, and a write landing after the read is re-diffed
        # by the apply itself. The provenance is what ``drop_dangling_edits`` needs to
        # name the hand edits the apply would trim. The columns the apply would send
        # stand in for the ones it would record, so the catalog edges on them show.
        would_send = {key: columns for batch in column_batches for key, columns in batch.items()}
        preview = _drift_applied_model(
            stored_model,
            stored_snapshot,
            schema=schema,
            privacy=privacy,
            drift=drift,
            dialect=dialect,
            gen=gen,
            relationship_edges=relationship_edges,
            new_tables={},
            recorded_columns=would_send,
            llm_items=([], [], []),
            connection_id=connection_id,
            log=False,
        )
        notes = list(preview.notes)
        if deferred:
            notes.append(_deferred_tables_warning(deferred, preview=True))
        if deferred_columns:
            notes.append(_deferred_columns_warning(deferred_columns, preview=True))
        if cut_columns:
            notes.append(_cut_columns_warning(cut_columns, preview=True))
        summary = summarize_model_change(
            stored_model,
            preview.model,
            warnings=notes,
            removed_items=preview.removed_items,
            columns_renamed=drift.renamed_columns,
        )
        return ApplyDriftResponse(
            semantic_model=stored_model,
            summary=summary.model_copy(
                update={
                    "dry_run": True,
                    # The tables the apply would send, sorted for display like every list.
                    # ``changed`` follows: a new table alone is a change.
                    "tables_added": sorted(to_annotate),
                    # Likewise the columns it would ask for (``_plan_column_batches``),
                    # by table key in sorted order.
                    "columns_added": dict(sorted(would_send.items())),
                }
            ),
        )

    new_tables: dict[str, TableSemantic] = {}
    new_columns: dict[str, dict[str, ColumnSemantic]] = {}
    llm_items: tuple[list[BusinessMetric], list[DerivedColumn], list[Segment]]
    llm_items = ([], [], [])
    configured_model: str | None = None
    provider_name: str | None = None
    provider_failure: str | None = None  # why the provider could not be resolved
    if to_annotate or (column_batches and only_columns):
        # New tables need the provider, and a column-only apply has nothing else to do:
        # an unresolvable one fails the request (400) before the model is touched.
        llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)
    elif column_batches:
        # New columns beside removals or renames only: an unresolvable provider fails the
        # column phase like a failed call, and the removals and renames are still applied.
        try:
            llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)
        except HTTPException as exc:
            provider_failure = str(exc.detail)[:200]
    if to_annotate:
        for start in range(0, len(to_annotate), GENERATION_BATCH_SIZE):
            batch = to_annotate[start : start + GENERATION_BATCH_SIZE]
            generated = await _run_llm_phase(
                gen.generate_tables(
                    schema=schema,
                    provider=llm_provider,
                    model=configured_model or None,
                    table_keys=batch,
                    relationship_edges=relationship_edges,
                    privacy=privacy,
                ),
                what=f"Drift batch {batch} generation",
                connection_id=connection_id,
                provider=provider_name,
            )
            # ``generate_tables`` grounds its output to the batch it was given.
            new_tables.update(generated)
        if new_tables:
            # Nothing to say about tables the LLM returned nothing for: skip the call
            # (and the warnings it would add to a model it changed nothing in).
            # Pruned strictly so the LLM is not invited to write metrics over gone or
            # hidden tables; log=False, the post-lock prune warns about the same drops.
            prompt_tables = {
                **prune_to_schema(stored_model.tables, schema, log=False),
                **new_tables,
            }
            # ``_drift_applied_model`` runs the metric checks over the metrics it actually
            # adds (``llm_warnings``).
            metrics, derived, segments = await _run_llm_phase(
                gen.generate_globals(
                    schema=schema,
                    provider=llm_provider,
                    model=configured_model or None,
                    all_tables=prompt_tables,
                    relationship_edges=relationship_edges,
                ),
                what="Drift globals generation",
                connection_id=connection_id,
                provider=provider_name,
            )
            llm_items = (metrics, derived, segments)
    # Unlike the phases above, a failure here is not fatal: the removals, the renames, the
    # new tables and the tables an earlier batch answered for are applied without the
    # remaining new columns (named in the warnings and in ``column_tables_failed``), so one
    # bad batch cannot hold back the rest of the drift. The exception is an apply with
    # nothing else to save, not even an answered batch, where a throttle or an unavailable
    # provider propagates with its status (``_TRANSIENT_STATUSES``): the user sees it and
    # can apply again, instead of a 200 that changed nothing. The batches after a failed one
    # are not tried: the provider is likely still failing, and every try costs up to the
    # generation timeout; with no provider resolved, none is tried. A table the batch
    # returned *nothing* for is failed too (per table, without ending the phase): an empty
    # answer is a soft failure, not evidence that its columns need no annotation, and must
    # not mark them as seen for good.
    answered: dict[str, set[str]] = {}  # table key → the columns a batch answered for
    no_answer: set[str] = set()  # tables a batch that returned got nothing for
    failed_batch: dict[str, list[str]] = {}  # the batch whose call failed, if one did
    column_failure: str | None = None
    # The batches not sent: those after the failed one, or all of them without a provider.
    untried: list[dict[str, list[str]]] = column_batches if provider_failure else []
    for i, batch in enumerate([] if provider_failure else column_batches):
        try:
            generated_columns = await _run_llm_phase(
                gen.generate_new_columns(
                    schema=schema,
                    provider=llm_provider,
                    model=configured_model or None,
                    columns_by_table=batch,
                    table_context={key: stored_model.tables[key] for key in batch},
                    relationship_edges=relationship_edges,
                    privacy=privacy,
                ),
                what=f"Drift new-columns batch {list(batch)} generation",
                connection_id=connection_id,
                provider=provider_name,
            )
        except HTTPException as exc:
            if only_columns and not answered and exc.status_code in _TRANSIENT_STATUSES:
                raise
            column_failure = str(exc.detail)[:200]
            failed_batch = batch
            untried = column_batches[i + 1 :]
            break
        new_columns.update(generated_columns)
        for key, names in batch.items():
            if generated_columns.get(key):
                answered.setdefault(key, set()).update(names)
            else:
                no_answer.add(key)

    # Re-read under the row lock: a PUT or a batch may have landed during the LLM calls.
    await _lock_generation(connection_id, db, "apply-drift")
    current_model, snapshot = await semantic_store.load_model_with_provenance(db, connection_id)
    if current_model is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Semantic model was deleted during apply-drift")
    current_header = await semantic_store.load_header(db, connection_id)
    if (
        current_header is not None
        and current_header.generation_status == GenerationStatus.TABLES_PARTIAL
    ):
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "A generation run started while drift was being applied — its results "
                "were discarded"
            ),
        )

    # A Regenerate or another apply may have stored a "new" table while the LLM ran.
    # The re-read holds it now: it is not new, so its stored annotation is not
    # overwritten, and items over it do not count as "over a new table".
    new_tables = {k: v for k, v in new_tables.items() if k not in current_model.tables}
    # Likewise a column another writer stored while the LLM ran is not new any more, and
    # a table the re-read no longer holds gets nothing.
    kept_columns: dict[str, dict[str, ColumnSemantic]] = {}
    for key, cols in new_columns.items():
        table = current_model.tables.get(key)
        if table is None:
            continue
        if fresh := {name: col for name, col in cols.items() if name not in table.columns}:
            kept_columns[key] = fresh
    new_columns = kept_columns
    # The drift the apply acts on is the one against the model it saves over, not the
    # pre-lock read: a table a PUT added during the LLM calls that the database no
    # longer has leaves with its items, and a drift another tab applied meanwhile is
    # not applied — or logged — twice. Against the baseline as the lock sees it, too: a
    # generation batch or another apply may have moved it while the LLM ran.
    known = await semantic_store.load_known_columns(db, connection_id)
    effective = _effective_known_columns(known, current_model.tables, schema, privacy)
    drift = compute_drift(schema, current_model, privacy, effective)
    if drift.is_empty:
        # Every table sent to the LLM is stored by now too (``new_tables`` is empty).
        # Nothing to do: no row rewrite, no version bump, no cache purge, no log entry.
        await db.rollback()
        return ApplyDriftResponse(semantic_model=current_model, summary=DriftApplySummary())
    assembly = _drift_applied_model(
        current_model,
        snapshot,
        schema=schema,
        privacy=privacy,
        drift=drift,
        dialect=dialect,
        gen=gen,
        relationship_edges=relationship_edges,
        new_tables=new_tables,
        new_columns=new_columns,
        # Exactly the new columns the baseline write below records for held tables.
        recorded_columns=answered,
        llm_items=llm_items,
        connection_id=connection_id,
    )
    gen_warnings = [*assembly.llm_warnings, *assembly.notes]
    # The plan was made before the lock: what another writer stored meanwhile is not new
    # any more, so the warnings name only what the post-lock drift still reports.
    if still_deferred := [key for key in deferred if key in drift.new_table_keys]:
        gen_warnings.append(_deferred_tables_warning(still_deferred, preview=False))
    if still_waiting := [key for key in deferred_columns if drift.added_columns.get(key)]:
        gen_warnings.append(_deferred_columns_warning(still_waiting, preview=False))
    still_cut = {
        key: kept
        for key, names in cut_columns.items()
        if (kept := [name for name in names if name in drift.added_columns.get(key, ())])
    }
    if still_cut:
        gen_warnings.append(_cut_columns_warning(still_cut, preview=False))

    # ``drift`` is the post-lock one: a column another writer stored meanwhile is not
    # reported as skipped.
    def still_new(key: str, name: str) -> bool:
        return name in drift.added_columns.get(key, ()) and name not in new_columns.get(key, {})

    # A batch answered for the table but left these out: seen from now on (the baseline
    # below), so drift clears instead of re-asking forever. A batch that failed, or was
    # not reached after one, leaves its columns new: they retry.
    omitted = [
        f"{key}.{name}"
        for key, names in sorted(answered.items())
        for name in sorted(names)
        if still_new(key, name)
    ]
    planned = {key: names for batch in column_batches for key, names in batch.items()}

    def still_new_of(keys: Collection[str]) -> list[str]:
        """The columns of the tables *keys* that were asked for and are still new, as
        ``table.column``."""
        return [
            f"{key}.{name}" for key in sorted(keys) for name in planned[key] if still_new(key, name)
        ]

    def sample_of(items: list[str]) -> str:
        return ", ".join(items[:5]) + (", …" if len(items) > 5 else "")

    if omitted:
        gen_warnings.append(
            f"{len(omitted)} new column(s) were not annotated (the LLM returned nothing for "
            f"them) and are now treated as seen, so Check Drift stops reporting them: "
            f"{sample_of(omitted)}. Regenerate to annotate them."
        )
    # Not annotated and still new: each reason is its own clause.
    got_nothing = still_new_of(no_answer)
    call_failed = still_new_of(failed_batch)
    never_sent = still_new_of({key for batch in untried for key in batch})
    reasons: list[str] = []
    if got_nothing:
        reasons.append(f"the LLM returned nothing for their table: {sample_of(got_nothing)}")
    if call_failed:
        reasons.append(
            f"the new-column LLM call failed ({column_failure}): {sample_of(call_failed)}"
        )
    if never_sent and provider_failure:
        reasons.append(
            f"the LLM provider could not be used ({provider_failure}): {sample_of(never_sent)}"
        )
    elif never_sent:
        later = "the batch" if len(untried) == 1 else f"the {len(untried)} batches"
        were = "was" if len(untried) == 1 else "were"
        reasons.append(f"{later} after the failed call {were} not tried: {sample_of(never_sent)}")
    if reasons:
        count = len(got_nothing) + len(call_failed) + len(never_sent)
        gen_warnings.append(
            f"{count} new column(s) were not annotated and stay missing from the model "
            f"({'; '.join(reasons)}). Run Check Drift and apply again to retry."
        )
    # Tables whose batch failed, or that got no answer, go last next time. A table this
    # apply got no answer for — deferred past the budget, or in a batch after the failed
    # one — keeps the mark it had: the log holds only the newest apply, so dropping it
    # here would put the failing table first again.
    column_tables_failed = sorted(
        key
        for key in no_answer | set(failed_batch) | (failed_before - set(answered))
        if drift.added_columns.get(key)
    )
    # What the model has seen after this apply: the baseline of the tables it holds (a
    # table it removed is still in ``current_model``; a row of a table it does not hold
    # is left to the prune); each new table's live columns (annotated or not, like a
    # generation batch); every column a batch answered for. A new table the LLM returned
    # nothing for is not here: it must stay new.
    # A column that left the database leaves the baseline together with its annotation
    # (the prune in ``_drift_applied_model`` removed it): if it comes back it is new
    # again and is annotated, instead of being silently "seen". A hidden or unreadable
    # table is not in the live schema, so its entry is kept as it is. A column the policy
    # hides is absent from the live columns too, so it leaves the entry here, as in the
    # finalisation, unless the model stores it: ``_baseline_entries`` records it under its
    # real name, with every live column the model stores however it got there (a
    # hand-added annotation included), so the baseline is a superset of what the model
    # stores. Once un-hidden, a hidden column the model never annotated is reported new
    # and the next apply annotates it.
    live_now = _live_columns(schema)
    # ``effective`` has an entry for every stored live table (``_effective_known_columns``
    # over the same model and schema), so each one below has a base to complete.
    seen = {
        key: set(names) & set(live_now[key]) if key in live_now else set(names)
        for key, names in effective.items()
        if key in current_model.tables
    }
    seen.update(
        _baseline_entries(
            seen, {key: t.columns for key, t in current_model.tables.items()}, schema, privacy
        )
    )
    # A new table has seen every live column, whatever row it left behind (a check that
    # seeded it before an earlier apply removed it): that row is overwritten.
    for key, names in _live_columns(schema, set(new_tables)).items():
        seen[key] = set(names)
    for key, names in answered.items():
        if key in current_model.tables:
            seen.setdefault(key, set()).update(names)
    moved = _baseline_changes(known, seen)  # all the store is asked to write
    header_update: dict[str, Any] = {"generation_warnings": gen_warnings}
    if new_tables or new_columns:
        # New LLM content (tables or columns): unreviewed, like after a generation run.
        header_update.update(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "generation_model": configured_model or provider_name,
                "is_user_reviewed": False,
            }
        )
    final = assembly.model.model_copy(update=header_update)

    def summary_of(after: SemanticModel) -> DriftApplySummary:
        return summarize_model_change(
            current_model,
            after,
            # Sent and still missing: the LLM returned nothing for them. The tables past
            # the cap were never sent; the warning above names those.
            tables_not_annotated=sorted(set(to_annotate) - set(after.tables)),
            column_tables_failed=column_tables_failed,
            warnings=gen_warnings,
            removed_items=assembly.removed_items,
            columns_renamed=drift.renamed_columns,
        )

    summary = summary_of(final)
    if not summary.changed and not assembly.notes:
        # The LLM returned nothing for the only new tables or columns, or a batch failed:
        # the model is as it was. (A trimmed hand edit — ``notes`` — is a change the lists
        # do not show, so it is saved.) Not saved or purged. Skipped tables and failed
        # batches are still logged, and the commit releases the lock: the next apply
        # orders its tables by the newest log entry (``_new_tables_in_send_order``,
        # ``_column_tables_in_send_order``), so without the entry the tables that keep
        # failing would hold the front of the list and starve the rest. A baseline that
        # moved is committed too — for example, columns the LLM answered but omitted are
        # now seen, a legacy table is baselined, or a known column the model never stored
        # left the live schema: that is what lets drift clear.
        skipped = summary.tables_not_annotated or summary.column_tables_failed
        if skipped or moved:
            await semantic_store.update_known_columns(db, connection_id, moved, prune=True)
            if skipped:
                await semantic_store.record_drift_apply(
                    db, connection_id, summary.model_dump(mode="json"), applied_by=user_id
                )
            await db.commit()
        else:
            await db.rollback()
        return ApplyDriftResponse(semantic_model=current_model, summary=summary)

    try:
        stored = await semantic_store.save_model(
            db,
            connection_id,
            final,
            updated_by=user_id,
            source="drift",
            provenance=assembly.provenance,
            protected=assembly.protected,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Of what was stored: the store fits values and dedupes name-keyed items.
    summary = summary_of(stored)
    # In the save's transaction, like the log entry, so the baseline and the model it
    # describes commit together. ``prune`` drops the rows of the tables the save removed.
    await semantic_store.update_known_columns(db, connection_id, moved, prune=True)
    # Logged in the save's transaction: an apply is in the log exactly when it happened.
    await semantic_store.record_drift_apply(
        db, connection_id, summary.model_dump(mode="json"), applied_by=user_id
    )
    # The schema cache holds this apply's own live introspection: kept (the page's
    # column lists read it); the query and embedding caches go.
    await _invalidate_connection_caches(connection_id, db, keep_schema=True)
    await db.commit()
    return ApplyDriftResponse(semantic_model=stored, summary=summary)


@router.get("/{connection_id}/semantic/drift/history", response_model=list[DriftApplyHistoryEntry])
async def drift_apply_history(
    connection_id: str,
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[DriftApplyHistoryEntry]:
    """The connection's logged Apply Drifts, newest first.

    Each summary is validated into today's ``DriftApplySummary``: a field an older
    entry lacks takes its default, so the page can render it; an entry that does not
    validate at all is skipped and logged, and the route reads on so the response
    still holds ``limit`` entries when the log has them.

    OVERHEAD: app-only — reads ``semantic_model_history`` in the app DB.
    """
    await get_connection_or_404(connection_id, db)
    entries: list[DriftApplyHistoryEntry] = []
    offset = 0
    while len(entries) < limit:
        wanted = limit - len(entries)
        records = await semantic_store.list_drift_applies(
            db, connection_id, limit=wanted, offset=offset
        )
        offset += len(records)
        for record in records:
            try:
                summary = DriftApplySummary.model_validate(record.summary)
            except ValidationError:
                # A row from before a field changed type, or not a summary at all: one
                # bad entry must not 500 the whole list. Skipped, and said so in the log;
                # the next page fills its place.
                logger.warning(
                    "drift_apply_history: skipping unreadable entry: connection=%s at=%s",
                    connection_id,
                    record.applied_at.isoformat(),
                )
                continue
            entries.append(
                DriftApplyHistoryEntry(
                    applied_at=record.applied_at.isoformat(),
                    applied_by=record.applied_by,
                    summary=summary,
                )
            )
        if len(records) < wanted:
            break  # the log ran out
    return entries


# ── Semantic suggestions ────────────────────────────────────────────────────────


@router.get(
    "/{connection_id}/semantic/suggestions",
    response_model=PaginatedResponse[SemanticSuggestionResponse],
)
async def list_suggestions(
    connection_id: str,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[SemanticSuggestionResponse]:
    """Return pending semantic model suggestions from user feedback.

    OVERHEAD: app-only — reads from PostgreSQL app DB only.
    """
    await get_connection_or_404(connection_id, db)
    total = (
        await db.scalar(
            select(func.count())
            .select_from(SemanticSuggestion)
            .where(
                SemanticSuggestion.connection_id == connection_id,
                SemanticSuggestion.is_applied.is_(False),
            )
        )
        or 0
    )
    result = await db.execute(
        select(SemanticSuggestion)
        .where(
            SemanticSuggestion.connection_id == connection_id,
            SemanticSuggestion.is_applied.is_(False),
        )
        .order_by(SemanticSuggestion.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    suggestions = result.scalars().all()
    return PaginatedResponse(
        items=[SemanticSuggestionResponse.model_validate(s) for s in suggestions],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{connection_id}/semantic/suggestions/{suggestion_id}/apply",
    response_model=SemanticModel,
)
async def apply_suggestion(
    connection_id: str,
    suggestion_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Apply a specific semantic suggestion to the stored model.

    OVERHEAD: app-only — reads/writes PostgreSQL app DB only.
    """
    # Take the connection row lock first (it also 404s a missing connection) and hold
    # it for the whole write: column rows carry no optimistic version predicate, so the
    # lock is what serialises concurrent writers. The suggestion is read under it, and
    # fresh, so a concurrent apply of the same suggestion is seen as applied.
    await lock_and_reread_connection(connection_id, db)

    sug_result = await db.execute(
        select(SemanticSuggestion)
        .where(
            SemanticSuggestion.id == suggestion_id,
            SemanticSuggestion.connection_id == connection_id,
        )
        .execution_options(populate_existing=True)
    )
    suggestion = sug_result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if suggestion.is_applied:
        # Applying it again would overwrite whatever has been edited since, as a fresh
        # hand edit; a double click or a second tab must not do that.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Suggestion already applied"
        )

    current = await semantic_store.load_model(db, connection_id)
    if current is None:
        raise HTTPException(status_code=404, detail="No semantic model to update")
    table = current.tables.get(suggestion.table_key)
    if table is None:
        raise HTTPException(
            status_code=404,
            detail=f"Table {suggestion.table_key!r} not found in semantic model",
        )

    correction_type = suggestion.correction_type
    value = suggestion.value if isinstance(suggestion.value, dict) else {}
    table_patch: dict

    # ``field`` comes from chat feedback (arbitrary text). Refuse a column the model
    # doesn't have rather than let the store create a nameless column row.
    column_targeted = correction_type == "add_value_mapping" or (
        correction_type == "update_description" and value.get("target", "table") == "column"
    )
    col = table.columns.get(suggestion.field) if column_targeted else None
    if column_targeted and col is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Column {suggestion.field!r} not found on {suggestion.table_key!r} "
                "in semantic model"
            ),
        )

    if correction_type == "add_value_mapping":
        existing = [m.model_dump(mode="json") for m in col.value_mappings]
        try:
            # Full mapping dict (raw_value, display_value, description) so the
            # store persists whole items, never a partial suggestion payload.
            new_mapping = ValueMapping.model_validate(value).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if new_mapping["raw_value"] not in {m["raw_value"] for m in existing}:
            existing.append(new_mapping)
        table_patch = {"columns": {suggestion.field: {"value_mappings": existing}}}
    elif correction_type == "update_filter":
        filters = list(table.default_filters)
        new_filter = value.get("filter", "")
        if new_filter and new_filter not in filters:
            filters.append(new_filter)
        table_patch = {"default_filters": filters}
    elif correction_type == "update_description":
        if value.get("target", "table") == "column":
            table_patch = {
                "columns": {suggestion.field: {"description": value.get("description", "")}}
            }
        else:
            table_patch = {"description": value.get("description", "")}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown correction_type: {correction_type!r}")

    try:
        patch = SemanticModelUpdate.model_validate({"tables": {suggestion.table_key: table_patch}})
        new_model = await semantic_store.apply_user_patch(
            db, connection_id, patch, updated_by=current_user.id
        )
    except semantic_store.SemanticStoreConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        # pydantic's ValidationError is a ValueError; the routers map it to 400.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.execute(
        update(SemanticSuggestion)
        .where(SemanticSuggestion.id == suggestion_id)
        .values(is_applied=True)
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    return new_model


# ── Phased generation helpers + endpoints ──────────────────────────────────────


async def _resolve_provider(
    provider_param: str,
    db: AsyncSession,
) -> tuple:
    """Resolve and instantiate an LLM provider from a name or UUID.

    Returns ``(provider, configured_model, provider_name)``.
    """
    try:
        provider, model, provider_name, _max_tokens = await resolve_provider_config(
            provider_param, db
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return provider, model, provider_name


async def _load_schema_for_connection(
    connection_id: str,
    current_user: User,
    db: AsyncSession,
) -> tuple:
    """Load schema from cache (or auto-refresh from the live DB) and return ``(schema, conn)``."""
    conn = await get_connection_or_404(connection_id, db)
    settings = get_settings()
    try:
        schema = await get_or_refresh_schema(conn, current_user.id, db, settings.encryption_key)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Schema load failed: {e}") from e
    return schema, conn


def _drop_unstorable[T](
    items: list[T],
    kind: semantic_store.NameKind,
    name_of: Callable[[T], str],
    connection_id: str,
) -> list[T]:
    """``items`` without the ones ``save_model`` would reject for their name.

    The rule is the store's (``name_rejection``): one bad LLM item must not fail
    the whole finalisation, and the router must not keep its own copy of the
    column limits.
    """
    kept: list[T] = []
    for item in items:
        why = semantic_store.name_rejection(kind, name_of(item))
        if why is not None:
            logger.warning(
                "dropped generated %s: %s: connection=%s",
                kind,
                why,
                connection_id,
            )
            continue
        kept.append(item)
    return kept


def _names_gone_table(
    refs: Iterable[str], live_keys: list[str], privacy: PrivacySettings | None
) -> bool:
    """Whether any of a join's table names is a table that left the database.

    *live_keys* is every table key that exists or is stored: the live schema, the tables
    the role cannot read, and the model's own tables (``generate_semantic_globals``
    passes all three). A name matches as elsewhere (``matching_real_keys``:
    quotes stripped, case-insensitive, a bare name such as ``orders`` for
    ``public.orders``). A bare name shared by several keys is ambiguous, not gone, and
    so is a table the privacy policy hides. No names, nothing to judge.
    """
    for ref in refs:
        if matching_real_keys(ref, live_keys, bare_ok=True):
            continue
        if policy_hides_table(strip_ident_quotes(ref), privacy, bare_ok=True):
            continue
        return True
    return False


def _drop_unstorable_items(
    metrics: list[BusinessMetric],
    derived: list[DerivedColumn],
    segments: list[Segment],
    connection_id: str,
) -> tuple[list[BusinessMetric], list[DerivedColumn], list[Segment]]:
    """``_drop_unstorable`` over the three kinds of LLM global item, keyed as the store
    keys them (by name)."""
    return (
        _drop_unstorable(metrics, "metric", lambda m: m.name, connection_id),
        _drop_unstorable(derived, "derived_column", lambda d: d.name, connection_id),
        _drop_unstorable(segments, "segment", lambda s: s.name, connection_id),
    )


def _rebuilt_fields(
    gen: SemanticModelGenerator, schema: DataSourceSchema, dialect: str
) -> dict[str, Any]:
    """The model fields every rebuild (globals, apply-drift) recomputes from the schema."""
    return {
        "time_expressions": gen.build_time_expressions(dialect),
        "schema_hash": gen.compute_schema_hash(schema),
        "source_dialect": dialect,
        "notes": gen._auto_generate_notes(schema),
    }


_RATE_LIMIT_MARK = "[RATE_LIMIT]"


async def _run_llm_phase[T](
    call: Coroutine[Any, Any, T], *, what: str, connection_id: str, provider: str
) -> T:
    """Await one LLM phase under the generation timeout, mapping failures to HTTP.

    ``generate_tables``, ``generate_globals`` and ``generate_new_columns`` wrap provider
    errors in ``ValueError``, keeping the providers' ``[RATE_LIMIT]`` marker in the
    message.

    - Timeout: 504, logged as a warning.
    - ``ValueError`` carrying ``[RATE_LIMIT]``: 429, so the page's retry-with-backoff
      sees it. Logged as a warning without a traceback: throttles are expected and
      retried. (A provider's exhausted quota carries ``[QUOTA_EXCEEDED]`` instead
      and is a 422: waiting does not fix it.)
    - ``ValueError`` carrying ``[PROVIDER_UNAVAILABLE]`` (a provider 5xx or a dropped
      connection; the SDK's own retries are off for these calls): 503, retried by the
      page like a throttle, logged as a warning without a traceback.
    - Any other ``ValueError``: 422, logged with its traceback.
    - Anything else: 500, logged with its traceback.

    ``provider`` appears only in the log lines; ``what`` also reaches the 504 detail.
    """
    try:
        return await asyncio.wait_for(call, timeout=_GENERATION_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        logger.warning(
            "%s timed out after %ss: connection=%s provider=%s",
            what,
            _GENERATION_TIMEOUT_SECONDS,
            connection_id,
            provider,
        )
        raise HTTPException(
            status_code=504, detail=f"{what} timed out after {_GENERATION_TIMEOUT_SECONDS}s"
        ) from exc
    except ValueError as exc:
        if _RATE_LIMIT_MARK in str(exc):
            logger.warning(
                "%s throttled: connection=%s provider=%s: %s", what, connection_id, provider, exc
            )
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if PROVIDER_UNAVAILABLE_MARK in str(exc):
            logger.warning(
                "%s provider unavailable: connection=%s provider=%s: %s",
                what,
                connection_id,
                provider,
                exc,
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        logger.exception("%s failed: connection=%s provider=%s", what, connection_id, provider)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("%s failed: connection=%s provider=%s", what, connection_id, provider)
        raise HTTPException(status_code=500, detail=str(exc) or f"{what} failed") from exc


async def _lock_generation(connection_id: str, db: AsyncSession, during: str) -> None:
    """Take the connection row lock (``FOR NO KEY UPDATE``) the generation routes serialise on.

    The helper 404s on a vanished row; these routes report 409 instead (after a
    rollback), because the connection existed when the request started and the
    frontend treats 409 as "stop, state moved on" rather than as a client error.
    """
    try:
        await lock_and_reread_connection(connection_id, db)
    except HTTPException as exc:
        await db.rollback()
        if exc.status_code == 404:
            raise HTTPException(
                status_code=409, detail=f"Connection was deleted during {during}"
            ) from exc
        raise


@router.post("/{connection_id}/semantic/generate/init", response_model=GenerateInitResponse)
@limiter.limit("10/minute")
async def generate_semantic_init(
    request: Request,
    connection_id: str,
    provider: str = "claude",
    resume: bool = False,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GenerateInitResponse:
    """Phase 1 of phased generation: plan the run and mark it started. No LLM call.

    Returns ``pending_table_keys`` — the tables the frontend still has to POST to
    ``/generate/batch`` — which the frontend slices into batches itself; nothing
    positional survives on the server.

    **Never destructive.** No table or column row is touched; a run reconciles
    onto whatever rows exist. ``resume=true`` continues an interrupted run: it
    requires a ``tables_partial`` model whose ``schema_hash`` still matches the
    schema, and narrows the work list to the tables that run had not reached.
    A ``tables_partial`` model that has no schema hash or run id (one migration
    0003 backfilled) is not resumable; ``resume=true`` then runs a fresh
    non-destructive run and answers ``resumed: false``.
    """
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    gen = SemanticModelGenerator()
    all_keys = [table_key(t) for t in schema.tables]
    tables_total = len(all_keys)
    schema_hash = gen.compute_schema_hash(schema)

    # Lock before reading or writing run state. Without it, a fresh init could commit
    # between a batch's post-lock header check and its reconcile, and that batch's
    # tables would be stamped with the new run id.
    await _lock_generation(connection_id, db, "generation init")

    resumed = False
    pending = all_keys
    tables_done = 0
    if resume:
        existing = await semantic_store.load_header(db, connection_id)
        if existing is None or existing.generation_status != GenerationStatus.TABLES_PARTIAL:
            await db.rollback()  # release the row lock
            raise HTTPException(status_code=400, detail="No interrupted generation to resume")
        if existing.schema_hash is None or existing.generation_run_id is None:
            # Migration 0003 backfills a blob-era interrupted run with neither a hash
            # (the old init hashed nothing) nor a run id (nothing was stamped). There
            # is no run to narrow to: fall through to a fresh, non-resumed run, which
            # reconciles onto the rows and keeps every hand edit — what the user
            # asked for, minus a work list the server cannot shorten. ``resumed`` stays
            # False, and that is what begin_generation is told.
            pass
        elif existing.schema_hash != schema_hash:
            # The hash, not a table count: a schema that changed shape while keeping
            # its cardinality would otherwise resume onto the wrong tables.
            await db.rollback()  # release the row lock
            raise HTTPException(
                status_code=409,
                detail=(
                    "Schema changed since the interrupted run — regenerate (keeps your "
                    "edits); Restart from scratch discards them"
                ),
            )
        else:
            pending = await semantic_store.pending_table_keys(db, connection_id, all_keys)
            resumed = True
            tables_done = len(all_keys) - len(pending)
    batch_count = max(1, -(-len(pending) // GENERATION_BATCH_SIZE))

    await semantic_store.begin_generation(
        db,
        connection_id,
        schema_hash=schema_hash,
        tables_total=tables_total,
        tables_done=tables_done,
        batch_size=GENERATION_BATCH_SIZE,
        source_dialect=conn.source_type,
        resume=resumed,
    )
    await db.commit()

    return GenerateInitResponse(
        connection_id=connection_id,
        tables_total=tables_total,
        batch_count=batch_count,
        batch_size=GENERATION_BATCH_SIZE,
        pending_table_keys=pending,
        resumed=resumed,
    )


@router.post("/{connection_id}/semantic/generate/batch", response_model=GenerateBatchResponse)
# CONCURRENCY=2 in the frontend worker pool roughly doubles request density
# against this limit; retry/backoff in the frontend absorbs occasional 429s.
@limiter.limit("20/minute")
async def generate_semantic_batch(
    request: Request,
    connection_id: str,
    body: GenerateBatchRequest,
    provider: str = "claude",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GenerateBatchResponse:
    """Phase 2 of phased generation: annotate one batch of tables via LLM.

    Batch identity is the keys named in the body, never a position. Each call
    reconciles its results onto the stored rows (skipping every field a user
    has edited) and returns the run's progress — not the model.
    """
    user_id: str = current_user.id
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    header = await semantic_store.load_header(db, connection_id)
    if header is None or header.generation_status != GenerationStatus.TABLES_PARTIAL:
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init first before calling /generate/batch",
        )
    # The run this batch belongs to. A /generate/init that lands while the LLM call
    # is in flight mints a new one; see the post-lock check below.
    run_id = header.generation_run_id
    if run_id is None:
        # A tables_partial header with no run id — migration 0003 backfills an
        # interrupted blob-era run that way. There is nothing to stamp tables with:
        # reconciling would write NULL, the progress count would stay 0 and a later
        # resume would regenerate every table. /generate/init (resume=true included)
        # mints the id.
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init first before calling /generate/batch",
        )

    gen = SemanticModelGenerator()
    if header.schema_hash != gen.compute_schema_hash(schema):
        raise HTTPException(
            status_code=409,
            detail=(
                "Schema changed during generation — regenerate (keeps your edits); "
                "Restart from scratch discards them"
            ),
        )
    known = {table_key(t) for t in schema.tables}
    unknown = [k for k in body.table_keys if k not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown table keys: {unknown}")

    llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)
    # ``_apply_privacy_to_schema`` already strips ``excluded_columns`` and the FKs on
    # them (including from a stale schema cache), so the strict call (no ``privacy``)
    # is defence in depth: no edge on a column outside the schema reaches the prompt.
    relationship_edges = edges_in_schema(gen._build_relationship_graph(schema), schema)

    # The reply's value mappings are kept only for columns whose values may reach the LLM.
    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    generated = await _run_llm_phase(
        gen.generate_tables(
            schema=schema,
            provider=llm_provider,
            model=configured_model or None,
            table_keys=body.table_keys,
            relationship_edges=relationship_edges,
            privacy=privacy,
        ),
        what=f"Batch {body.table_keys} generation",
        connection_id=connection_id,
        provider=provider_name,
    )

    # The generator already grounds its output to the batch; this guards against a
    # future generator change writing (and stamping) a table this batch never named.
    requested = set(body.table_keys)
    batch_tables = {k: v for k, v in generated.items() if k in requested}
    omitted = [k for k in body.table_keys if k not in batch_tables]
    if omitted:
        # Not stamped, so they stay pending: reported to the client (which re-queues
        # them once) and named by /generate/globals if still unannotated.
        logger.warning(
            "generate_semantic_batch: the LLM returned no annotation for %s; they stay "
            "pending: connection=%s",
            omitted,
            connection_id,
        )

    # Take the connection row lock (FOR NO KEY UPDATE) before reconciling. It is what
    # serialises concurrent batches, and advance_progress's recount of this run's
    # stamped tables is only correct because no other batch stamps rows between
    # its count and its write.
    await _lock_generation(connection_id, db, "batch processing")
    current = await semantic_store.load_header(db, connection_id)
    if current is None or current.generation_status != GenerationStatus.TABLES_PARTIAL:
        # Another request (/generate/globals, or a delete) changed status while
        # this batch awaited the LLM. Discard rather than corrupt.
        await db.rollback()
        status_now = current.generation_status if current else "deleted"
        raise HTTPException(
            status_code=409,
            detail=(
                "Semantic generation state changed during batch processing "
                f"(status is now '{status_now}')"
            ),
        )
    if current.generation_run_id != run_id:
        # A fresh /generate/init restarted the run while this batch awaited the LLM.
        # Reconciling now would stamp these tables with the *new* run id, and a later
        # resume of that run would skip them although it never generated them.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Generation was restarted while this batch ran — its results were discarded",
        )

    try:
        # expected_run_id re-checks the run at the write itself (belt and braces with
        # the check above, which init's lock already makes sufficient).
        await semantic_store.reconcile_tables(
            db, connection_id, batch_tables, updated_by=user_id, expected_run_id=run_id
        )
        # The model has now seen every live column of these tables the privacy policy
        # leaves visible, annotated or not: one the LLM omitted is not a "new column"
        # to the next drift check. A column the policy hides is seen too when the
        # reconciled rows hold it, under its real name (``_held_column_names``).
        stored = await semantic_store.load_column_names(db, connection_id, set(batch_tables))
        await semantic_store.update_known_columns(
            db,
            connection_id,
            _baseline_entries(
                {}, {key: stored.get(key, ()) for key in batch_tables}, schema, privacy
            ),
        )
        if batch_tables:
            # The batch may have rewritten annotations the cached answers were written
            # against: purge them and the table embeddings, as every other semantic writer
            # does. The schema cache stays: the run reads it for every batch, and a batch
            # does not change the schema. A batch the LLM answered nothing for wrote no
            # annotation: nothing to purge.
            await _invalidate_connection_caches(connection_id, db, keep_schema=True)
        progress = await semantic_store.advance_progress(db, connection_id)
    except semantic_store.SemanticStoreConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Generation was restarted while this batch ran — {exc}",
        ) from exc
    except ValueError as exc:
        # An over-long identity key or otherwise unstorable value; the client's fault
        # as far as the store can tell, and never a 500.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return GenerateBatchResponse(
        connection_id=connection_id,
        table_keys=list(body.table_keys),
        tables_done=progress.tables_done if progress else 0,
        tables_total=progress.tables_total if progress else 0,
        batch_size=progress.batch_size if progress else GENERATION_BATCH_SIZE,
        omitted_table_keys=omitted,
    )


@router.post("/{connection_id}/semantic/generate/globals", response_model=SemanticModel)
@limiter.limit("10/minute")
async def generate_semantic_globals(
    request: Request,
    connection_id: str,
    provider: str = "claude",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Phase 3 of phased generation: cross-table metrics, segments, derived columns.

    Finalises the model (``generation_status = "complete"``) over whatever
    tables are stored, which is what makes "Finish with what's generated" work.
    Tables and columns that are no longer in the schema are pruned here
    (``prune_to_schema``), as they are by ``/apply-drift``: a regenerate never
    blanks the model, so this is where a full run drops them. A column key the drift
    renames (a near-miss spelling of a live column the model has seen, which this run
    did not annotate under its real name) is renamed first, as ``/apply-drift`` renames
    it, so its annotation and hand edits are kept rather than pruned.
    """
    user_id: str = current_user.id
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    # The header first: the run id lives there, not on the model. It is what the
    # post-lock check compares against to tell a restart from a plain re-finalise.
    header = await semantic_store.load_header(db, connection_id)
    if header is None or header.generation_status not in (
        GenerationStatus.TABLES_PARTIAL,
        GenerationStatus.COMPLETE,
    ):
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init and /generate/batch before /generate/globals",
        )
    partial_model = await semantic_store.load_model(db, connection_id)
    if partial_model is None:  # deleted between the two reads
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init and /generate/batch before /generate/globals",
        )
    run_id = header.generation_run_id

    llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)

    gen = SemanticModelGenerator()
    # ``_apply_privacy_to_schema`` already strips ``excluded_columns`` and the FKs on
    # them (including from a stale schema cache), so the strict call (no ``privacy``)
    # is defence in depth: no edge on a column outside the schema reaches the prompt
    # or is saved as a fresh edge.
    relationship_edges = edges_in_schema(gen._build_relationship_graph(schema), schema)
    # ``schema`` has the policy applied (excluded schemas, tables and columns
    # removed); pruning must not read that absence as drift.
    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None

    metrics, derived_columns, segments = await _run_llm_phase(
        gen.generate_globals(
            schema=schema,
            provider=llm_provider,
            model=configured_model or None,
            # Pruned so the LLM is not invited to write metrics over gone tables.
            # Strictly, without ``privacy``: what the policy hides is kept in the
            # stored model below but must never reach the LLM.
            # log=False: the post-lock prune below warns about the same drops.
            all_tables=prune_to_schema(partial_model.tables, schema, log=False),
            relationship_edges=relationship_edges,
        ),
        what="Globals generation",
        connection_id=connection_id,
        provider=provider_name,
    )

    # Re-read under a row lock so this doesn't clobber a concurrent batch write.
    await _lock_generation(connection_id, db, "globals generation")
    # One pass over the rows under the lock: the model and its marks together, after
    # the lock rather than before it, so a mark added between a pre-lock snapshot and
    # the save is not lost, and no concurrent writer waits on a second scan.
    current_model, snapshot = await semantic_store.load_model_with_provenance(db, connection_id)
    if current_model is None:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Semantic model was deleted during globals generation"
        )
    if current_model.generation_status not in (
        GenerationStatus.TABLES_PARTIAL,
        GenerationStatus.COMPLETE,
    ):
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Semantic generation state changed during globals generation "
                f"(status is now '{current_model.generation_status}')"
            ),
        )
    current_header = await semantic_store.load_header(db, connection_id)
    if (
        current_header is not None
        and current_header.generation_status == GenerationStatus.TABLES_PARTIAL
        and current_header.generation_run_id != run_id
    ):
        # A fresh /generate/init started a new run while the globals LLM call was in
        # flight. Finalising now would mark that run complete before its batches ran
        # (they would then 409) and pass the old run's globals off as the new run's.
        # A re-finalise of a complete model has no run id before or after, so it is
        # not caught here. Nor is a *resume* init during globals: it keeps the same
        # run id. Globals then finalises, and the resumed loop's batches get 409
        # (status moved on), which is acceptable.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Generation was restarted while globals ran — its results were discarded; "
                "finish the new run"
            ),
        )
    # A near-miss key of a live column the baseline has seen, held while the table stores
    # no column of that name, is renamed to it with its annotation, marks and references:
    # ``compute_drift``'s rule, read through the same effective baseline as Check Drift and
    # Apply Drift. A column this run annotated under its real name is stored, so its
    # near-miss key is a duplicate the prune below drops.
    known = await semantic_store.load_known_columns(db, connection_id)
    renames = compute_drift(
        schema,
        current_model,
        privacy,
        _effective_known_columns(known, current_model.tables, schema, privacy),
    ).renamed_columns
    tables, relationships, marks, protected = rename_columns(
        current_model.tables,
        current_model.relationships,
        snapshot.marks,
        snapshot.protected,
        renames,
    )
    # A hand edit that names a column the table no longer has would be written back
    # over the pruned row as a dangling reference, with a mark that stops every later
    # regenerate from replacing it. Drop the dangling names first (a list keeps its live
    # entries and its mark; a scalar or an emptied list is cleared), and tell the user.
    provenance, protected, edit_notes = drop_dangling_edits(marks, protected, schema, privacy)
    # The two metric checks run below over the final metrics and tables: a user-edited
    # metric replaces the LLM's same-named one, so the LLM's set is not what is checked.
    gen_warnings = list(edit_notes)

    # One bad LLM item must not fail the whole finalisation in save_model.
    metrics, derived_columns, segments = _drop_unstorable_items(
        metrics, derived_columns, segments, connection_id
    )

    # A user-authored metric, segment or derived column carries provenance; the
    # generator's fresh set must not replace it. Name-keyed, like the store.
    edited_metrics = set(provenance.get("metric", {}))
    edited_items = set(provenance.get("global_item", {}))
    metrics = [m for m in metrics if m.name not in edited_metrics] + [
        m for m in current_model.business_metrics if m.name in edited_metrics
    ]
    segments = [s for s in segments if f"segment:{s.name}" not in edited_items] + [
        s for s in current_model.segments if f"segment:{s.name}" in edited_items
    ]
    derived_columns = [
        d for d in derived_columns if f"derived_column:{d.name}" not in edited_items
    ] + [d for d in current_model.derived_columns if f"derived_column:{d.name}" in edited_items]

    # ``current_model.tables`` (with the renames above), not the pre-LLM snapshot: a batch
    # that landed during the globals call is in the stored rows. Pruned by schema
    # membership, not by run stamp, so finishing a partial run keeps every real table it
    # annotated. A table the connection's role cannot read is absent from ``schema`` too,
    # but it exists: ``keep_unreadable`` stops a narrower role from deleting its
    # annotations.
    pruned = prune_to_schema(tables, schema, privacy, keep_unreadable=True)
    # Joins are no longer generated: every stored join stays, a hand-written or edited one
    # (marked) unconditionally and a generated one unless it names a table that left the
    # database. Not "has no annotation": a live table the run skipped, or the role cannot
    # read, or the policy hides, still exists. Check Drift cannot report a gone table
    # once this regenerate has pruned it (it reads removed tables from the stored model),
    # so Apply Drift would never remove the join.
    existing_tables = sorted(
        {
            *pruned,
            *(table_key(t) for t in schema.tables),
            *schema.unreadable_tables,
        }
    )
    joins = [
        j
        for j in current_model.common_joins
        if f"common_join:{j.join_pattern}" in edited_items
        or not _names_gone_table(j.tables, existing_tables, privacy)
    ]
    gen_warnings = [
        *gen._validate_metrics_time_columns(metrics, schema),
        *gen._validate_metric_filter_values(metrics, pruned),
        *gen_warnings,
    ]
    # Every table of the schema should have a row by now. One without is a table the
    # LLM returned nothing for (twice, after the client's re-queue), one a "Finish
    # with what's generated" never reached, or one created since the run started; the
    # model is complete either way, so say so.
    unannotated = sorted(key for key in (table_key(t) for t in schema.tables) if key not in pruned)
    if unannotated:
        sample = ", ".join(unannotated[:5]) + (", …" if len(unannotated) > 5 else "")
        gen_warnings = [
            *gen_warnings,
            f"{len(unannotated)} table(s) have no annotation ({sample}): the LLM returned "
            "nothing for them, the run was finished before reaching them, or they are new "
            "since the run started. Regenerate to annotate them.",
        ]
    # Every catalog edge over the annotated tables, marked edges carried; an unmarked
    # edge to a hidden endpoint is not carried and returns from the FK once un-hidden
    # (``_rebuild_relationships``; "Finalisation" in semantic-model-generation.md).
    pruned_relationships = _rebuild_relationships(
        relationships,
        relationship_edges,
        provenance,
        schema,
        privacy,
        render_tables=set(pruned),
        keep_hidden_endpoints=False,
        new_tables=None,
    )

    dialect = conn.source_type
    final = current_model.model_copy(
        update={
            "tables": pruned,
            "business_metrics": metrics,
            "common_joins": joins,
            "derived_columns": derived_columns,
            "segments": segments,
            "relationships": pruned_relationships,
            "generated_at": datetime.now(UTC).isoformat(),
            "is_user_reviewed": False,
            "generation_model": configured_model or provider_name,
            "generation_status": GenerationStatus.COMPLETE,
            "generation_progress": None,
            "generation_warnings": gen_warnings,
            **_rebuilt_fields(gen, schema, dialect),
        }
    )

    try:
        stored = await semantic_store.save_model(
            db,
            connection_id,
            final,
            updated_by=user_id,
            source="generation",
            provenance=provenance,
            protected=protected,
        )
    except ValueError as exc:
        # An over-long identity value the drops above did not cover; never a 500.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Keep the baseline in step with what was just stored. A stored table with no entry
    # yet (stored before the baseline existed, and not annotated by this run: never
    # sent, or the LLM left it out) is seeded with its live columns, so a column added
    # since it was annotated is not reported; one with an entry keeps what it had seen
    # that is still live, so a column that left the database is new again if it returns.
    # The entries of tables the save removed are pruned. A hidden or unreadable table is
    # not in the live schema, so its entry is not rewritten, and the prune keeps it
    # because the saved model still holds the table. A column the policy hides is absent
    # from the live columns too, so unlike a table it leaves the entry, unless the model
    # stores it (below): once un-hidden, one it never annotated is reported new and the
    # next apply annotates it.
    live_now = _live_columns(schema, set(stored.tables))
    # What the model stores it has seen, however the column got there (a hand-added
    # annotation, or a near-miss key renamed above, included), hidden columns under
    # their real names: ``_baseline_entries`` adds them, so the baseline is a superset of
    # what the model stores.
    wanted = _baseline_entries(
        {key: set(known[key]) & set(names) for key, names in live_now.items() if key in known},
        {key: table.columns for key, table in stored.tables.items()},
        schema,
        privacy,
    )
    await semantic_store.update_known_columns(
        db, connection_id, _baseline_changes(known, wanted), prune=True
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    # What was stored, not ``final``: the store fits values (NULs, VARCHAR lengths)
    # and dedupes name-keyed items, and returns the result.
    return stored
