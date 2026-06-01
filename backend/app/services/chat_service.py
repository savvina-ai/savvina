# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Core orchestration service — routes user messages through the full NL-to-SQL pipeline.

Implementation is split across focused sub-modules:
  schema_utils.py   — schema serialization, SQL table extraction, query validation helpers
  schema_pruning.py — schema pruning, relevance filtering, schema resolution
  correction.py     — self-correction loops (schema, complexity, execution, zero-result)
  validation.py     — query validation pipeline
  pipeline.py       — LLM provider setup, prompt compression, query generation
  execution.py      — query execution, result masking, SQL injection helpers
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import and_, select, update

from ..cache.example_library import ExampleLibrary
from ..cache.query_cache import QueryCache
from ..config import get_settings
from ..database import async_session_maker
from ..datasources.models import DataSourceSchema, PrivacySettings, QueryResult
from ..datasources.registry import create_datasource
from ..models.chat import ChatMessage, ChatSession
from ..models.connection import Connection
from ..schemas.chat import ChatResponse, QueryResultsResponse
from ..schemas.sse import (
    DoneEvent,
    ErrorEvent,
    ExplanationEvent,
    SqlEvent,
    SseEvent,
    StatusEvent,
)
from ..semantic.models import SemanticModel
from ..utils.encryption import decrypt_value
from .execution import (
    _execute_auto_query,
    _inject_order_by,
    _inject_row_filter,
    _mask_sensitive_result_columns,
    _results_to_response,
    _stream_execute_with_correction,
    _StreamResult,
)
from .intent_classifier import IntentClassifier
from .pipeline import (
    _generate_query,
)
from .schema_pruning import (
    _filter_semantic_by_relevance,
    _filter_semantic_to_schema,
    _resolve_schema,
    _select_relevant_tables,
)
from .schema_utils import (
    _check_query_complexity,
)
from .validation import _validate_and_correct_query

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Module-level alias for the session factory — assign to this name so that unit
# tests can patch it without touching the shared database module.
_create_session = async_session_maker

logger = logging.getLogger(__name__)

_intent_classifier = IntentClassifier()


class ResourceNotFoundError(ValueError):
    """Raised when a requested resource (message, session, connection) does not exist."""


class _PipelineError(Exception):
    """Raised by pipeline stage functions; callers convert to SSE error or status field."""


@dataclass
class _PipelineContext:
    """Collected state after running all shared pipeline stages (1-8)."""

    conn: Connection
    config_dict: dict
    privacy: PrivacySettings | None
    adapter: Any  # BaseDataSource
    schema: DataSourceSchema
    gen: Any  # _GenerationResult
    generated_query: str | None  # post-validation (may differ from gen.generated_query)
    explanation: str  # post-validation
    error: str | None  # validation error if any
    status: str  # current pipeline status after validation
    session: ChatSession
    connected: bool
    semantic_model: Any  # SemanticModel | None


# ── Module-level helpers ───────────────────────────────────────────────────────


async def _get_or_create_session(
    session_id: str | None,
    connection_id: str,
    provider_name: str,
    title: str,
    db: AsyncSession,
    user_id: str | None = None,
) -> ChatSession:
    """Load an existing session by ID (user-scoped), or create a new one."""
    if session_id:
        stmt = select(ChatSession).where(ChatSession.id == session_id)
        if user_id is not None:
            stmt = stmt.where(ChatSession.user_id == user_id)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return existing

    session = ChatSession(
        id=str(uuid.uuid4()),
        connection_id=connection_id,
        title=title,
        provider=provider_name,
        user_id=user_id,
    )
    db.add(session)
    await db.flush()
    return session


async def _save_user_message(session_id: str, content: str, db: AsyncSession) -> ChatMessage:
    msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=content,
        status="executed",
    )
    db.add(msg)
    await db.flush()
    return msg


async def _save_assistant_message(
    *,
    session_id: str,
    content: str,
    query: str | None,
    query_dialect: str | None,
    status: str,
    results_json: dict | None,
    execution_time_ms: float | None,
    bytes_scanned: int | None,
    cache_hit: bool,
    error: str | None,
    token_count: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    db: AsyncSession,
) -> ChatMessage:
    msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=content,
        query_generated=query,
        query_dialect=query_dialect,
        status=status,
        results_json=results_json,
        execution_time_ms=execution_time_ms,
        bytes_scanned=bytes_scanned,
        cache_hit=cache_hit,
        error=error,
        token_count=token_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    db.add(msg)
    await db.flush()
    return msg


# ── Pipeline stage functions ───────────────────────────────────────────────────


async def _stage_load_connection(
    connection_id: str,
    db: AsyncSession,
    settings: Any,
) -> tuple[Connection, dict, PrivacySettings | None, Any]:
    """Stage 1-2: load active connection, decrypt config, create datasource adapter.

    Raises _PipelineError if the connection is not found or inactive.
    """
    conn_result = await db.execute(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.is_active == True,  # noqa: E712
        )
    )
    conn = conn_result.scalar_one_or_none()
    if conn is None:
        raise _PipelineError(f"Connection '{connection_id}' not found")
    config_dict: dict = json.loads(decrypt_value(conn.config_encrypted, settings.encryption_key))
    privacy = (
        PrivacySettings.from_dict(conn.privacy_settings)
        if conn.privacy_settings
        else PrivacySettings()
    )
    adapter = create_datasource(conn.source_type)
    return conn, config_dict, privacy, adapter


