# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model router — get, generate, update, and delete per-connection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
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
from ..semantic.drift import DriftApplySummary, compute_drift, summarize_model_change
from ..semantic.generator import (
    SemanticModelGenerator,
    drop_dangling_edits,
    edges_in_schema,
    names_any_table,
    prune_to_schema,
)
from ..semantic.models import (
    GENERATION_BATCH_SIZE,
    BusinessMetric,
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
    in the report; with no cache either it fails.

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

    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    if privacy:
        schema = _apply_privacy_to_schema(schema, privacy)

    # The store keeps what the policy hides; that absence from ``schema`` is not drift.
    # One diff for the warnings and the flag, the same diff apply-drift acts on.
    drift = compute_drift(schema, stored_model, privacy)
    warnings = SemanticModelGenerator().detect_drift(schema, stored_model, privacy, drift=drift)
    # Persist the refreshed cache (a no-op on the fallback path).
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
        rebuilt = [
            e
            for e in rebuilt
            if key(e) in had or e.from_table in new_tables or e.to_table in new_tables
        ]
    if keep_hidden_endpoints:
        strict = {key(e) for e in edges_in_schema(survivors, schema)}
        have = {key(e) for e in rebuilt}
        rebuilt += [e for e in survivors if (k := key(e)) not in strict and k not in have]
    return rebuilt


# One apply is one HTTP request, and the proxy gives it 900s (``frontend/nginx.conf``).
# Each LLM phase may take up to ``_GENERATION_TIMEOUT_SECONDS`` (180s): three table
# batches and the globals pass are four calls, 720s, which leaves room for the live
# introspection and the save. New tables past the cap are not sent; the next drift
# check names them again and the next apply annotates them.
_DRIFT_MAX_NEW_TABLES = 3 * GENERATION_BATCH_SIZE


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


async def _new_tables_in_send_order(
    db: AsyncSession, connection_id: str, new_keys: set[str]
) -> list[str]:
    """``new_keys`` in the order the apply sends them: sorted, except that the tables
    the newest logged apply reported in ``tables_not_annotated`` go last.

    Only the first ``_DRIFT_MAX_NEW_TABLES`` are sent. In plain sorted order, tables
    the LLM keeps returning nothing for would hold the front of the list on every
    apply and starve the ones past the cap; moved last, they are retried once the
    others are in. The log is read only when the cap applies. An entry whose summary
    is not a dict, or whose field is missing or not a list of strings, orders nothing.
    """
    ordered = sorted(new_keys)
    if len(ordered) <= _DRIFT_MAX_NEW_TABLES:
        return ordered
    retry: set[str] = set()
    for record in await semantic_store.list_drift_applies(db, connection_id, limit=1):
        field = (
            record.summary.get("tables_not_annotated") if isinstance(record.summary, dict) else None
        )
        if isinstance(field, list):
            retry = {key for key in field if isinstance(key, str)}
    return [k for k in ordered if k not in retry] + [k for k in ordered if k in retry]


class _DriftAssembly(NamedTuple):
    """What ``_drift_applied_model`` returns.

    ``removed_items`` names, by the store's kinds (``metric``, ``common_join``,
    ``derived_column``, ``segment``), the items taken out because they named a
    removed table, for ``summarize_model_change(removed_items=...)``.
    ``llm_warnings`` are the generator's metric checks over the LLM metrics actually
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
    llm_items: tuple[list[BusinessMetric], list[CommonJoin], list[DerivedColumn], list[Segment]],
    connection_id: str,
    log: bool = True,
) -> _DriftAssembly:
    """*current_model* with *drift* applied, and what ``save_model`` needs to store it.

    No session: the apply runs this under the row lock, with the drift recomputed
    against the re-read model, and saves the result; the dry run runs it on an
    unlocked read, with no new tables and no LLM items, and only diffs the result — so
    a preview is computed by the code that applies it. The dry run passes
    ``log=False``: the hand-edit trims and near-miss drops it previews did not happen,
    so they must not be logged as if they had.

    Returns a ``_DriftAssembly``; its ``notes`` are ``drop_dangling_edits``' notes on
    the hand edits it trimmed. The fields a generation sets (``generated_at``,
    ``generation_model``, ``is_user_reviewed``, ``generation_warnings``) are left to
    the caller.
    """
    provenance, protected, edit_notes = drop_dangling_edits(
        snapshot.marks, snapshot.protected, schema, privacy, log=log
    )

    # Removed tables and columns leave here; hidden and unreadable ones stay.
    pruned = prune_to_schema(current_model.tables, schema, privacy, log=log, keep_unreadable=True)
    merged_tables = {**pruned, **new_tables}
    metrics, joins, derived_columns, segments = _without_removed_tables(
        current_model, drift.removed_table_keys
    )
    removed_items = {
        "metric": {m.name for m in current_model.business_metrics} - {m.name for m in metrics},
        "common_join": {j.join_pattern for j in current_model.common_joins}
        - {j.join_pattern for j in joins},
        "derived_column": {d.name for d in current_model.derived_columns}
        - {d.name for d in derived_columns},
        "segment": {s.name for s in current_model.segments} - {s.name for s in segments},
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
        llm_metrics, llm_joins, llm_derived, llm_segments = llm_items
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
        llm_joins = [j for j in llm_joins if over_new(j.tables)]
        llm_derived = [d for d in llm_derived if over_new(d.base_tables)]
        llm_segments = [s for s in llm_segments if over_new(s.applicable_tables)]
        # Additive: an existing item (hand-written or generated) is never replaced.
        llm_metrics, llm_joins, llm_derived, llm_segments = _drop_unstorable_items(
            llm_metrics, llm_joins, llm_derived, llm_segments, connection_id
        )
        have_metrics = {m.name for m in metrics}
        have_joins = {j.join_pattern for j in joins}
        have_derived = {d.name for d in derived_columns}
        have_segments = {s.name for s in segments}
        added_metrics = [m for m in llm_metrics if m.name not in have_metrics]
        metrics = metrics + added_metrics
        # The globals pass's own warnings may name metrics dropped above, and so point
        # at the user's same-named one; the same check over what was added cannot.
        llm_warnings = gen._validate_metrics_time_columns(added_metrics, schema)
        joins = joins + [j for j in llm_joins if j.join_pattern not in have_joins]
        derived_columns = derived_columns + [d for d in llm_derived if d.name not in have_derived]
        segments = segments + [s for s in llm_segments if s.name not in have_segments]

    relationships = _rebuild_relationships(
        current_model.relationships,
        relationship_edges,
        provenance,
        schema,
        privacy,
        render_tables=set(merged_tables),
        keep_hidden_endpoints=True,
        new_tables=set(new_tables),
    )

    model = current_model.model_copy(
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
    - Relationships, time expressions, notes and the schema hash are recomputed.

    No LLM call when nothing was added. The schema is always a **live**
    introspection: acting on a cached one could delete what still exists, so an
    unreachable source is a 400. What the privacy policy hides and what the
    connection's role cannot read are never treated as removed (``compute_drift``).
    A run in progress or interrupted (``tables_partial``) is a 409: pruning under it
    would race its batches.

    ``dry_run=true`` is the preview: the same assembly (``_drift_applied_model``) on
    the unlocked read the drift was computed from, no LLM call, nothing written but
    the refreshed schema cache (which every call commits). New tables are listed by
    name in ``tables_added`` — exactly the ones the apply would send, in its order
    (``_new_tables_in_send_order``); relationships to them and the LLM's items over
    them appear only on apply. The preview's ``warnings`` are the hand-edit notes and,
    past ``_DRIFT_MAX_NEW_TABLES``, the deferred-tables warning.

    Answers with the stored model (on a dry run, the model as it is) and
    ``summarize_model_change`` of the result against the model it was built from —
    every cascade named. Every apply that writes also logs that summary in its save's
    transaction (``semantic_store.record_drift_apply``); a dry run and an apply with no
    drift to act on (before or under the lock) write, purge and log nothing. An apply
    whose result equals the current model (the LLM returned nothing for the only new
    tables) saves and purges nothing, but is logged when it skipped tables, because the
    next apply reads that entry to retry them last.
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
    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    if privacy:
        schema = _apply_privacy_to_schema(schema, privacy)

    drift = compute_drift(schema, stored_model, privacy)
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

    if dry_run:
        # No lock: nothing is written, and a write landing after the read is re-diffed
        # by the apply itself. The provenance is what ``drop_dangling_edits`` needs to
        # name the hand edits the apply would trim.
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
            llm_items=([], [], [], []),
            connection_id=connection_id,
            log=False,
        )
        notes = list(preview.notes)
        if deferred:
            notes.append(_deferred_tables_warning(deferred, preview=True))
        summary = summarize_model_change(
            stored_model, preview.model, warnings=notes, removed_items=preview.removed_items
        )
        return ApplyDriftResponse(
            semantic_model=stored_model,
            summary=summary.model_copy(
                update={
                    "dry_run": True,
                    # The tables the apply would send, sorted for display like every list.
                    # ``changed`` follows: a new table alone is a change.
                    "tables_added": sorted(to_annotate),
                }
            ),
        )

    new_tables: dict[str, TableSemantic] = {}
    llm_items: tuple[list[BusinessMetric], list[CommonJoin], list[DerivedColumn], list[Segment]]
    llm_items = ([], [], [], [])
    configured_model: str | None = None
    provider_name: str | None = None
    if to_annotate:
        llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)
        for start in range(0, len(to_annotate), GENERATION_BATCH_SIZE):
            batch = to_annotate[start : start + GENERATION_BATCH_SIZE]
            generated = await _run_llm_phase(
                gen.generate_tables(
                    schema=schema,
                    provider=llm_provider,
                    model=configured_model or None,
                    table_keys=batch,
                    relationship_edges=relationship_edges,
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
            # Its warnings are dropped: ``_drift_applied_model`` re-runs the check over
            # the metrics it actually adds (``llm_warnings``).
            metrics, joins, derived, _, segments = await _run_llm_phase(
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
            llm_items = (metrics, joins, derived, segments)

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
    # The drift the apply acts on is the one against the model it saves over, not the
    # pre-lock read: a table a PUT added during the LLM calls that the database no
    # longer has leaves with its items, and a drift another tab applied meanwhile is
    # not applied — or logged — twice.
    drift = compute_drift(schema, current_model, privacy)
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
        llm_items=llm_items,
        connection_id=connection_id,
    )
    gen_warnings = [*assembly.llm_warnings, *assembly.notes]
    if deferred:
        gen_warnings.append(_deferred_tables_warning(deferred, preview=False))
    header_update: dict[str, Any] = {"generation_warnings": gen_warnings}
    if new_tables:
        # New LLM content: unreviewed, like after a generation run.
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
            warnings=gen_warnings,
            removed_items=assembly.removed_items,
        )

    summary = summary_of(final)
    if not summary.changed and not assembly.notes:
        # The LLM returned nothing for the only new tables: the model is as it was. (A
        # trimmed hand edit — ``notes`` — is a change the lists do not show, so it is
        # saved.) Not saved or purged. Skipped tables are still logged, and the commit
        # releases the lock: the next apply orders its new tables by the newest log
        # entry's ``tables_not_annotated`` (``_new_tables_in_send_order``), so without
        # the entry the tables the LLM keeps skipping would hold the front of the list
        # and starve the rest.
        if summary.tables_not_annotated:
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


def _drop_unstorable_items(
    metrics: list[BusinessMetric],
    joins: list[CommonJoin],
    derived: list[DerivedColumn],
    segments: list[Segment],
    connection_id: str,
) -> tuple[list[BusinessMetric], list[CommonJoin], list[DerivedColumn], list[Segment]]:
    """``_drop_unstorable`` over the four kinds of LLM global item, keyed as the store
    keys them (metrics, derived columns and segments by name, joins by pattern)."""
    return (
        _drop_unstorable(metrics, "metric", lambda m: m.name, connection_id),
        _drop_unstorable(joins, "common_join", lambda j: j.join_pattern, connection_id),
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

    ``generate_tables`` and ``generate_globals`` wrap provider errors in
    ``ValueError``, keeping the providers' ``[RATE_LIMIT]`` marker in the message.

    - Timeout: 504, logged as a warning.
    - ``ValueError`` carrying ``[RATE_LIMIT]``: 429, so the page's retry-with-backoff
      sees it. Logged as a warning without a traceback: throttles are expected and
      retried. (A provider's exhausted quota carries ``[QUOTA_EXCEEDED]`` instead
      and is a 422: waiting does not fix it.)
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
        logger.exception("%s failed: connection=%s provider=%s", what, connection_id, provider)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("%s failed: connection=%s provider=%s", what, connection_id, provider)
        raise HTTPException(status_code=500, detail=str(exc) or f"{what} failed") from exc


async def _lock_generation(connection_id: str, db: AsyncSession, during: str) -> None:
    """Take the connection row lock (``FOR UPDATE``) the generation routes serialise on.

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
    schema, _conn = await _load_schema_for_connection(connection_id, current_user, db)

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

    generated = await _run_llm_phase(
        gen.generate_tables(
            schema=schema,
            provider=llm_provider,
            model=configured_model or None,
            table_keys=body.table_keys,
            relationship_edges=relationship_edges,
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

    # Take the connection row lock (FOR UPDATE) before reconciling. It is what
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
    # No cache invalidation per batch; /generate/globals purges once at the end.
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
    """Phase 3 of phased generation: cross-table metrics, joins, derived columns.

    Finalises the model (``generation_status = "complete"``) over whatever
    tables are stored, which is what makes "Finish with what's generated" work.
    Tables and columns that are no longer in the schema are pruned here
    (``prune_to_schema``), as they are by ``/apply-drift``: a regenerate never
    blanks the model, so this is where a full run drops them.
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

    metrics, joins, derived_columns, gen_warnings, segments = await _run_llm_phase(
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
    # A hand edit that names a column the table no longer has would be written back
    # over the pruned row as a dangling reference, with a mark that stops every later
    # regenerate from replacing it. Drop the dangling names first (a list keeps its live
    # entries and its mark; a scalar or an emptied list is cleared), and tell the user.
    provenance, protected, edit_notes = drop_dangling_edits(
        snapshot.marks, snapshot.protected, schema, privacy
    )
    gen_warnings = [*gen_warnings, *edit_notes]

    # One bad LLM item must not fail the whole finalisation in save_model.
    metrics, joins, derived_columns, segments = _drop_unstorable_items(
        metrics, joins, derived_columns, segments, connection_id
    )

    # A user-authored metric, segment, derived column or join carries provenance;
    # the generator's fresh set must not replace it. Name-keyed, like the store.
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
    joins = [j for j in joins if f"common_join:{j.join_pattern}" not in edited_items] + [
        j for j in current_model.common_joins if f"common_join:{j.join_pattern}" in edited_items
    ]

    # ``current_model.tables``, not the pre-LLM snapshot: a batch that landed during
    # the globals call is in the stored rows. Pruned by schema membership, not by run
    # stamp, so finishing a partial run keeps every real table it annotated. A table
    # the connection's role cannot read is absent from ``schema`` too, but it exists:
    # ``keep_unreadable`` stops a narrower role from deleting its annotations.
    pruned = prune_to_schema(current_model.tables, schema, privacy, keep_unreadable=True)
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
        current_model.relationships,
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
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    # What was stored, not ``final``: the store fits values (NULs, VARCHAR lengths)
    # and dedupes name-keyed items, and returns the result.
    return stored
