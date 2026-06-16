# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model router — get, generate, update, and delete per-connection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update

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
    GenerateInitResponse,
    SemanticModelUpdate,
    SemanticSuggestionResponse,
)
from ..semantic.generator import _BATCH_SIZE, SemanticModelGenerator
from ..semantic.models import GenerationProgress, GenerationStatus, SemanticModel
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


def _advance_progress(
    progress: GenerationProgress | None, tables_done: int
) -> GenerationProgress | None:
    """Return *progress* with ``tables_done`` updated, or ``None`` if absent."""
    if progress is None:
        return None
    return progress.model_copy(update={"tables_done": tables_done})


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/{connection_id}/semantic", response_model=SemanticModel)
async def get_semantic_model(
    request: Request,
    connection_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    conn = await get_connection_or_404(connection_id, db)
    if not conn.semantic_model:
        raise HTTPException(
            status_code=404,
            detail="No semantic model — call /generate to auto-generate one",
        )
    model = SemanticModel.model_validate(conn.semantic_model)
    return cached_json_response(model.model_dump(mode="json"), request)


@router.put("/{connection_id}/semantic", response_model=SemanticModel)
async def update_semantic_model(
    connection_id: str,
    body: SemanticModelUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Partially update the semantic model — applies user refinements on top of
    the existing model without overwriting unmentioned keys.

    Top-level lists (``business_metrics``, ``common_joins`` …) are wholesale
    replacements when supplied. ``tables`` is the only section that deep-merges
    per-column, preserving v2 metadata (``semantic_type``, ``cardinality`` …)
    that the frontend doesn't always echo back.
    """
    conn = await lock_and_reread_connection(connection_id, db)
    existing_dict: dict = dict(conn.semantic_model) if conn.semantic_model else {}
    existing_model = (
        SemanticModel.model_validate(existing_dict) if existing_dict else SemanticModel()
    )

    # ── Per-column deep merge (Pydantic's model_copy(update=…) is shallow) ────
    merged_tables = dict(existing_model.tables)
    if body.tables is not None:
        for tbl_key, tbl_update in body.tables.items():
            existing_tbl = merged_tables.get(tbl_key)
            tbl_patch = tbl_update.model_dump(exclude_unset=True, exclude={"columns"})
            if existing_tbl is None:
                # New table: require display_name from the patch (Pydantic
                # validation will catch its absence).
                base_tbl_data = tbl_patch
            else:
                base_tbl_data = {**existing_tbl.model_dump(mode="json"), **tbl_patch}

            # Per-column deep merge
            existing_cols = existing_tbl.columns if existing_tbl else {}
            merged_cols = {k: v.model_dump(mode="json") for k, v in existing_cols.items()}
            if tbl_update.columns is not None:
                for col_name, col_update in tbl_update.columns.items():
                    col_patch = col_update.model_dump(exclude_unset=True)
                    merged_cols[col_name] = {**merged_cols.get(col_name, {}), **col_patch}
            base_tbl_data["columns"] = merged_cols
            merged_tables[tbl_key] = base_tbl_data

    # ── Shallow updates for the remaining sections ───────────────────────────
    update_data: dict = body.model_dump(exclude_unset=True, exclude={"tables"})
    # ``model_copy(update=…)`` accepts already-validated values; tables go in as
    # raw dicts because we just deep-merged them. Round-trip through
    # ``model_validate`` to surface any shape errors before persisting.
    merged_dict = {
        **existing_model.model_dump(mode="json"),
        **{k: v for k, v in update_data.items()},
        "tables": merged_tables,
    }
    new_model = SemanticModel.model_validate(merged_dict)

    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(
            semantic_model=new_model.model_dump(mode="json"),
            semantic_model_updated_at=datetime.now(UTC),
        )
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    return new_model


@router.delete("/{connection_id}/semantic", status_code=status.HTTP_204_NO_CONTENT)
async def delete_semantic_model(
    connection_id: str,
    _current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await get_connection_or_404(connection_id, db)
    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(semantic_model=None, semantic_model_updated_at=None)
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
    if not conn.semantic_model:
        raise HTTPException(
            status_code=404,
            detail="No semantic model — generate one first",
        )

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
    stored_model = SemanticModel.model_validate(conn.semantic_model)

    warnings = SemanticModelGenerator().detect_drift(schema, stored_model)

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
    conn = await get_connection_or_404(connection_id, db)

    sug_result = await db.execute(
        select(SemanticSuggestion).where(
            SemanticSuggestion.id == suggestion_id,
            SemanticSuggestion.connection_id == connection_id,
        )
    )
    suggestion = sug_result.scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    # Re-read under a row lock right before merging so this doesn't clobber a
    # concurrent write (e.g. a batch-generation call) made since the read above.
    conn = await lock_and_reread_connection(connection_id, db)
    if not conn.semantic_model:
        raise HTTPException(status_code=404, detail="No semantic model to update")

    model_dict: dict = dict(conn.semantic_model)
    tables: dict = dict(model_dict.get("tables", {}))

    table_data: dict = dict(tables.get(suggestion.table_key, {}))
    correction_type = suggestion.correction_type
    value = suggestion.value

    if correction_type == "add_value_mapping":
        columns: dict = dict(table_data.get("columns", {}))
        field_data: dict = dict(columns.get(suggestion.field, {}))
        mappings: list = list(field_data.get("value_mappings", []))
        mappings.append(value)
        field_data["value_mappings"] = mappings
        columns[suggestion.field] = field_data
        table_data["columns"] = columns

    elif correction_type == "update_filter":
        filters: list = list(table_data.get("default_filters", []))
        new_filter = value.get("filter", "")
        if new_filter and new_filter not in filters:
            filters.append(new_filter)
        table_data["default_filters"] = filters

    elif correction_type == "update_description":
        target = value.get("target", "table")
        if target == "column":
            columns = dict(table_data.get("columns", {}))
            field_data = dict(columns.get(suggestion.field, {}))
            field_data["description"] = value.get("description", "")
            columns[suggestion.field] = field_data
            table_data["columns"] = columns
        else:
            table_data["description"] = value.get("description", "")

    tables[suggestion.table_key] = table_data
    model_dict["tables"] = tables

    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(
            semantic_model=model_dict,
            semantic_model_updated_at=datetime.now(UTC),
        )
    )
    await db.execute(
        update(SemanticSuggestion)
        .where(SemanticSuggestion.id == suggestion_id)
        .values(is_applied=True)
    )
    await _invalidate_connection_caches(connection_id, db)
    await db.commit()
    return SemanticModel.model_validate(model_dict)


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


@router.post("/{connection_id}/semantic/generate/init", response_model=GenerateInitResponse)
@limiter.limit("10/minute")
async def generate_semantic_init(
    request: Request,
    connection_id: str,
    provider: str = "claude",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GenerateInitResponse:
    """Phase 1 of phased generation: compute batch plan and initialise partial model in DB.

    No LLM call is made. Returns the batch count so the frontend can loop over
    ``/generate/batch?batch_idx=N`` calls.
    """
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    gen = SemanticModelGenerator()
    prep = gen.prepare_generation(schema)

    # Build initial partial model — preserve any existing non-table fields by
    # round-tripping through ``SemanticModel`` so unknown / stale keys from
    # earlier shapes are silently dropped (extra="ignore").
    existing_dict: dict = dict(conn.semantic_model) if conn.semantic_model else {}
    existing_model = (
        SemanticModel.model_validate(existing_dict) if existing_dict else SemanticModel()
    )
    partial_model = existing_model.model_copy(
        update={
            "tables": {},
            "generation_status": GenerationStatus.TABLES_PARTIAL,
            "generation_progress": GenerationProgress(
                tables_done=0,
                tables_total=prep["tables_total"],
                batch_size=_BATCH_SIZE,
            ),
        }
    )

    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(
            semantic_model=partial_model.model_dump(mode="json"),
            semantic_model_updated_at=datetime.now(UTC),
        )
    )
    await db.commit()

    return GenerateInitResponse(
        connection_id=connection_id,
        tables_total=prep["tables_total"],
        batch_count=prep["batch_count"],
        batch_size=prep["batch_size"],
    )


@router.post("/{connection_id}/semantic/generate/batch", response_model=SemanticModel)
# CONCURRENCY=2 in the frontend worker pool roughly doubles request density
# against this limit; retry/backoff in runBatchWithRetry absorbs occasional
# 429s. Revisit if logs show frequent 429s on large schemas.
@limiter.limit("20/minute")
async def generate_semantic_batch(
    request: Request,
    connection_id: str,
    batch_idx: int = 0,
    provider: str = "claude",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Phase 2 of phased generation: annotate one batch of tables via LLM.

    Call repeatedly with ``batch_idx`` from 0 to ``batch_count - 1`` (returned
    by ``/generate/init``).  Each call merges results into the partial model
    persisted in DB and increments ``generation_progress.tables_done``.
    """
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    if not conn.semantic_model:
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init first before calling /generate/batch",
        )
    partial_model = SemanticModel.model_validate(conn.semantic_model)
    if partial_model.generation_status != GenerationStatus.TABLES_PARTIAL:
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init first before calling /generate/batch",
        )

    progress = partial_model.generation_progress
    tables_total: int = progress.tables_total if progress else len(schema.tables)
    batch_count = max(1, -(-tables_total // _BATCH_SIZE))
    if batch_idx >= batch_count:
        raise HTTPException(
            status_code=400,
            detail=f"batch_idx {batch_idx} out of range (batch_count={batch_count})",
        )

    llm_provider, configured_model, _ = await _resolve_provider(provider, db)

    gen = SemanticModelGenerator()
    # Rebuild relationship edges from schema — pure computation, zero overhead
    relationship_edges = gen._build_relationship_graph(schema)

    try:
        batch_tables = await asyncio.wait_for(
            gen.generate_table_batch(
                schema=schema,
                provider=llm_provider,
                model=configured_model or None,
                batch_idx=batch_idx,
                relationship_edges=relationship_edges,
            ),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Batch {batch_idx} generation timed out after {_GENERATION_TIMEOUT_SECONDS}s",
        ) from exc
    except ValueError as exc:
        logger.error(
            "generate_semantic_batch: batch %d failed: connection=%s: %s",
            batch_idx,
            connection_id,
            exc,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "generate_semantic_batch: batch %d failed: connection=%s", batch_idx, connection_id
        )
        detail = str(exc) if str(exc) else f"Batch {batch_idx} generation failed"
        raise HTTPException(status_code=500, detail=detail) from exc

    # Re-read with a row lock so concurrent batch writes merge into the latest
    # DB state, not the stale snapshot loaded at request start 40 s ago.
    # populate_existing=True forces SQLAlchemy to bypass the identity-map cache.
    locked = await db.execute(
        select(Connection)
        .where(Connection.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    conn_locked = locked.scalar_one()
    current_model = SemanticModel.model_validate(conn_locked.semantic_model)

    if current_model.generation_status != GenerationStatus.TABLES_PARTIAL:
        # Another request (e.g. a fresh /generate/init, or /generate/globals
        # finalizing the model) changed status while this batch call was
        # awaiting the LLM. Discard this batch's results rather than corrupt
        # a model that has moved on.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Semantic generation state changed during batch processing "
                f"(status is now '{current_model.generation_status}')"
            ),
        )

    merged_tables = {**current_model.tables, **batch_tables}
    new_progress = _advance_progress(current_model.generation_progress, len(merged_tables))
    new_partial = current_model.model_copy(
        update={"tables": merged_tables, "generation_progress": new_progress}
    )

    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(
            semantic_model=new_partial.model_dump(mode="json"),
            semantic_model_updated_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return new_partial


@router.post("/{connection_id}/semantic/generate/globals", response_model=SemanticModel)
@limiter.limit("10/minute")
async def generate_semantic_globals(
    request: Request,
    connection_id: str,
    provider: str = "claude",
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> SemanticModel:
    """Phase 3 of phased generation: generate cross-table metrics, joins, and derived columns.

    Call after all batch calls have completed.  Finalises the model by setting
    ``generation_status = "complete"`` and persisting the full semantic model.
    """
    schema, conn = await _load_schema_for_connection(connection_id, current_user, db)

    if not conn.semantic_model:
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init and /generate/batch before /generate/globals",
        )
    partial_model = SemanticModel.model_validate(conn.semantic_model)
    if partial_model.generation_status not in (
        GenerationStatus.TABLES_PARTIAL,
        GenerationStatus.COMPLETE,
    ):
        raise HTTPException(
            status_code=400,
            detail="Call /generate/init and /generate/batch before /generate/globals",
        )

    llm_provider, configured_model, provider_name = await _resolve_provider(provider, db)

    gen = SemanticModelGenerator()
    relationship_edges = gen._build_relationship_graph(schema)
    all_tables = partial_model.tables

    try:
        metrics, joins, derived_columns, gen_warnings, segments = await asyncio.wait_for(
            gen.generate_globals(
                schema=schema,
                provider=llm_provider,
                model=configured_model or None,
                all_tables=all_tables,
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
        logger.error(
            "generate_semantic_globals failed: connection=%s provider=%s: %s",
            connection_id,
            provider_name,
            exc,
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

    # Re-read under a row lock so this doesn't clobber a concurrent batch
    # write that landed while the (slow) globals LLM call was in flight.
    conn_locked = await lock_and_reread_connection(connection_id, db)
    current_model = SemanticModel.model_validate(conn_locked.semantic_model)
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

    # Prune relationship edges to generated tables only
    generated_tables = set(all_tables.keys())
    pruned_relationships = [
        e
        for e in relationship_edges
        if e.from_table in generated_tables and e.to_table in generated_tables
    ]

    dialect = conn.source_type
    final = current_model.model_copy(
        update={
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

    await db.execute(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(
            semantic_model=final.model_dump(mode="json"),
            semantic_model_updated_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return final