async def _stage_resolve_schema(
    conn: Connection,
    adapter: Any,
    config_dict: dict,
    privacy: PrivacySettings | None,
    connected: bool,
    db: AsyncSession,
    user_id: str | None,
    cache: QueryCache,
) -> Any:  # _SchemaResult
    """Stage 3: resolve per-user schema (cache or introspect).

    Raises _PipelineError wrapping any underlying exception.
    """
    try:
        return await _resolve_schema(
            conn,
            adapter,
            config_dict,
            privacy,
            connected,
            db,
            user_id=user_id or "",
            query_cache=cache,
        )
    except Exception as exc:
        raise _PipelineError(f"Schema error: {exc}") from exc


async def _stage_prune_and_filter(
    sr: Any,  # _SchemaResult
    conn: Connection,
    message: str,
    settings: Any,
    cache: QueryCache,
    db: AsyncSession,
    connection_id: str,
    user_id: str | None,
    privacy: PrivacySettings | None,
) -> tuple[DataSourceSchema, Any]:  # (DataSourceSchema, SemanticModel | None)
    """Stage 3b-4: apply schema pruning and semantic model filtering."""
    schema = sr.schema
    _raw_semantic: SemanticModel | None = (
        SemanticModel.model_validate(conn.semantic_model) if conn.semantic_model else None
    )
    if sr.embeddings_available and getattr(settings, "schema_pruning_enabled", True):
        schema = await _select_relevant_tables(
            schema=schema,
            question=message,
            privacy=privacy,
            settings=settings,
            cache=cache,
            db=db,
            connection_id=connection_id,
            user_id=user_id or "",
            semantic_model=_raw_semantic,
        )
    semantic_model: SemanticModel | None = _raw_semantic
    if semantic_model:
        semantic_model = _filter_semantic_to_schema(semantic_model, schema)
        semantic_model = _filter_semantic_by_relevance(semantic_model, message)
    return schema, semantic_model


async def _stage_generate(
    *,
    cache: QueryCache,
    examples: ExampleLibrary,
    connection_id: str,
    message: str,
    session_id: str | None,
    adapter: Any,
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
    semantic_model: Any,
    provider_name: str,
    options: dict,
    settings: Any,
) -> Any:  # _GenerationResult
    """Stage 5: run LLM query generation (cache lookup + LLM call).

    Raises _PipelineError on unexpected exceptions from _generate_query.
    The DB session is managed internally by _generate_query and is released before
    the LLM call, so no pool slot is held during inference.
    """
    try:
        return await _generate_query(
            cache=cache,
            examples=examples,
            connection_id=connection_id,
            message=message,
            session_id=session_id,
            adapter=adapter,
            schema=schema,
            privacy=privacy,
            semantic_model=semantic_model,
            provider_name=provider_name,
            options=options,
            settings=settings,
            bypass_cache=bool(options.get("bypass_cache", False)),
            force_refresh=bool(options.get("force_refresh", False)),
        )
    except Exception as exc:
        raise _PipelineError(f"Generation error: {exc}") from exc


async def _stage_validate(
    gen: Any,  # _GenerationResult
    adapter: Any,
    schema: DataSourceSchema,
    message: str,
) -> Any:  # _ValidationResult
    """Stage 7: validate and optionally correct the generated query.

    Raises _PipelineError on unexpected exceptions; soft validation errors are
    returned in the result object (val.error) and do not raise.
    """
    try:
        return await _validate_and_correct_query(
            generated_query=gen.generated_query,
            explanation=gen.explanation,
            error=gen.error,
            adapter=adapter,
            schema=schema,
            cache_hit=gen.cache_hit,
            provider=gen.provider,
            configured_model=gen.configured_model,
            configured_max_tokens=gen.configured_max_tokens,
            system_prompt=gen.system_prompt,
            history=gen.history,
            message=message,
        )
    except Exception as exc:
        raise _PipelineError(f"Validation error: {exc}") from exc


