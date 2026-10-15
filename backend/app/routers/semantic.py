# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model router — get, generate, update, and delete per-connection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    DriftReport,
    GenerateBatchRequest,
    GenerateBatchResponse,
    GenerateInitResponse,
    SemanticModelUpdate,
    SemanticSuggestionResponse,
)
from ..semantic import store as semantic_store
from ..semantic.generator import (
    SemanticModelGenerator,
    drop_dangling_edits,
    edges_in_schema,
    prune_to_schema,
)
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
    # Rows and the legacy blob both go; the store owns that pairing.
    await semantic_store.delete_model(db, connection_id, changed_by=current_user.id)
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
                "generate_semantic_globals: dropped generated %s: %s: connection=%s",
                kind,
                why,
                connection_id,
            )
            continue
        kept.append(item)
    return kept


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
    (``prune_to_schema``): a regenerate never blanks the model and the CE has no
    apply-drift, so this is where they leave.
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
    metrics = _drop_unstorable(metrics, "metric", lambda m: m.name, connection_id)
    segments = _drop_unstorable(segments, "segment", lambda s: s.name, connection_id)
    derived_columns = _drop_unstorable(
        derived_columns, "derived_column", lambda d: d.name, connection_id
    )
    joins = _drop_unstorable(joins, "common_join", lambda j: j.join_pattern, connection_id)

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
    # A marked edge is carried forward by carry_edited_relationships unbounded, so drop
    # the ones whose endpoints left the database first — against the live schema, not
    # the annotated tables, so a user edge to a real but unannotated table survives.
    # Only *marked* edges are carried from ``current``: an unmarked FK edge to an
    # endpoint the policy hides is not regenerated while it is hidden
    # (``relationship_edges`` comes from the narrowed schema) and returns on the
    # first globals run after it is un-hidden, from the FK it is built from. Nothing
    # hand-written is lost — see "Finalisation" in semantic-model-generation.md. An
    # endpoint on a table the role cannot read follows the same rule.
    pruned_relationships = semantic_store.carry_edited_relationships(
        edges_in_schema(current_model.relationships, schema, privacy, keep_unreadable=True),
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
