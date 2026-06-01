# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Two-level query cache: exact match first, semantic similarity fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import re
import threading
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..config import get_settings
from ..models.cache import QueryCacheEntry, QueryCacheStats

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Temporal reference detection ──────────────────────────────────────────────
# Questions that reference relative time periods ("last quarter", "yesterday",
# "past 7 days", etc.) must not be served from the semantic similarity cache:
# two questions differing only in time period ("this month" vs "last month")
# produce different SQL but may have nearly-identical embeddings.
#
# For temporal questions we still allow exact-match cache hits, but restrict
# the freshness window to 1 day so stale time-relative SQL is never reused
# (e.g., "last month revenue" asked today ≠ asked next month).

_TEMPORAL_PATTERN = re.compile(
    r"\b(?:"
    r"today|yesterday|tomorrow|"
    r"now|"
    r"(?:this|last|next|previous|prior|current|past)\s+(?:second|minute|hour|day|week|month|quarter|year)s?|"
    r"(?:last|past)\s+\d+\s+(?:days?|hours?|weeks?|months?|years?)|"
    r"ytd|mtd|qtd|"
    r"year[- ]to[- ]date|month[- ]to[- ]date|quarter[- ]to[- ]date|"
    r"rolling\s+\d+|"
    r"trailing\s+\d+"
    r")\b",
    re.IGNORECASE,
)

_TEMPORAL_EXACT_TTL_DAYS = 1  # Fresh window for exact-match hits on temporal questions

# Maximum number of cache entries per connection; LRU entries are evicted above this cap.
_CACHE_MAX_ENTRIES = 500


@dataclass
class CacheHit:
    """Returned by :meth:`QueryCache.lookup` on a cache hit."""

    cached_question: str
    generated_query: str
    query_dialect: str
    similarity_score: float  # 1.0 for exact match
    cache_type: str  # "exact" or "semantic"