async def _run_shared_pipeline(
    *,
    connection_id: str,
    session_id: str | None,
    message: str,
    provider_name: str,
    options: dict,
    db: AsyncSession,
    user_id: str | None,
    cache: QueryCache,
    examples: ExampleLibrary,
    settings: Any,
) -> _PipelineContext:
    """Run pipeline stages 1-8 and return a fully populated _PipelineContext.

    Raises _PipelineError on any stage failure. If the adapter was connected
    during schema resolution and a later stage fails, it is disconnected before
    re-raising so the caller does not inherit a dangling connection.
    """
    conn, config_dict, privacy, adapter = await _stage_load_connection(connection_id, db, settings)
    connected = False
    try:
        sr = await _stage_resolve_schema(
            conn, adapter, config_dict, privacy, connected, db, user_id, cache
        )
        connected = sr.connected
        schema, semantic_model = await _stage_prune_and_filter(
            sr, conn, message, settings, cache, db, connection_id, user_id, privacy
        )
        gen = await _stage_generate(
            cache=cache,
            examples=examples,
            connection_id=connection_id,
            message=message,
            session_id=session_id,
            adapter=adapter,
            schema=schema,
            privacy=privacy,
            semantic_model=semantic_model,
            provider_name=provider_name,
            options=options,
            settings=settings,
        )
        val = await _stage_validate(gen, adapter, schema, message)
        title = message[:50] + ("..." if len(message) > 50 else "")
        session = await _get_or_create_session(
            session_id, connection_id, provider_name, title, db, user_id=user_id
        )
    except _PipelineError:
        if connected:
            with contextlib.suppress(Exception):
                await adapter.disconnect()
        raise

    generated_query = val.generated_query
    explanation = val.explanation
    error = val.error
    status = val.status if val.error else gen.status

    return _PipelineContext(
        conn=conn,
        config_dict=config_dict,
        privacy=privacy,
        adapter=adapter,
        schema=schema,
        gen=gen,
        generated_query=generated_query,
        explanation=explanation,
        error=error,
        status=status,
        session=session,
        connected=connected,
        semantic_model=semantic_model,
    )


# ── ChatService ────────────────────────────────────────────────────────────────


