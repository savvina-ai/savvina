# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model router — get, generate, update, and delete per-connection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, null, select, update

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_active_user
from ..auth.limiter import limiter
from ..config import get_settings
from ..database import get_db
from ..datasources.models import PrivacySettings
from ..models.connection import Connection
from ..models.semantic_suggestion import SemanticSuggestion
from ..models.user import User
from ..models.user_schema_cache import UserSchemaCache
from ..providers._factory import resolve_provider_config
from ..schemas.pagination import PaginatedResponse
from ..schemas.semantic import (
    DriftReport,
    GenerateBatchRequest,
    GenerateBatchResponse,
    GenerateInitResponse,
    SemanticModelUpdate,
    SemanticSuggestionResponse,
)
from ..semantic import store as semantic_store
from ..semantic.generator import SemanticModelGenerator, edges_in_schema, prune_to_schema
from ..semantic.models import (
    GENERATION_BATCH_SIZE,
    GenerationStatus,
    SemanticModel,
    ValueMapping,
)
from ..services.schema_utils import (
    _apply_privacy_to_schema,
    _schema_from_dict,
    get_or_refresh_schema,
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
    await semantic_store.delete_model(db, connection_id, changed_by=current_user.id)
    # Also clear the legacy blob: the migration that drops it must find no connection
    # with a blob but no semantic_models row. ``null()``, not None: None on a JSON
    # column stores the JSON text 'null'.
    await db.execute(
        update(Connection).where(Connection.id == connection_id).values(semantic_model=null())
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()


# ── Drift detection ─────────────────────────────────────────────────────────────


@router.get("/{connection_id}/semantic/drift", response_model=DriftReport)
async def check_drift(
    connection_id: str,
    current_admin: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> DriftReport:
    """Check for schema drift since the semantic model was last generated.

    OVERHEAD: catalog-read — reads current schema from cache (no DB introspection),
    then diffs structure against stored semantic model. No statistical re-queries.
    """
    conn = await get_connection_or_404(connection_id, db)
    stored_model = await semantic_store.load_model(db, connection_id)
    if stored_model is None:
        raise HTTPException(status_code=404, detail="No semantic model — generate one first")

    usc_result = await db.execute(
        select(UserSchemaCache).where(
            UserSchemaCache.connection_id == connection_id,
            UserSchemaCache.user_id == current_admin.id,
        )
    )
    user_schema_cache = usc_result.scalar_one_or_none()
    if not user_schema_cache or not user_schema_cache.schema_cache:
        raise HTTPException(
            status_code=404,
            detail="No schema cached — run a schema refresh first",
        )

    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None
    schema = _schema_from_dict(user_schema_cache.schema_cache)
    if privacy:
        schema = _apply_privacy_to_schema(schema, privacy)

    # The store keeps what the policy hides; that absence from ``schema`` is not drift.
    warnings = SemanticModelGenerator().detect_drift(schema, stored_model, privacy)

    return DriftReport(
        connection_id=connection_id,
        warnings=warnings,
        warning_count=len(warnings),
        checked_at=datetime.now(UTC).isoformat(),
    )


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
    await get_connection_or_404(connection_id, db)

    sug_result = await db.execute(
        select(SemanticSuggestion).where(
            SemanticSuggestion.id == suggestion_id,
            SemanticSuggestion.connection_id == connection_id,
        )
    )
    suggestion = sug_result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    # Hold the connection row lock for the whole write: column rows carry no
    # optimistic version predicate, so the lock is what serialises concurrent writers.
    await lock_and_reread_connection(connection_id, db)
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


# Identity columns for metrics (and the store's other name-keyed rows) are VARCHAR(512).
# A generator item named longer than that would make ``save_model`` raise and fail the
# whole finalisation, so ``/generate/globals`` drops it instead.
_MAX_GENERATED_NAME_LENGTH = 512


class _Named(Protocol):
    name: str


def _drop_overlong_names[T: _Named](items: list[T], kind: str, connection_id: str) -> list[T]:
    """``items`` without the ones whose name (NULs stripped) is too long to store."""
    kept: list[T] = []
    for item in items:
        length = len(item.name.replace("\x00", ""))
        if length > _MAX_GENERATED_NAME_LENGTH:
            logger.warning(
                "generate_semantic_globals: dropped generated %s %r... (%d characters, "
                "limit %d): connection=%s",
                kind,
                item.name[:60],
                length,
                _MAX_GENERATED_NAME_LENGTH,
                connection_id,
            )
            continue
        kept.append(item)
    return kept


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
    """
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    gen = SemanticModelGenerator()
    prep = gen.prepare_generation(schema)
    all_keys = [f"{t.schema_name}.{t.name}" for t in schema.tables]
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
        # The hash, not a table count: a schema that changed shape while keeping
        # its cardinality would otherwise resume onto the wrong tables.
        if existing.schema_hash != schema_hash:
            await db.rollback()  # release the row lock
            raise HTTPException(
                status_code=409,
                detail=(
                    "Schema changed since the interrupted run — regenerate (keeps your "
                    "edits); Restart from scratch discards them"
                ),
            )
        pending = await semantic_store.pending_table_keys(db, connection_id, all_keys)
        resumed = True
        tables_done = len(all_keys) - len(pending)
    batch_count = max(1, -(-len(pending) // GENERATION_BATCH_SIZE))

    await semantic_store.begin_generation(
        db,
        connection_id,
        schema_hash=schema_hash,
        tables_total=prep["tables_total"],
        tables_done=tables_done,
        batch_size=GENERATION_BATCH_SIZE,
        source_dialect=conn.source_type,
        resume=resume,
    )
    await db.commit()

    return GenerateInitResponse(
        connection_id=connection_id,
        tables_total=prep["tables_total"],
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

    gen = SemanticModelGenerator()
    if header.schema_hash != gen.compute_schema_hash(schema):
        raise HTTPException(
            status_code=409,
            detail=(
                "Schema changed during generation — regenerate (keeps your edits); "
                "Restart from scratch discards them"
            ),
        )
    known = {f"{t.schema_name}.{t.name}" for t in schema.tables}
    unknown = [k for k in body.table_keys if k not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown table keys: {unknown}")

    llm_provider, configured_model, _ = await _resolve_provider(provider, db)
    # Strict (no ``privacy``): adapter FKs are filtered by table only, so an FK on an
    # excluded column would otherwise name that column in the prompt.
    relationship_edges = edges_in_schema(gen._build_relationship_graph(schema), schema)

    try:
        generated = await asyncio.wait_for(
            gen.generate_tables(
                schema=schema,
                provider=llm_provider,
                model=configured_model or None,
                table_keys=body.table_keys,
                relationship_edges=relationship_edges,
            ),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Batch {body.table_keys} generation timed out after "
            f"{_GENERATION_TIMEOUT_SECONDS}s",
        ) from exc
    except ValueError as exc:
        # generate_tables wraps provider errors in ValueError; keep the stack.
        logger.exception(
            "generate_semantic_batch: batch %s failed: connection=%s",
            body.table_keys,
            connection_id,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "generate_semantic_batch: batch %s failed: connection=%s",
            body.table_keys,
            connection_id,
        )
        detail = str(exc) if str(exc) else f"Batch {body.table_keys} generation failed"
        raise HTTPException(status_code=500, detail=detail) from exc

    # The generator already grounds its output to the batch; this guards against a
    # future generator change writing (and stamping) a table this batch never named.
    requested = set(body.table_keys)
    batch_tables = {k: v for k, v in generated.items() if k in requested}
    omitted = [k for k in body.table_keys if k not in batch_tables]
    if omitted:
        # Not stamped, so they stay pending and a resume retries them.
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
    (``prune_to_schema``): a regenerate never blanks the model and the CE has no
    apply-drift, so this is where they leave.
    """
    user_id: str = current_user.id
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    partial_model = await semantic_store.load_model(db, connection_id)
    if partial_model is None or partial_model.generation_status not in (
        GenerationStatus.TABLES_PARTIAL,
        GenerationStatus.COMPLETE,
    ):
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init and /generate/batch before /generate/globals",
        )

    llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)

    gen = SemanticModelGenerator()
    # Strict (no ``privacy``): adapter FKs are filtered by table only, so an FK on an
    # excluded column would otherwise reach the prompt and be saved as a fresh edge.
    relationship_edges = edges_in_schema(gen._build_relationship_graph(schema), schema)
    # ``schema`` has the policy applied (excluded schemas/tables removed, excluded
    # columns never introspected); pruning must not read that absence as drift.
    privacy = PrivacySettings.from_dict(conn.privacy_settings) if conn.privacy_settings else None

    try:
        metrics, joins, derived_columns, gen_warnings, segments = await asyncio.wait_for(
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
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Globals generation timed out after {_GENERATION_TIMEOUT_SECONDS}s",
        ) from exc
    except ValueError as exc:
        # generate_globals wraps provider errors in ValueError; keep the stack.
        logger.exception(
            "generate_semantic_globals failed: connection=%s provider=%s",
            connection_id,
            provider_name,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "generate_semantic_globals failed: connection=%s provider=%s",
            connection_id,
            provider_name,
        )
        detail = str(exc) if str(exc) else "Globals generation failed"
        raise HTTPException(status_code=500, detail=detail) from exc

    # Re-read under a row lock so this doesn't clobber a concurrent batch write.
    await _lock_generation(connection_id, db, "globals generation")
    current_model = await semantic_store.load_model(db, connection_id)
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
    # After the lock, not before: a mark added between a pre-lock snapshot and the
    # save would otherwise be lost.
    provenance = await semantic_store.provenance_map(db, connection_id)

    # One bad LLM item must not fail the whole finalisation in save_model.
    metrics = _drop_overlong_names(metrics, "metric", connection_id)
    segments = _drop_overlong_names(segments, "segment", connection_id)
    derived_columns = _drop_overlong_names(derived_columns, "derived column", connection_id)

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
    # stamp, so finishing a partial run keeps every real table it annotated.
    pruned = prune_to_schema(current_model.tables, schema, privacy)
    # A marked edge is carried forward by carry_edited_relationships unbounded, so drop
    # the ones whose endpoints left the database first — against the live schema, not
    # the annotated tables, so a user edge to a real but unannotated table survives.
    pruned_relationships = semantic_store.carry_edited_relationships(
        edges_in_schema(current_model.relationships, schema, privacy),
        relationship_edges,
        provenance,
        render_tables=set(pruned),
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
            "time_expressions": gen.build_time_expressions(dialect),
            "schema_hash": gen.compute_schema_hash(schema),
            "source_dialect": dialect,
            "generated_at": datetime.now(UTC).isoformat(),
            "is_user_reviewed": False,
            "generation_model": configured_model or provider_name,
            "generation_status": GenerationStatus.COMPLETE,
            "generation_progress": None,
            "generation_warnings": gen_warnings,
            "notes": gen._auto_generate_notes(schema),
        }
    )

    try:
        await semantic_store.save_model(
            db, connection_id, final, updated_by=user_id, source="generation", provenance=provenance
        )
    except ValueError as exc:
        # An over-long identity value the drops above did not cover; never a 500.
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    # What was stored, not ``final``: the store fits values (NULs, VARCHAR lengths)
    # and dedupes name-keyed items, so the two can differ.
    stored = await semantic_store.load_model(db, connection_id)
    return stored if stored is not None else final
