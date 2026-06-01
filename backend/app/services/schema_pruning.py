# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Schema pruning, relevance filtering, semantic model scoping, and schema resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import re
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..cache.query_cache import QueryCache
from ..datasources.models import DataSourceSchema, PrivacySettings, TableInfo
from ..models.connection import Connection
from ..models.table_embedding_cache import TableEmbeddingCache
from ..models.user_schema_cache import UserSchemaCache
from ..semantic.models import SemanticModel
from .schema_utils import (
    _apply_privacy_to_schema,
    _schema_from_dict,
    _schema_to_dict,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Schema pruning tuning constants
_DOMAIN_BOOST = 0.15  # Cosine score added to tables sharing the dominant domain tag
_PRUNING_FALLBACK_MIN = 3  # Minimum tables required before falling back to the full schema
_FK_EXPANSION_CAP = 10  # Maximum tables added by FK graph expansion


@dataclass
class _SchemaResult:
    schema: DataSourceSchema
    connected: bool
    # True when semantic search is available (table embeddings exist for this conn/user)
    embeddings_available: bool = False


def _filter_semantic_to_schema(semantic: SemanticModel, schema: DataSourceSchema) -> SemanticModel:
    """Return a copy of semantic model narrowed to tables visible in schema.

    Also drops relationships and common_joins that reference pruned tables.
    Business metrics are kept as-is — they remain useful signal regardless of table scope.
    """
    visible = {f"{t.schema_name}.{t.name}" for t in schema.tables}
    filtered_tables = {k: v for k, v in semantic.tables.items() if k in visible}
    filtered_rels = [
        r for r in semantic.relationships if r.from_table in visible and r.to_table in visible
    ]
    filtered_joins = [
        j for j in semantic.common_joins if all(t.strip() in visible for t in j.tables)
    ]
    return semantic.model_copy(
        update={
            "tables": filtered_tables,
            "relationships": filtered_rels,
            "common_joins": filtered_joins,
        }
    )


def _filter_semantic_by_relevance(
    model: SemanticModel,
    question: str,
    max_tables: int = 10,
) -> SemanticModel:
    """Return a copy of model narrowed to tables most relevant to question.

    Algorithm:
    1. Tokenise question into lowercase words (strip punctuation).
    2. Score each table: +2 for table bare name match, +1 for display_name word match,
       +1 per column whose name or display_name shares a token with the question.
    3. Include the top-scoring tables up to max_tables.
    4. Follow one hop of relationship edges so JOINs still work.
    5. Filter relationships and common_joins to the included table set.
    6. Keep business_metrics, derived_columns, and time_expressions as-is.

    Returns the model unchanged when it has ≤ max_tables tables or question is empty.
    """
    if not question or len(model.tables) <= max_tables:
        return model

    tokens = set(re.sub(r"[^a-z0-9_]", " ", question.lower()).split())
    tokens.discard("")

    scores: dict[str, int] = {}
    for table_key, tbl in model.tables.items():
        score = 0
        bare = table_key.rpartition(".")[2].lower()
        # +2 for table name match
        if bare in tokens or any(part in tokens for part in bare.split("_")):
            score += 2
        # +1 for display_name word match
        for word in tbl.display_name.lower().split():
            if word in tokens:
                score += 1
                break
        # +1 per column with a name or display_name token hit
        for col_name, col in tbl.columns.items():
            col_bare = col_name.lower()
            if col_bare in tokens or any(part in tokens for part in col_bare.split("_")):
                score += 1
                continue
            for word in col.display_name.lower().split():
                if word in tokens:
                    score += 1
                    break
        # +2 when the table's domain tag appears as a token in the question
        if tbl.domain:
            domain_tokens = set(tbl.domain.lower().replace("_", " ").replace("-", " ").split())
            if domain_tokens & tokens:
                score += 2
        scores[table_key] = score

    # Select top max_tables by score (ties broken by stable dict order)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    included: set[str] = {key for key, _ in ranked[:max_tables]}

    # One-hop expansion: add tables that are direct FK neighbours of included tables
    for rel in model.relationships:
        if rel.from_table in included:
            included.add(rel.to_table)
        elif rel.to_table in included:
            included.add(rel.from_table)

    filtered_tables = {k: v for k, v in model.tables.items() if k in included}
    filtered_rels = [
        r for r in model.relationships if r.from_table in included and r.to_table in included
    ]
    filtered_joins = [j for j in model.common_joins if all(t.strip() in included for t in j.tables)]
    return model.model_copy(
        update={
            "tables": filtered_tables,
            "relationships": filtered_rels,
            "common_joins": filtered_joins,
        }
    )


def _build_table_text(table: TableInfo) -> str:
    """Build embeddable text representing a table for semantic retrieval."""
    col_names = ", ".join(c.name for c in table.columns[:40])  # cap to avoid huge vectors
    parts = [f"{table.schema_name}.{table.name}"]
    if table.description:
        parts.append(table.description)
    if col_names:
        parts.append(f"Columns: {col_names}")
    return ". ".join(parts)


def _pin_mentioned_tables(
    schema: DataSourceSchema,
    question: str,
    privacy: PrivacySettings | None,
) -> tuple[set[str], list[TableInfo], dict[str, TableInfo]]:
    """Return tables that must be included regardless of ANN score.

    Pins any table whose name appears verbatim in the question, plus any table
    listed in privacy.always_include_tables.  Returns (always_keys, always_tables,
    table_map) so the caller can check ANN results against the same structures.
    """
    always_include: set[str] = set(privacy.always_include_tables if privacy else [])
    question_lower = question.lower()
    always_keys: set[str] = set()
    table_map: dict[str, TableInfo] = {f"{t.schema_name}.{t.name}": t for t in schema.tables}

    for table in schema.tables:
        key = f"{table.schema_name}.{table.name}"
        if (
            key in always_include
            or table.name in always_include
            or key.lower() in question_lower
            or table.name.lower() in question_lower
        ):
            always_keys.add(key)

    always_tables: list[TableInfo] = [table_map[k] for k in always_keys if k in table_map]
    return always_keys, always_tables, table_map


def _apply_domain_boost(
    candidates: list[tuple[float, TableInfo]],
    semantic_model: SemanticModel,
) -> list[tuple[float, TableInfo]]:
    """Boost candidates that share the dominant domain tag among the top-5.

    Finds the most common domain tag in the top-5 scored candidates, then adds
    +_DOMAIN_BOOST (clamped to 1.0) to every candidate from that domain and re-sorts so
    domain-coherent tables are more likely to survive the threshold cut.
    Returns the re-sorted candidate list (unchanged if no domain tags found).
    """
    from collections import Counter

    domain_boost: float = _DOMAIN_BOOST
    domain_counts: Counter[str] = Counter()
    for _s, _tbl in candidates[:5]:
        _key = f"{_tbl.schema_name}.{_tbl.name}"
        _sem_tbl = semantic_model.tables.get(_key)
        if _sem_tbl and _sem_tbl.domain:
            domain_counts[_sem_tbl.domain] += 1

    if not domain_counts:
        return candidates

    dominant_domain = domain_counts.most_common(1)[0][0]
    boosted: list[tuple[float, TableInfo]] = []
    for _s, _tbl in candidates:
        _key = f"{_tbl.schema_name}.{_tbl.name}"
        _sem_tbl = semantic_model.tables.get(_key)
        if _sem_tbl and _sem_tbl.domain == dominant_domain:
            _s = min(1.0, _s + domain_boost)
        boosted.append((_s, _tbl))
    return sorted(boosted, key=lambda x: x[0], reverse=True)


def _expand_fk_tables(
    selected: list[TableInfo],
    schema: DataSourceSchema,
    fk_cap: int = _FK_EXPANSION_CAP,
) -> list[TableInfo]:
    """Walk FK relationships up to 2 hops from selected tables.

    Tables needed for JOIN paths often score low on embedding similarity because
    their column names don't contain the query's domain vocabulary.  Walking the
    FK graph closes that gap without inflating the ANN top_k.
    Returns a list of additional TableInfo objects (not already in selected).
    """
    if not schema.relationships:
        return []

    table_map: dict[str, TableInfo] = {f"{t.schema_name}.{t.name}": t for t in schema.tables}
    sel_keys: set[str] = {f"{t.schema_name}.{t.name}" for t in selected}
    fk_extra: list[TableInfo] = []

    for _hop in range(2):
        added_this_hop = 0
        for rel in schema.relationships:
            fk = f"{rel.from_schema}.{rel.from_table}"
            pk = f"{rel.to_schema}.{rel.to_table}"
            for candidate in (
                (pk if fk in sel_keys and pk not in sel_keys else None),
                (fk if pk in sel_keys and fk not in sel_keys else None),
            ):
                if candidate and len(fk_extra) < fk_cap:
                    tbl = table_map.get(candidate)
                    if tbl:
                        fk_extra.append(tbl)
                        sel_keys.add(candidate)
                        added_this_hop += 1
        if added_this_hop == 0:
            break

    return fk_extra


async def _select_relevant_tables(
    schema: DataSourceSchema,
    question: str,
    privacy: PrivacySettings | None,
    settings: Any,
    cache: QueryCache,
    db: AsyncSession,
    connection_id: str,
    user_id: str,
    semantic_model: SemanticModel | None = None,
) -> DataSourceSchema:
    """Return a copy of schema filtered to tables most relevant to the question.

    Uses a pgvector HNSW ANN query against table_embedding_cache to rank tables by
    cosine similarity to the question embedding. Falls back to the full schema when
    pruning cannot produce at least 3 tables.

    When a semantic_model is supplied and tables carry domain tags, tables sharing
    the dominant domain of the top-5 candidates receive a +_DOMAIN_BOOST score boost so
    that domain-coherent sets of tables are more likely to be kept together.
    """
    top_k: int = getattr(settings, "schema_pruning_top_k", 15)
    threshold: float = privacy.schema_pruning_threshold if privacy else 0.30

    q_emb = await cache.compute_embedding_async(question)

    always_keys, always, table_map = _pin_mentioned_tables(schema, question, privacy)

    # ANN query — fetch top_k + buffer to accommodate domain boost re-ranking
    dist_col = TableEmbeddingCache.embedding.cosine_distance(q_emb)
    ann_rows = (
        await db.execute(
            select(TableEmbeddingCache.table_key, dist_col.label("distance"))
            .where(
                TableEmbeddingCache.connection_id == connection_id,
                TableEmbeddingCache.user_id == user_id,
                TableEmbeddingCache.embedding.is_not(None),
            )
            .order_by(dist_col)
            .limit(top_k + 25)
        )
    ).all()

    ann_key_set = {row.table_key for row in ann_rows}
    candidates: list[tuple[float, TableInfo]] = []
    for row in ann_rows:
        if row.table_key in always_keys or row.table_key not in table_map:
            continue
        candidates.append((1.0 - row.distance, table_map[row.table_key]))

    # Tables absent from ANN results (no embedding row) → include for safety
    for table in schema.tables:
        key = f"{table.schema_name}.{table.name}"
        if key not in always_keys and key not in ann_key_set:
            always.append(table)

    candidates.sort(key=lambda x: x[0], reverse=True)

    if semantic_model and candidates:
        candidates = _apply_domain_boost(candidates, semantic_model)

    selected_candidates = [t for s, t in candidates if s >= threshold][:top_k]
    selected = always + selected_candidates

    if len(selected) < _PRUNING_FALLBACK_MIN:
        logger.debug(
            "Schema pruning fallback: %d tables above threshold=%.2f (need %d), using full schema",
            len(selected),
            threshold,
            _PRUNING_FALLBACK_MIN,
        )
        return schema

    fk_extra = _expand_fk_tables(selected, schema)
    if fk_extra:
        selected = selected + fk_extra
        logger.debug(
            "Schema pruning FK expansion: +%d related tables (cap=%d)",
            len(fk_extra),
            _FK_EXPANSION_CAP,
        )

    logger.debug(
        "Schema pruning: %d → %d tables (threshold=%.2f, top_k=%d)",
        len(schema.tables),
        len(selected),
        threshold,
        top_k,
    )
    return DataSourceSchema(
        source_type=schema.source_type,
        schemas=schema.schemas,
        tables=selected,
        relationships=schema.relationships,
        metadata=schema.metadata,
    )


async def _resolve_schema(
    conn: Connection,
    adapter: Any,
    config_dict: dict,
    privacy: PrivacySettings | None,
    connected: bool,
    db: AsyncSession,
    user_id: str,
    query_cache: QueryCache | None = None,
) -> _SchemaResult:
    """Step 4+4b: load per-user schema from cache or introspect, then apply privacy.

    Also computes and persists per-table embeddings (used for schema pruning) if not
    already stored. Embeddings are keyed by "schema.table" and built from table name,
    description, and column names.
    """
    usc_result = await db.execute(
        select(UserSchemaCache).where(
            UserSchemaCache.connection_id == conn.id,
            UserSchemaCache.user_id == user_id,
        )
    )
    usc = usc_result.scalar_one_or_none()

    if usc and usc.schema_cache:
        schema = _schema_from_dict(usc.schema_cache)
    else:
        await adapter.connect(config_dict)
        connected = True
        schema = await adapter.introspect(privacy)
        now = datetime.now(UTC)
        schema_dict = _schema_to_dict(schema)
        from ..semantic.generator import SemanticModelGenerator

        schema_hash = SemanticModelGenerator().compute_schema_hash(schema)
        if usc is None:
            usc = UserSchemaCache(
                connection_id=conn.id,
                user_id=user_id,
                schema_cache=schema_dict,
                schema_cached_at=now,
                schema_hash=schema_hash,
                created_at=now,
                updated_at=now,
            )
            db.add(usc)
        else:
            usc.schema_cache = schema_dict
            usc.schema_cached_at = now
            usc.schema_hash = schema_hash
            usc.updated_at = now
        # Commit schema cache now so it survives even if the LLM call later fails and
        # the outer request transaction is rolled back.
        await db.flush()
        await db.commit()

    # Compute and persist per-table embeddings if not already stored in table_embedding_cache.
    # Done before privacy filtering so all tables get embeddings, even those that will be
    # excluded by privacy — harmless, and avoids recomputing when privacy settings change.
    embeddings_available = False
    if query_cache is not None:
        existing_keys_result = await db.execute(
            select(TableEmbeddingCache.table_key).where(
                TableEmbeddingCache.connection_id == conn.id,
                TableEmbeddingCache.user_id == user_id,
            )
        )
        existing_keys = {row.table_key for row in existing_keys_result}

        missing = [
            table
            for table in schema.tables
            if f"{table.schema_name}.{table.name}" not in existing_keys
        ]
        if missing:
            now = datetime.now(UTC)
            keys = [f"{t.schema_name}.{t.name}" for t in missing]
            texts = [_build_table_text(t) for t in missing]
            embeddings = await asyncio.gather(
                *[query_cache.compute_embedding_async(txt) for txt in texts]
            )
            insert_stmt = pg_insert(TableEmbeddingCache).values(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "connection_id": conn.id,
                        "user_id": user_id,
                        "table_key": key,
                        "embedding": emb,
                        "updated_at": now,
                    }
                    for key, emb in zip(keys, embeddings, strict=False)
                ]
            )
            await db.execute(
                insert_stmt.on_conflict_do_update(
                    index_elements=["connection_id", "user_id", "table_key"],
                    set_={
                        "embedding": insert_stmt.excluded.embedding,
                        "updated_at": insert_stmt.excluded.updated_at,
                    },
                )
            )
            await db.commit()
        embeddings_available = True

    if privacy:
        schema = _apply_privacy_to_schema(schema, privacy)

    return _SchemaResult(
        schema=schema, connected=connected, embeddings_available=embeddings_available
    )