class ChatService:
    """Orchestrates the full NL-to-SQL pipeline with caching and execution modes."""

    def __init__(self, query_cache: QueryCache, example_library: ExampleLibrary) -> None:
        self.cache = query_cache
        self.examples = example_library

    async def stream_message(
        self,
        connection_id: str,
        session_id: str | None,
        message: str,
        provider_name: str,
        options: dict,
        user_id: str | None = None,
    ) -> AsyncGenerator[SseEvent, None]:
        """Full pipeline with SSE streaming: emits typed events as each stage completes.

        Never raises — all exceptions are caught and emitted as error + done events.

        DB sessions are managed internally:
        - Session A (pre-LLM): stages 1-4; released before _stage_generate is called.
        - _stage_generate opens its own short session for cache/examples/history/provider
          reads and closes it before the LLM call, so no pool slot is held during inference.
        - Session B1 (post-LLM): get_or_create_session only; committed and closed before
          query execution begins, so no pool slot is held while the user's DB runs the query.
        - Session B2 (post-execution): cache store + message persistence; closed after commit.

        On client disconnect the caller should call aclose() on this generator, which
        propagates GeneratorExit through any open async-with blocks, releasing sessions
        and returning pool slots immediately.
        """
        settings = get_settings()
        adapter: Any = None
        connected = False
        gen: Any = None

        try:
            # ── Session A: stages 1-4 (pre-LLM reads) ────────────────────────────
            async with _create_session() as db_pre:
                # ── Stage 1-2: Load connection ────────────────────────────────────
                yield StatusEvent(type="status", message="Loading connection…")
                try:
                    conn, config_dict, privacy, adapter = await _stage_load_connection(
                        connection_id, db_pre, settings
                    )
                except _PipelineError as exc:
                    yield ErrorEvent(type="error", message=str(exc))
                    yield DoneEvent(
                        type="done",
                        session_id="",
                        message_id="",
                        execution_time_ms=None,
                        cache_hit=False,
                        status="error",
                        token_count=None,
                        input_tokens=None,
                        output_tokens=None,
                        warning=None,
                    )
                    return

                # ── Stage 3: Resolve schema ───────────────────────────────────────
                yield StatusEvent(type="status", message="Resolving schema…")
                try:
                    sr = await _stage_resolve_schema(
                        conn, adapter, config_dict, privacy, connected, db_pre, user_id, self.cache
                    )
                    connected = sr.connected
                except _PipelineError as exc:
                    yield ErrorEvent(type="error", message=str(exc))
                    yield DoneEvent(
                        type="done",
                        session_id="",
                        message_id="",
                        execution_time_ms=None,
                        cache_hit=False,
                        status="error",
                        token_count=None,
                        input_tokens=None,
                        output_tokens=None,
                        warning=None,
                    )
                    return

                # ── Stages 3b-4: Schema pruning + semantic filtering ──────────────
                try:
                    schema, semantic_model = await _stage_prune_and_filter(
                        sr,
                        conn,
                        message,
                        settings,
                        self.cache,
                        db_pre,
                        connection_id,
                        user_id,
                        privacy,
                    )
                except Exception as exc:
                    yield ErrorEvent(type="error", message=f"Schema pruning error: {exc}")
                    yield DoneEvent(
                        type="done",
                        session_id="",
                        message_id="",
                        execution_time_ms=None,
                        cache_hit=False,
                        status="error",
                        token_count=None,
                        input_tokens=None,
                        output_tokens=None,
                        warning=None,
                    )
                    return
            # ── Session A closed — pool slot returned before LLM call ────────────

            # ── Stage 5: LLM generation ───────────────────────────────────────────
            # _stage_generate manages its own short session for DB reads and closes
            # it before the LLM call.  No pool slot is held during inference.
            yield StatusEvent(type="status", message="Generating query…")
            try:
                gen = await _stage_generate(
                    cache=self.cache,
                    examples=self.examples,
                    connection_id=connection_id,
                    message=message,
                    session_id=session_id,
                    adapter=adapter,
                    schema=schema,
                    privacy=privacy,
                    semantic_model=semantic_model,
                    provider_name=provider_name,
                    options=options,
                    settings=settings,
                )
            except _PipelineError as exc:
                yield ErrorEvent(type="error", message=str(exc))
                yield DoneEvent(
                    type="done",
                    session_id="",
                    message_id="",
                    execution_time_ms=None,
                    cache_hit=False,
                    status="error",
                    token_count=None,
                    input_tokens=None,
                    output_tokens=None,
                    warning=None,
                )
                return

            # ── Stage 6: Emit SQL event immediately ───────────────────────────────
            if gen.generated_query:
                yield SqlEvent(
                    type="sql",
                    query=gen.generated_query,
                    dialect=adapter.query_dialect,
                )

            # ── Stage 7: Validate + correct (no DB session) ───────────────────────
            yield StatusEvent(type="status", message="Validating query…")
            try:
                val = await _stage_validate(gen, adapter, schema, message)
            except _PipelineError as exc:
                yield ErrorEvent(type="error", message=str(exc))
                yield DoneEvent(
                    type="done",
                    session_id="",
                    message_id="",
                    execution_time_ms=None,
                    cache_hit=gen.cache_hit,
                    status="error",
                    token_count=gen.tokens_used,
                    input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens,
                    warning=None,
                )
                return

            generated_query = val.generated_query
            explanation = val.explanation
            error: str | None = val.error
            status: str = val.status if val.error else gen.status

            # Emit updated sql event if validation corrected the query
            if generated_query and generated_query != gen.generated_query:
                yield SqlEvent(type="sql", query=generated_query, dialect=adapter.query_dialect)

            # ── Session B1: create chat session ──────────────────────────────────
            # Committed immediately so the session row is durable before execution
            # starts; the pool slot is released before the (potentially slow) user
            # query runs against the external database.
            title = message[:50] + ("..." if len(message) > 50 else "")
            async with _create_session() as db_sess:
                chat_session = await _get_or_create_session(
                    session_id,
                    connection_id,
                    provider_name,
                    title,
                    db_sess,
                    user_id=user_id,
                )
                await db_sess.commit()
            # ── Session B1 closed — chat session row committed ────────────────────
            # chat_session.id is a plain UUID string set at object creation; it
            # remains accessible after session close because expire_on_commit=False.
            chat_session_id = chat_session.id

            # ── Stages 9-10: Execute (no app DB session held) ─────────────────────
            results_response: QueryResultsResponse | None = None
            execution_time_ms: float | None = None
            bytes_scanned_val: int | None = None

            if not error:
                if conn.execution_mode == "generate_only":
                    status = "query_only"

                elif conn.execution_mode == "review_first":
                    status = "pending_approval"

                elif conn.execution_mode == "auto_execute" and generated_query:
                    yield StatusEvent(type="status", message="Executing query…")
                    result_out = _StreamResult()
                    async for event in _stream_execute_with_correction(
                        generated_query=generated_query,
                        explanation=explanation,
                        adapter=adapter,
                        config_dict=config_dict,
                        connected=connected,
                        cache_hit=gen.cache_hit,
                        provider=gen.provider,
                        configured_model=gen.configured_model,
                        configured_max_tokens=gen.configured_max_tokens,
                        system_prompt=gen.system_prompt,
                        history=gen.history,
                        message=message,
                        schema=schema,
                        options=options,
                        settings=settings,
                        result_out=result_out,
                        privacy=privacy,
                        intent=_intent_classifier.classify(message),
                    ):
                        yield event

                    exe = result_out.result
                    if exe is not None:
                        results_response = exe.results_response
                        execution_time_ms = exe.execution_time_ms
                        bytes_scanned_val = exe.bytes_scanned
                        status = exe.status
                        error = exe.error
                        generated_query = exe.generated_query
                        explanation = exe.explanation
                        connected = exe.connected

            # ── Session B2: cache + persist messages ──────────────────────────────
            async with _create_session() as db_post:
                # ── Stage 11: Cache store ─────────────────────────────────────────
                if (
                    results_response is not None
                    and gen.cache_embedding is not None
                    and generated_query
                ):
                    await self.cache.store(
                        connection_id=connection_id,
                        question=message,
                        generated_query=generated_query,
                        query_dialect=adapter.query_dialect,
                        embedding=gen.cache_embedding,
                        db=db_post,
                        force=bool(options.get("force_refresh", False)),
                    )

                # ── Stage 12: Emit explanation ────────────────────────────────────
                if explanation:
                    yield ExplanationEvent(type="explanation", text=explanation)

                # ── Stage 13: Persist messages ────────────────────────────────────
                try:
                    await _save_user_message(chat_session_id, message, db_post)
                    results_json = results_response.model_dump() if results_response else None
                    assistant_msg = await _save_assistant_message(
                        session_id=chat_session_id,
                        content=explanation,
                        query=generated_query,
                        query_dialect=adapter.query_dialect if generated_query else None,
                        status=status,
                        results_json=results_json,
                        execution_time_ms=execution_time_ms,
                        bytes_scanned=bytes_scanned_val,
                        cache_hit=gen.cache_hit,
                        error=error,
                        token_count=gen.tokens_used,
                        input_tokens=gen.input_tokens,
                        output_tokens=gen.output_tokens,
                        db=db_post,
                    )
                    await db_post.execute(
                        update(ChatSession)
                        .where(ChatSession.id == chat_session_id)
                        .values(updated_at=datetime.now(UTC))
                    )
                    await db_post.commit()
                except Exception as exc:
                    await db_post.rollback()
                    logger.exception("Failed to persist messages in stream: %s", exc)
                    yield ErrorEvent(type="error", message=f"Failed to save: {exc}")
                    yield DoneEvent(
                        type="done",
                        session_id=chat_session_id,
                        message_id="",
                        execution_time_ms=execution_time_ms,
                        cache_hit=gen.cache_hit,
                        status="error",
                        token_count=None,
                        input_tokens=None,
                        output_tokens=None,
                        warning=None,
                    )
                    return
            # ── Session B2 closed ─────────────────────────────────────────────────

            # ── Done ──────────────────────────────────────────────────────────────
            if error and status == "error":
                yield ErrorEvent(type="error", message=error)

            yield DoneEvent(
                type="done",
                session_id=chat_session_id,
                message_id=assistant_msg.id,
                execution_time_ms=execution_time_ms,
                cache_hit=gen.cache_hit,
                status=status,
                token_count=gen.tokens_used,
                input_tokens=gen.input_tokens,
                output_tokens=gen.output_tokens,
                warning=gen.tpm_warning,
            )

        except Exception as exc:
            logger.exception("Unexpected error in stream_message: %s", exc)
            yield ErrorEvent(type="error", message=f"Unexpected error: {exc}")
            yield DoneEvent(
                type="done",
                session_id="",
                message_id="",
                execution_time_ms=None,
                cache_hit=False,
                status="error",
                token_count=None,
                input_tokens=None,
                output_tokens=None,
                warning=None,
            )

        finally:
            if connected and adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.disconnect()

    async def process_message(
        self,
        connection_id: str,
        session_id: str | None,
        message: str,
        provider_name: str,
        options: dict,
        user_id: str | None = None,
    ) -> ChatResponse:
        """Full pipeline without SSE streaming — returns a ChatResponse directly.

        DB sessions are managed internally:
        - Session A: stages 1-8 via _run_shared_pipeline; closed before execution.
          _stage_generate within it manages its own short session for pre-LLM reads,
          so no pool slot is held during LLM inference.
        - Session B: execution (no app DB), then cache store + message persistence.
        """
        settings = get_settings()

        async with _create_session() as db_pipeline:
            try:
                ctx = await _run_shared_pipeline(
                    connection_id=connection_id,
                    session_id=session_id,
                    message=message,
                    provider_name=provider_name,
                    options=options,
                    db=db_pipeline,
                    user_id=user_id,
                    cache=self.cache,
                    examples=self.examples,
                    settings=settings,
                )
            except _PipelineError as exc:
                raise ResourceNotFoundError(str(exc)) from exc
        # ── Session A closed — LLM inference already completed inside _stage_generate

        generated_query = ctx.generated_query
        explanation = ctx.explanation
        error = ctx.error
        status = ctx.status
        gen = ctx.gen
        adapter = ctx.adapter
        connected = ctx.connected

        results_response: QueryResultsResponse | None = None
        execution_time_ms: float | None = None
        bytes_scanned_val: int | None = None

        try:
            # ── Execution (no app DB session held) ───────────────────────────────
            if not error:
                if ctx.conn.execution_mode == "generate_only":
                    status = "query_only"
                elif ctx.conn.execution_mode == "review_first":
                    status = "pending_approval"
                elif ctx.conn.execution_mode == "auto_execute" and generated_query:
                    exe = await _execute_auto_query(
                        generated_query=generated_query,
                        explanation=explanation,
                        adapter=adapter,
                        config_dict=ctx.config_dict,
                        connected=connected,
                        cache_hit=gen.cache_hit,
                        provider=gen.provider,
                        configured_model=gen.configured_model,
                        configured_max_tokens=gen.configured_max_tokens,
                        system_prompt=gen.system_prompt,
                        history=gen.history,
                        message=message,
                        schema=ctx.schema,
                        options=options,
                        settings=settings,
                        privacy=ctx.privacy,
                        intent=_intent_classifier.classify(message),
                    )
                    results_response = exe.results_response
                    execution_time_ms = exe.execution_time_ms
                    bytes_scanned_val = exe.bytes_scanned
                    status = exe.status
                    error = exe.error
                    generated_query = exe.generated_query
                    explanation = exe.explanation
                    connected = exe.connected

            # ── Session B: cache + persist messages ──────────────────────────────
            async with _create_session() as db_post:
                if not error and gen.cache_embedding is not None and generated_query:
                    await self.cache.store(
                        connection_id=connection_id,
                        question=message,
                        generated_query=generated_query,
                        query_dialect=adapter.query_dialect,
                        embedding=gen.cache_embedding,
                        db=db_post,
                        force=bool(options.get("force_refresh", False)),
                    )

                await _save_user_message(ctx.session.id, message, db_post)
                results_json = results_response.model_dump() if results_response else None
                assistant_msg = await _save_assistant_message(
                    session_id=ctx.session.id,
                    content=explanation,
                    query=generated_query,
                    query_dialect=adapter.query_dialect if generated_query else None,
                    status=status,
                    results_json=results_json,
                    execution_time_ms=execution_time_ms,
                    bytes_scanned=bytes_scanned_val,
                    cache_hit=gen.cache_hit,
                    error=error,
                    token_count=gen.tokens_used,
                    input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens,
                    db=db_post,
                )
                await db_post.execute(
                    update(ChatSession)
                    .where(ChatSession.id == ctx.session.id)
                    .values(updated_at=datetime.now(UTC))
                )
                await db_post.commit()

            return ChatResponse(
                session_id=ctx.session.id,
                message_id=assistant_msg.id,
                query=generated_query,
                query_dialect=adapter.query_dialect if generated_query else None,
                explanation=explanation,
                results=results_response,
                execution_time_ms=execution_time_ms,
                status=status,  # type: ignore[arg-type]
                cache_hit=gen.cache_hit,
                error=error,
                token_count=gen.tokens_used,
                input_tokens=gen.input_tokens,
                output_tokens=gen.output_tokens,
            )

        finally:
            if connected:
                with contextlib.suppress(Exception):
                    await adapter.disconnect()

    async def execute_pending(
        self,
        message_id: str,
        db: AsyncSession,
        user_id: str | None = None,
    ) -> ChatResponse:
        """Execute a query that was previously returned as pending_approval.

        Called when the user clicks 'Run Query' in review_first mode.
        """
        settings = get_settings()

        # 1. Load message + session + connection in a single round-trip
        joined_result = await db.execute(
            select(ChatMessage, ChatSession, Connection)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id, isouter=True)
            .join(
                Connection,
                and_(Connection.id == ChatSession.connection_id, Connection.is_active == True),  # noqa: E712
                isouter=True,
            )
            .where(ChatMessage.id == message_id)
        )
        joined = joined_result.one_or_none()
        if joined is None:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        msg, session, conn = joined
        if msg.status != "pending_approval":
            raise ValueError(
                f"Message '{message_id}' is not pending approval (status: {msg.status})"
            )
        if not msg.query_generated:
            raise ValueError(f"Message '{message_id}' has no generated query to execute")
        if session is None:
            raise ResourceNotFoundError("Chat session not found")
        if user_id is not None and session.user_id != user_id:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        if conn is None:
            raise ResourceNotFoundError("Connection not found")

        # 3. Connect adapter and execute
        config_dict: dict = json.loads(
            decrypt_value(conn.config_encrypted, settings.encryption_key)
        )
        privacy = (
            PrivacySettings.from_dict(conn.privacy_settings)
            if conn.privacy_settings
            else PrivacySettings()
        )
        adapter = create_datasource(conn.source_type)

        approved_query = msg.query_generated

        if privacy and privacy.row_filter_sql:
            approved_query = _inject_row_filter(
                approved_query, privacy.row_filter_sql, adapter.query_dialect
            )

        exec_error: str | None = None
        query_result: QueryResult | None = None
        try:
            await adapter.connect(config_dict)
            query_result = await adapter.execute_query(
                approved_query,
                timeout=settings.default_query_timeout,
                max_rows=settings.default_row_limit,
            )
        except Exception as e:
            exec_error = str(e)
        finally:
            await adapter.disconnect()

        if exec_error:
            await db.execute(
                update(ChatMessage).where(ChatMessage.id == message_id).values(status="error")
            )
            session.updated_at = datetime.now(UTC)
            await db.commit()
            return ChatResponse(
                session_id=msg.session_id,
                message_id=msg.id,
                query=msg.query_generated,
                query_dialect=msg.query_dialect,
                explanation=msg.content,
                status="error",
                error=exec_error,
                cache_hit=msg.cache_hit,
            )

        if query_result is None:
            raise ValueError("Query execution produced no result object")
        results_response = _mask_sensitive_result_columns(
            _results_to_response(query_result), privacy
        )

        # 4. Update the message with results
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id == message_id)
            .values(
                status="executed",
                results_json=results_response.model_dump(),
                execution_time_ms=query_result.execution_time_ms,
                bytes_scanned=query_result.bytes_scanned,
            )
        )
        session.updated_at = datetime.now(UTC)
        await db.commit()

        return ChatResponse(
            session_id=msg.session_id,
            message_id=msg.id,
            query=msg.query_generated,
            query_dialect=msg.query_dialect,
            explanation=msg.content,
            results=results_response,
            execution_time_ms=query_result.execution_time_ms,
            status="executed",
            cache_hit=msg.cache_hit,
            token_count=msg.token_count,
            input_tokens=msg.input_tokens,
            output_tokens=msg.output_tokens,
        )

    async def edit_and_execute(
        self,
        message_id: str,
        edited_query: str,
        db: AsyncSession,
        user_id: str | None = None,
    ) -> ChatResponse:
        """Execute a user-edited version of a generated query.

        Called when the user edits the query in review_first mode.
        """
        settings = get_settings()

        # 1. Load message + session + connection in a single round-trip
        joined_result = await db.execute(
            select(ChatMessage, ChatSession, Connection)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id, isouter=True)
            .join(
                Connection,
                and_(Connection.id == ChatSession.connection_id, Connection.is_active == True),  # noqa: E712
                isouter=True,
            )
            .where(ChatMessage.id == message_id)
        )
        joined = joined_result.one_or_none()
        if joined is None:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        msg, session, conn = joined
        if session is None:
            raise ResourceNotFoundError("Chat session not found")
        if user_id is not None and session.user_id != user_id:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        if conn is None:
            raise ResourceNotFoundError("Connection not found")

        config_dict: dict = json.loads(
            decrypt_value(conn.config_encrypted, settings.encryption_key)
        )
        privacy = (
            PrivacySettings.from_dict(conn.privacy_settings)
            if conn.privacy_settings
            else PrivacySettings()
        )
        adapter = create_datasource(conn.source_type)

        # 2. Validate the edited query before touching the DB
        validation = adapter.validate_query(edited_query)
        if not validation.is_valid:
            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.id == message_id)
                .values(status="error", error=validation.error_message)
            )
            session.updated_at = datetime.now(UTC)
            await db.commit()
            return ChatResponse(
                session_id=msg.session_id,
                message_id=msg.id,
                query=edited_query,
                query_dialect=msg.query_dialect,
                explanation=msg.content,
                status="error",
                error=validation.error_message,
            )

        complexity_error = _check_query_complexity(edited_query)
        if complexity_error:
            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.id == message_id)
                .values(status="error", error=complexity_error)
            )
            session.updated_at = datetime.now(UTC)
            await db.commit()
            return ChatResponse(
                session_id=msg.session_id,
                message_id=msg.id,
                query=edited_query,
                query_dialect=msg.query_dialect,
                explanation=msg.content,
                status="error",
                error=complexity_error,
            )

        # 3. Execute the edited query (with row filter enforced)
        exec_error: str | None = None
        run_query = edited_query
        if privacy and privacy.row_filter_sql:
            run_query = _inject_row_filter(run_query, privacy.row_filter_sql, adapter.query_dialect)
        try:
            await adapter.connect(config_dict)
            query_result = await adapter.execute_query(
                run_query,
                timeout=settings.default_query_timeout,
                max_rows=settings.default_row_limit,
            )
        except Exception as e:
            exec_error = str(e)
        finally:
            await adapter.disconnect()

        if exec_error:
            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.id == message_id)
                .values(status="error", error=exec_error)
            )
            session.updated_at = datetime.now(UTC)
            await db.commit()
            return ChatResponse(
                session_id=msg.session_id,
                message_id=msg.id,
                query=edited_query,
                query_dialect=msg.query_dialect,
                explanation=msg.content,
                status="error",
                error=exec_error,
            )

        results_response = _mask_sensitive_result_columns(
            _results_to_response(query_result), privacy
        )

        # 4. Update the message with the edited query + results
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.id == message_id)
            .values(
                query_generated=edited_query,
                status="executed",
                results_json=results_response.model_dump(),
                execution_time_ms=query_result.execution_time_ms,
                bytes_scanned=query_result.bytes_scanned,
            )
        )
        session.updated_at = datetime.now(UTC)
        await db.commit()

        return ChatResponse(
            session_id=msg.session_id,
            message_id=msg.id,
            query=edited_query,
            query_dialect=adapter.query_dialect,
            explanation=msg.content,
            results=results_response,
            execution_time_ms=query_result.execution_time_ms,
            status="executed",
            cache_hit=False,
            token_count=msg.token_count,
            input_tokens=msg.input_tokens,
            output_tokens=msg.output_tokens,
        )

    async def sort_and_execute(
        self,
        message_id: str,
        sort_column: str,
        sort_order: str,
        db: AsyncSession,
        user_id: str | None = None,
    ) -> QueryResultsResponse:
        """Re-execute the original query with ORDER BY injected for server-side sort.

        Does not mutate the stored message — sort is a read-only view operation.
        """
        settings = get_settings()

        # 1. Load message + session + connection in a single round-trip
        joined_result = await db.execute(
            select(ChatMessage, ChatSession, Connection)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id, isouter=True)
            .join(
                Connection,
                and_(Connection.id == ChatSession.connection_id, Connection.is_active == True),  # noqa: E712
                isouter=True,
            )
            .where(ChatMessage.id == message_id)
        )
        joined = joined_result.one_or_none()
        if joined is None:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        msg, session, conn = joined

        # 2. Ownership check via session
        if session is None or (user_id is not None and session.user_id != user_id):
            raise ResourceNotFoundError(f"Message '{message_id}' not found")

        # 3. Validate sort_column against stored columns (SQL injection guard)
        if not msg.results_json:
            raise ValueError("Message has no stored results")
        stored_columns: list[str] = msg.results_json.get("columns", [])
        if sort_column not in stored_columns:
            raise ValueError(f"Column '{sort_column}' is not in the result set")

        original_query = msg.query_generated
        if not original_query:
            raise ValueError("Message has no generated query")

        # 4. Inject ORDER BY
        dialect = msg.query_dialect or "sql"
        sorted_query = _inject_order_by(original_query, sort_column, sort_order, dialect)

        # 5. Use the already-loaded connection
        if conn is None:
            raise ResourceNotFoundError("Connection not found")

        config_dict: dict = json.loads(
            decrypt_value(conn.config_encrypted, settings.encryption_key)
        )
        privacy = (
            PrivacySettings.from_dict(conn.privacy_settings)
            if conn.privacy_settings
            else PrivacySettings()
        )
        adapter = create_datasource(conn.source_type)

        # 6. Safety-validate the modified query (read-only check)
        validation = adapter.validate_query(sorted_query)
        if not validation.is_valid:
            raise ValueError(f"Sort query failed validation: {validation.error_message}")

        complexity_error = _check_query_complexity(sorted_query)
        if complexity_error:
            raise ValueError(f"Sort query failed complexity check: {complexity_error}")

        # Apply row filter after ORDER BY injection (wraps the sorted query)
        run_query = sorted_query
        if privacy and privacy.row_filter_sql:
            run_query = _inject_row_filter(run_query, privacy.row_filter_sql, adapter.query_dialect)

        # 7. Execute
        exec_error: str | None = None
        query_result: QueryResult | None = None
        try:
            await adapter.connect(config_dict)
            query_result = await adapter.execute_query(
                run_query,
                timeout=settings.default_query_timeout,
                max_rows=settings.default_row_limit,
            )
        except Exception as e:
            exec_error = str(e)
        finally:
            await adapter.disconnect()

        if exec_error or query_result is None:
            raise ValueError(f"Sort execution failed: {exec_error}")

        return _mask_sensitive_result_columns(_results_to_response(query_result), privacy)

    async def submit_feedback(
        self,
        message_id: str,
        feedback: str,  # 'thumbs_up' or 'thumbs_down'
        db: AsyncSession,
        user_id: str | None = None,
    ) -> str:
        """Process user feedback on a generated query.

        thumbs_up  → add to example library for future few-shot prompting.
        thumbs_down → remove from query cache; do NOT add to examples.

        Returns the connection_id of the message's session so callers can use
        it (e.g. record a semantic correction) without an extra DB round-trip.
        """
        # 1. Load the assistant message joined through its session to verify ownership
        joined_result = await db.execute(
            select(ChatMessage, ChatSession)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id, isouter=True)
            .where(ChatMessage.id == message_id)
        )
        joined = joined_result.one_or_none()
        if joined is None:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        msg, session = joined
        if session is None:
            raise ResourceNotFoundError("Chat session not found")
        if user_id is not None and session.user_id != user_id:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")

        connection_id = session.connection_id

        # 2. Retrieve the user question that preceded this assistant message
        user_msg_result = await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == msg.session_id,
                ChatMessage.role == "user",
                ChatMessage.created_at <= msg.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
        user_msg = user_msg_result.scalar_one_or_none()
        question = user_msg.content if user_msg else ""

        if feedback == "thumbs_up" and msg.query_generated and question:
            # Compute embedding and add to example library for future few-shot use
            embedding = await self.cache.compute_embedding_async(question)
            await self.examples.add_example(
                connection_id=connection_id,
                question=question,
                query=msg.query_generated,
                query_dialect=msg.query_dialect or "",
                embedding=embedding,
                db=db,
            )

        elif feedback == "thumbs_down" and question:
            # Evict all cache entries semantically similar to the user's question —
            # not just exact-match — so entries served via cosine lookup are also removed.
            evicted = await self.cache.evict_similar(connection_id, question, db)
            logger.debug(
                "thumbs_down: evicted %d cache entries for connection %s", evicted, connection_id
            )

        # Persist the rating so feedback history survives restarts
        msg.feedback = feedback
        await db.commit()
        return connection_id

    async def retract_feedback(
        self,
        message_id: str,
        db: AsyncSession,
        user_id: str | None = None,
    ) -> None:
        """Remove previously submitted feedback from a message.

        thumbs_up retraction: deletes the verified example that was added so it
        no longer influences future few-shot prompting.
        thumbs_down retraction: clears the feedback field only — evicted cache
        entries cannot be restored, but the field is cleared so re-submission works.
        """
        joined_result = await db.execute(
            select(ChatMessage, ChatSession)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id, isouter=True)
            .where(ChatMessage.id == message_id)
        )
        joined = joined_result.one_or_none()
        if joined is None:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")
        msg, session = joined
        if session is None:
            raise ResourceNotFoundError("Chat session not found")
        if user_id is not None and session.user_id != user_id:
            raise ResourceNotFoundError(f"Message '{message_id}' not found")

        if msg.feedback == "thumbs_up" and msg.query_generated:
            # Find the user question that preceded this message
            user_msg_result = await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == msg.session_id,
                    ChatMessage.role == "user",
                    ChatMessage.created_at <= msg.created_at,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
            user_msg = user_msg_result.scalar_one_or_none()
            if user_msg:
                # Remove the verified example that was added when thumbs-up was submitted
                from sqlalchemy import delete as sa_delete

                from ..models.example import VerifiedExample

                await db.execute(
                    sa_delete(VerifiedExample).where(
                        VerifiedExample.connection_id == session.connection_id,
                        VerifiedExample.question == user_msg.content,
                        VerifiedExample.query == msg.query_generated,
                    )
                )

        msg.feedback = None
        await db.commit()