class QueryCache:
    """
    Caches question → generated_query pairs to avoid redundant LLM calls.

    Lookup order:
    1. **Exact match** — normalised lowercase question compared directly.
    2. **Semantic match** — cosine similarity of question embeddings against
       all cached entries for the same connection.  Skipped for questions
       that contain relative time references ("last quarter", "yesterday",
       etc.) to prevent wrong-period cache hits.

    The fastembed ONNX embedding model is lazy-loaded on first use.
    """

    def __init__(
        self,
        embedding_model_name: str,
        similarity_threshold: float,
        max_age_days: int = 30,
    ) -> None:
        self._model_name = embedding_model_name
        self._max_age_days = max_age_days
        self._model = None
        self._model_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    async def lookup(self, connection_id: str, question: str, db: AsyncSession) -> CacheHit | None:
        """
        Look up *question* in the cache for *connection_id*.

        Returns a :class:`CacheHit` on success, ``None`` on cache miss.
        Increments ``hit_count`` and updates ``last_hit_at`` on a hit.

        Temporal questions (containing relative-time keywords such as
        "last quarter" or "yesterday") are matched by exact string only
        and use a 1-day freshness window instead of the configured TTL.
        Semantic similarity matching is skipped for them to prevent
        cross-period false positives.
        """
        normalized = _normalize(question)
        is_temporal = has_temporal_reference(question)

        # Temporal questions get a tight freshness window so stale time-relative
        # SQL is never reused (e.g., "last month revenue" means different SQL
        # on different days).
        effective_max_age = _TEMPORAL_EXACT_TTL_DAYS if is_temporal else self._max_age_days
        fresh = _fresh_condition(effective_max_age)

        # ── 1. Exact match ────────────────────────────────────────────────────
        result = await db.execute(
            select(QueryCacheEntry).where(
                QueryCacheEntry.connection_id == connection_id,
                QueryCacheEntry.question_normalized == normalized,
                fresh,
            )
        )
        entry = result.scalar_one_or_none()
        if entry is not None:
            await _bump_hit(entry.id, db)
            return CacheHit(
                cached_question=entry.question_raw,
                generated_query=entry.generated_query,
                query_dialect=entry.query_dialect,
                similarity_score=1.0,
                cache_type="exact",
            )

        # ── 2. Semantic match — skipped for temporal questions ────────────────
        if is_temporal:
            await _record_miss(connection_id, db)
            return None

        # Read threshold live so UI changes take effect without restart.
        threshold = get_settings().semantic_similarity_threshold
        query_embedding = await self.compute_embedding_async(question)

        dist_col = QueryCacheEntry.question_embedding.cosine_distance(query_embedding)
        result = await db.execute(
            select(QueryCacheEntry, dist_col.label("distance"))
            .where(
                QueryCacheEntry.connection_id == connection_id,
                QueryCacheEntry.question_embedding.is_not(None),
                _fresh_condition(self._max_age_days),
            )
            .order_by(dist_col)
            .limit(1)
        )
        row = result.first()
        if row is None:
            await _record_miss(connection_id, db)
            return None

        best_entry, distance = row
        best_score = 1.0 - distance

        if best_score >= threshold:
            # Reject if the questions contain different numeric literals.
            # "earning > €60k" must not serve a cached result for "earning > €80k"
            # even when the embedding similarity is above the threshold.
            if _extract_numbers(question) != _extract_numbers(best_entry.question_raw):
                await _record_miss(connection_id, db)
                return None
            await _bump_hit(best_entry.id, db)
            return CacheHit(
                cached_question=best_entry.question_raw,
                generated_query=best_entry.generated_query,
                query_dialect=best_entry.query_dialect,
                similarity_score=best_score,
                cache_type="semantic",
            )

        await _record_miss(connection_id, db)
        return None

    async def store(
        self,
        connection_id: str,
        question: str,
        generated_query: str,
        query_dialect: str,
        embedding: list[float] | None,
        db: AsyncSession,
        force: bool = False,
    ) -> None:
        """
        Persist a question → query pair.

        If an entry with the same normalised question already exists for this
        connection it is updated in-place (upsert) to avoid duplicates.
        Computes the embedding if one is not provided.
        When *force* is True the existing entry is overwritten with the new
        query (used by the force-refresh flow where the user explicitly asked
        to regenerate and update the cache).

        Only flushes — does not commit. The caller is responsible for the
        final commit so the cache write and the chat message saves land in
        the same transaction and cannot become orphaned.
        """
        normalized = _normalize(question)
        if embedding is None:
            embedding = await self.compute_embedding_async(question)

        result = await db.execute(
            select(QueryCacheEntry).where(
                QueryCacheEntry.connection_id == connection_id,
                QueryCacheEntry.question_normalized == normalized,
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            if not force:
                # Don't overwrite an existing entry — the cached query was previously
                # validated or came from a verified example. Only explicit invalidation
                # (thumbs-down, schema refresh) should replace it.
                return
            existing.generated_query = generated_query
            existing.question_embedding = embedding
            existing.query_dialect = query_dialect
            await db.flush()
            return

        # New entry: INSERT ... ON CONFLICT DO NOTHING prevents duplicate rows when two
        # concurrent requests for the same question both see no existing entry.
        await db.execute(
            pg_insert(QueryCacheEntry)
            .values(
                id=str(uuid.uuid4()),
                connection_id=connection_id,
                question_normalized=normalized,
                question_raw=question,
                question_embedding=embedding,
                generated_query=generated_query,
                query_dialect=query_dialect,
                hit_count=0,
            )
            .on_conflict_do_nothing(index_elements=["connection_id", "question_normalized"])
        )
        await self._evict_oldest_if_needed(connection_id, db)
        await db.flush()

    async def _evict_oldest_if_needed(self, connection_id: str, db: AsyncSession) -> None:
        """Delete LRU entries when this connection's cache exceeds _CACHE_MAX_ENTRIES.

        Keeps the most-recently-hit entries so the semantic lookup window always
        contains the entries most likely to match future questions.
        """
        count = (
            await db.execute(
                select(func.count())
                .select_from(QueryCacheEntry)
                .where(QueryCacheEntry.connection_id == connection_id)
            )
        ).scalar_one()
        if count <= _CACHE_MAX_ENTRIES:
            return
        subq = (
            select(QueryCacheEntry.id)
            .where(QueryCacheEntry.connection_id == connection_id)
            .order_by(
                QueryCacheEntry.last_hit_at.asc().nulls_first(),
                QueryCacheEntry.created_at.asc(),
            )
            .limit(count - _CACHE_MAX_ENTRIES)
            .scalar_subquery()
        )
        await db.execute(delete(QueryCacheEntry).where(QueryCacheEntry.id.in_(subq)))

    async def invalidate(self, connection_id: str, db: AsyncSession) -> None:
        """
        Delete all cache entries for *connection_id*.

        Called when the user triggers a schema refresh, since cached queries
        may reference tables or columns that no longer exist.
        """
        await db.execute(
            delete(QueryCacheEntry).where(QueryCacheEntry.connection_id == connection_id)
        )
        await db.commit()

    async def evict_similar(
        self,
        connection_id: str,
        question: str,
        db: AsyncSession,
    ) -> int:
        """Delete all cache entries semantically similar to *question*.

        Used by thumbs-down feedback so that a bad cached result is evicted
        even when it was stored under a different but semantically equivalent
        question (i.e. served via cosine-similarity lookup, not exact match).
        Returns the number of rows deleted.
        """
        normalized = _normalize(question)
        threshold = get_settings().semantic_similarity_threshold
        embedding = await self.compute_embedding_async(question)

        dist_col = QueryCacheEntry.question_embedding.cosine_distance(embedding)
        result = await db.execute(
            select(
                QueryCacheEntry.id,
                QueryCacheEntry.question_normalized,
                dist_col.label("distance"),
            )
            .where(
                QueryCacheEntry.connection_id == connection_id,
                QueryCacheEntry.question_embedding.is_not(None),
            )
            .order_by(dist_col)
            .limit(1000)
        )
        rows = result.all()

        ids_to_delete: list[str] = []
        for row in rows:
            if row.question_normalized == normalized or (1.0 - row.distance) >= threshold:
                ids_to_delete.append(row.id)

        if ids_to_delete:
            await db.execute(delete(QueryCacheEntry).where(QueryCacheEntry.id.in_(ids_to_delete)))
            await db.commit()

        return len(ids_to_delete)

    async def prune_expired(self, db: AsyncSession) -> int:
        """
        Delete all entries that have exceeded the configured TTL.

        Returns the number of rows deleted.  Safe to call on a schedule
        (e.g. a background task) — a no-op when ``max_age_days`` is 0.
        """
        if self._max_age_days == 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._max_age_days)
        result = await db.execute(
            delete(QueryCacheEntry).where(
                func.coalesce(
                    QueryCacheEntry.last_hit_at,
                    QueryCacheEntry.created_at,
                )
                < cutoff
            )
        )
        await db.commit()
        return result.rowcount

    # ── embedding helpers ─────────────────────────────────────────────────────

    def compute_embedding(self, text: str) -> list[float]:
        """Compute a dense embedding vector using fastembed (ONNX Runtime)."""
        return next(self._get_embedding_model().embed([text])).tolist()

    async def compute_embedding_async(self, text: str) -> list[float]:
        """Async entry point for embedding computation.

        fastembed's ONNX Runtime is thread-safe, so standard asyncio.to_thread
        is sufficient — no dedicated single-threaded executor needed.
        """
        return await asyncio.to_thread(self.compute_embedding, text)

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        return _cosine_similarity(a, b)

    def _get_embedding_model(self):
        if self._model is None:
            with self._model_lock:
                if self._model is None:  # re-check after acquiring lock
                    from fastembed import TextEmbedding  # lazy import

                    self._model = TextEmbedding(model_name=self._model_name)
        return self._model


# ── module-level helpers ───────────────────────────────────────────────────────


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors.

    For BAAI/bge models (current default) fastembed returns L2-normalized
    unit vectors, so norm_a == norm_b == 1.0 by construction and the result
    equals np.dot(a, b). The general form is kept for model-agnosticism.
    Returns 0.0 if either vector has zero norm.
    """
    import numpy as np  # lazy import — large package

    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm_a = float(np.linalg.norm(va))
    norm_b = float(np.linalg.norm(vb))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def _normalize(question: str) -> str:
    return question.lower().strip()


def has_temporal_reference(question: str) -> bool:
    """Return True if *question* contains a relative time reference.

    Used to skip the semantic similarity cache for time-sensitive questions
    so that "last quarter revenue" never matches "this quarter revenue".
    """
    return bool(_TEMPORAL_PATTERN.search(question))


_NUMERIC_PATTERN = re.compile(
    r"[€$£¥₹]?\d[\d,]*(?:\.\d+)?(?:\s*[kKmMbBtT%])?(?!\w)",
)


def _extract_numbers(text: str) -> list[str]:
    """Extract normalised numeric literals from *text*.

    Used to guard against semantic cache hits where the questions are
    identical in intent but differ in a threshold, limit, or amount
    (e.g. '>€60,000' vs '>€80,000').  Strips commas and lowercases
    suffixes so '80,000' and '80000' compare equal.
    """
    return sorted(m.group().replace(",", "").lower() for m in _NUMERIC_PATTERN.finditer(text))


def _fresh_condition(max_age_days: int):
    """Return a SQLAlchemy WHERE clause that excludes stale cache entries.

    An entry is *fresh* if it was created or last accessed within *max_age_days*.
    Returns ``True`` (no filter) when *max_age_days* is 0 (TTL disabled).
    """
    if max_age_days == 0:
        return True
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    return (
        func.coalesce(
            QueryCacheEntry.last_hit_at,
            QueryCacheEntry.created_at,
        )
        >= cutoff
    )


async def _bump_hit(entry_id: str, db: AsyncSession) -> None:
    """Atomically increment hit_count and set last_hit_at.

    Uses a server-side column expression so concurrent hits do not lose
    increments (no Python-side read-modify-write race).
    """
    await db.execute(
        update(QueryCacheEntry)
        .where(QueryCacheEntry.id == entry_id)
        .values(
            hit_count=QueryCacheEntry.hit_count + 1,
            last_hit_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _record_miss(connection_id: str, db: AsyncSession) -> None:
    """Atomically increment the miss counter for *connection_id*."""
    await db.execute(
        pg_insert(QueryCacheStats)
        .values(connection_id=connection_id, miss_count=1)
        .on_conflict_do_update(
            index_elements=["connection_id"],
            set_={"miss_count": QueryCacheStats.miss_count + 1},
        )
    )
    await db.commit()
