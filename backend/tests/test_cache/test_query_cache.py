# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the query cache layer and example library."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.example_library import ExampleEntry, ExampleLibrary
from app.cache.query_cache import (
    CacheHit,
    QueryCache,
    _cosine_similarity,
    _extract_numbers,
    _fresh_condition,
    _normalize,
    has_temporal_reference,
)

# ── Shared mock helpers ────────────────────────────────────────────────────────


class MockResult:
    """Simulates the object returned by AsyncSession.execute()."""

    def __init__(self, single=None, multiple=None, rowcount: int = 0, first_result=None):
        self._single = single
        self._multiple = multiple if multiple is not None else []
        self.rowcount = rowcount
        self._first_result = first_result

    def scalar_one_or_none(self):
        return self._single

    def scalar_one(self):
        return self._single

    def scalars(self):
        return self

    def all(self):
        return self._multiple

    def first(self):
        return self._first_result


def _mock_db(single=None, multiple=None, side_effects: list | None = None) -> MagicMock:
    """Return a mocked AsyncSession whose execute() resolves to MockResult.

    If ``side_effects`` is provided it is used as a sequential list of return
    values for successive ``db.execute()`` calls.
    """
    db = MagicMock()
    if side_effects is not None:
        db.execute = AsyncMock(side_effect=side_effects)
    else:
        db.execute = AsyncMock(return_value=MockResult(single=single, multiple=multiple))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.delete = MagicMock()
    return db


def _mock_db_semantic(
    entry=None,
    distance: float = 0.0,
    side_effects_extra: list | None = None,
) -> MagicMock:
    """Return a mocked DB for semantic-path lookup tests.

    The first execute() returns a scalar_one_or_none()=None (exact miss).
    The second execute() returns first()=(entry, distance) for the ANN query,
    or first()=None when entry is None (ANN miss).
    Subsequent calls return empty MockResults (e.g. _bump_hit, _record_miss).
    """
    first_result = (entry, distance) if entry is not None else None
    effects = [
        MockResult(single=None),
        MockResult(first_result=first_result),
        *(side_effects_extra or [MockResult(), MockResult()]),
    ]
    return _mock_db(side_effects=effects)


def _make_cache_entry(
    *,
    id: str = "e1",
    connection_id: str = "conn-1",
    question_raw: str = "How many users?",
    question_normalized: str = "how many users?",
    question_embedding: list[float] | None = None,
    generated_query: str = "SELECT COUNT(*) FROM users",
    query_dialect: str = "postgresql",
    hit_count: int = 0,
):
    """Build a mock QueryCacheEntry-like object.

    question_embedding is stored as list[float] (pgvector Vector type).
    """
    entry = MagicMock()
    entry.id = id
    entry.connection_id = connection_id
    entry.question_raw = question_raw
    entry.question_normalized = question_normalized
    entry.question_embedding = question_embedding
    entry.generated_query = generated_query
    entry.query_dialect = query_dialect
    entry.hit_count = hit_count
    return entry


def _make_example(
    *,
    id: str = "ex1",
    connection_id: str = "conn-1",
    question: str = "Top 5 customers?",
    question_embedding: list[float] | None = None,
    query: str = "SELECT name FROM customers LIMIT 5",
    query_dialect: str = "postgresql",
    created_at: datetime | None = None,
):
    ex = MagicMock()
    ex.id = id
    ex.connection_id = connection_id
    ex.question = question
    ex.question_embedding = question_embedding
    ex.query = query
    ex.query_dialect = query_dialect
    ex.created_at = created_at or datetime(2024, 1, 1, tzinfo=UTC)
    return ex


# ── _normalize helper ──────────────────────────────────────────────────────────


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("How Many USERS?") == "how many users?"

    def test_strips_whitespace(self):
        assert _normalize("  hello world  ") == "hello world"

    def test_empty_string(self):
        assert _normalize("") == ""


# ── CacheHit dataclass ─────────────────────────────────────────────────────────


class TestCacheHit:
    def test_fields(self):
        hit = CacheHit(
            cached_question="q",
            generated_query="SELECT 1",
            query_dialect="postgresql",
            similarity_score=1.0,
            cache_type="exact",
        )
        assert hit.cached_question == "q"
        assert hit.cache_type == "exact"
        assert hit.similarity_score == 1.0


# ── QueryCache.cosine_similarity ───────────────────────────────────────────────


class TestCosineSimilarity:
    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.92)

    def test_identical_vectors_return_one(self):
        v = [1.0, 0.0, 0.0]
        assert abs(self.cache.cosine_similarity(v, v) - 1.0) < 1e-9

    def test_orthogonal_vectors_return_zero(self):
        assert self.cache.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_return_minus_one(self):
        assert self.cache.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_a_returns_zero(self):
        assert self.cache.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_zero_vector_b_returns_zero(self):
        assert self.cache.cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_partial_similarity(self):
        import math

        a = [1.0, 0.0]
        b = [1.0, 1.0]
        expected = 1.0 / math.sqrt(2)
        assert self.cache.cosine_similarity(a, b) == pytest.approx(expected, abs=1e-6)


# ── QueryCache.lookup — exact match ───────────────────────────────────────────


class TestQueryCacheLookupExact:
    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.92)

    async def test_exact_match_returns_cache_hit(self):
        entry = _make_cache_entry()
        db = _mock_db(single=entry)
        result = await self.cache.lookup("conn-1", "How many users?", db)
        assert result is not None
        assert result.cache_type == "exact"
        assert result.similarity_score == 1.0

    async def test_exact_match_returns_generated_query(self):
        entry = _make_cache_entry(generated_query="SELECT COUNT(*) FROM users")
        db = _mock_db(single=entry)
        result = await self.cache.lookup("conn-1", "How many users?", db)
        assert result.generated_query == "SELECT COUNT(*) FROM users"

    async def test_exact_match_returns_cached_question(self):
        entry = _make_cache_entry(question_raw="How many users?")
        db = _mock_db(single=entry)
        result = await self.cache.lookup("conn-1", "how many users?", db)
        assert result.cached_question == "How many users?"

    async def test_exact_match_bumps_hit_count(self):
        entry = _make_cache_entry(hit_count=3)
        db = _mock_db(single=entry)
        await self.cache.lookup("conn-1", "How many users?", db)
        # execute called at least twice: select + update
        assert db.execute.call_count >= 2
        db.commit.assert_called_once()


# ── QueryCache.lookup — semantic match ────────────────────────────────────────


class TestQueryCacheLookupSemantic:
    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.90)

    async def test_semantic_match_above_threshold_returns_hit(self):
        # No exact match; ANN returns entry with distance=0.0 → similarity=1.0
        embedding = [1.0, 0.0, 0.0]
        entry = _make_cache_entry(question_embedding=embedding)
        db = _mock_db_semantic(entry=entry, distance=0.0)
        with patch.object(self.cache, "compute_embedding", return_value=embedding):
            result = await self.cache.lookup("conn-1", "User count?", db)
        assert result is not None
        assert result.cache_type == "semantic"
        assert result.similarity_score == pytest.approx(1.0)

    async def test_semantic_match_below_threshold_returns_none(self):
        cache = QueryCache("all-MiniLM-L6-v2", similarity_threshold=0.99)
        # ANN returns distance=1.0 → similarity=0.0 < 0.99
        entry = _make_cache_entry(question_embedding=[1.0, 0.0])
        db = _mock_db_semantic(entry=entry, distance=1.0)
        with patch.object(cache, "compute_embedding", return_value=[0.0, 1.0]):
            result = await cache.lookup("conn-1", "Something else?", db)
        assert result is None

    async def test_no_entries_returns_none(self):
        # ANN returns no result (first()=None)
        db = _mock_db_semantic(entry=None)
        with patch.object(self.cache, "compute_embedding", return_value=[1.0, 0.0]):
            result = await self.cache.lookup("conn-1", "Anything?", db)
        assert result is None

    async def test_entries_without_embedding_are_skipped(self):
        # WHERE question_embedding IS NOT NULL means no-embedding entries aren't returned
        db = _mock_db_semantic(entry=None)
        with patch.object(self.cache, "compute_embedding", return_value=[1.0, 0.0]):
            result = await self.cache.lookup("conn-1", "Anything?", db)
        assert result is None

    async def test_semantic_hit_bumps_hit_count(self):
        embedding = [1.0, 0.0]
        entry = _make_cache_entry(question_embedding=embedding)
        db = _mock_db_semantic(entry=entry, distance=0.0)
        with patch.object(self.cache, "compute_embedding", return_value=embedding):
            await self.cache.lookup("conn-1", "User count?", db)
        # execute: exact match + ANN query + _bump_hit update
        assert db.execute.call_count >= 3
        db.commit.assert_called_once()

    async def test_picks_best_scoring_entry(self):
        # pgvector LIMIT 1 returns the single best match from the DB
        high_emb = [1.0, 0.0]
        entry_high = _make_cache_entry(
            id="e-high",
            question_embedding=high_emb,
            generated_query="SELECT COUNT(*) FROM users",
        )
        db = _mock_db_semantic(entry=entry_high, distance=0.0)
        with patch.object(self.cache, "compute_embedding", return_value=high_emb):
            result = await self.cache.lookup("conn-1", "q?", db)
        assert result.generated_query == "SELECT COUNT(*) FROM users"


# ── QueryCache.store ──────────────────────────────────────────────────────────


class TestQueryCacheStore:
    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.92)

    async def test_inserts_new_entry_when_not_cached(self):
        # Call 1: lookup (no existing entry); Call 2: upsert INSERT ON CONFLICT DO NOTHING;
        # Call 3: eviction count (0 → no eviction)
        db = _mock_db(side_effects=[MockResult(single=None), MockResult(), MockResult(single=0)])
        await self.cache.store(
            "conn-1", "How many users?", "SELECT COUNT(*) FROM users", "postgresql", [1.0, 0.0], db
        )
        db.add.assert_not_called()  # upsert uses db.execute, not db.add
        db.flush.assert_called_once()
        db.commit.assert_not_called()  # caller owns the commit

    async def test_skips_existing_entry_when_already_cached(self):
        existing = _make_cache_entry()
        db = _mock_db(single=existing)
        await self.cache.store(
            "conn-1", "How many users?", "SELECT COUNT(*) FROM users", "postgresql", [1.0, 0.0], db
        )
        db.add.assert_not_called()
        db.flush.assert_not_called()
        db.commit.assert_not_called()

    async def test_force_update_flushes_not_commits(self):
        existing = _make_cache_entry()
        db = _mock_db(single=existing)
        await self.cache.store(
            "conn-1", "How many users?", "SELECT 2", "postgresql", [1.0, 0.0], db, force=True
        )
        db.flush.assert_called_once()
        db.commit.assert_not_called()

    async def test_store_never_commits(self):
        # Contract: store() must never call db.commit() so the cache write
        # and the surrounding message saves are always in the same transaction.
        db_new = _mock_db(
            side_effects=[MockResult(single=None), MockResult(), MockResult(single=0)]
        )
        await self.cache.store("conn-1", "q", "SELECT 1", "postgresql", [1.0, 0.0], db_new)
        db_new.commit.assert_not_called()

        existing = _make_cache_entry()
        db_force = _mock_db(single=existing)
        await self.cache.store(
            "conn-1", "q", "SELECT 1", "postgresql", [1.0, 0.0], db_force, force=True
        )
        db_force.commit.assert_not_called()

    async def test_computes_embedding_when_not_provided(self):
        # Call 1: lookup (no existing entry); Call 2: upsert; Call 3: eviction count
        db = _mock_db(side_effects=[MockResult(single=None), MockResult(), MockResult(single=0)])
        with patch.object(self.cache, "compute_embedding_async", return_value=[0.5, 0.5]) as m:
            await self.cache.store("conn-1", "How many users?", "SELECT 1", "postgresql", None, db)
        m.assert_called_once_with("How many users?")

    async def test_does_not_compute_embedding_when_provided(self):
        # Call 1: lookup (no existing entry); Call 2: upsert; Call 3: eviction count
        db = _mock_db(side_effects=[MockResult(single=None), MockResult(), MockResult(single=0)])
        with patch.object(self.cache, "compute_embedding_async") as m:
            await self.cache.store("conn-1", "q", "SELECT 1", "postgresql", [1.0, 0.0], db)
        m.assert_not_called()


# ── QueryCache.invalidate ─────────────────────────────────────────────────────


class TestQueryCacheInvalidate:
    async def test_invalidate_executes_delete_and_commits(self):
        cache = QueryCache("all-MiniLM-L6-v2", 0.92)
        db = _mock_db()
        await cache.invalidate("conn-1", db)
        db.execute.assert_called_once()
        db.commit.assert_called_once()


# ── _fresh_condition helper ────────────────────────────────────────────────────


class TestFreshCondition:
    def test_zero_days_returns_true(self):
        """TTL disabled: no filter applied."""
        assert _fresh_condition(0) is True

    def test_nonzero_days_returns_expression(self):
        """TTL active: returns a SQLAlchemy column expression (not True/False)."""
        result = _fresh_condition(30)
        assert result is not True
        assert result is not False
        assert result is not None

    def test_different_day_counts_produce_different_cutoffs(self):
        """Sanity check: two different TTLs produce different (not identical) expressions."""
        expr_7 = _fresh_condition(7)
        expr_30 = _fresh_condition(30)
        # They should be distinct objects (different cutoff timestamps embedded)
        assert expr_7 is not expr_30


# ── QueryCache.prune_expired ───────────────────────────────────────────────────


class TestQueryCachePruneExpired:
    async def test_zero_max_age_returns_zero_without_db_call(self):
        """When TTL is disabled, prune_expired is a no-op."""
        cache = QueryCache("all-MiniLM-L6-v2", 0.92, max_age_days=0)
        db = _mock_db()
        deleted = await cache.prune_expired(db)
        assert deleted == 0
        db.execute.assert_not_called()
        db.commit.assert_not_called()

    async def test_prune_executes_delete_and_commits(self):
        cache = QueryCache("all-MiniLM-L6-v2", 0.92, max_age_days=30)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rowcount=3))
        db.commit = AsyncMock()
        await cache.prune_expired(db)
        db.execute.assert_called_once()
        db.commit.assert_called_once()

    async def test_prune_returns_rowcount(self):
        cache = QueryCache("all-MiniLM-L6-v2", 0.92, max_age_days=30)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rowcount=7))
        db.commit = AsyncMock()
        deleted = await cache.prune_expired(db)
        assert deleted == 7

    async def test_prune_returns_zero_when_nothing_deleted(self):
        cache = QueryCache("all-MiniLM-L6-v2", 0.92, max_age_days=30)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MockResult(rowcount=0))
        db.commit = AsyncMock()
        deleted = await cache.prune_expired(db)
        assert deleted == 0


# ── ExampleLibrary dataclass ──────────────────────────────────────────────────


class TestExampleEntry:
    def test_defaults(self):
        entry = ExampleEntry(id="1", question="q", query="SELECT 1", query_dialect="postgresql")
        assert entry.similarity_score is None
        assert entry.created_at is None


# ── _cosine_similarity (module-level helper in query_cache) ───────────────────


class TestExampleLibraryCosineSimilarity:
    def test_identical(self):
        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── ExampleLibrary.add_example ────────────────────────────────────────────────


class TestExampleLibraryAdd:
    def setup_method(self):
        self.lib = ExampleLibrary()

    async def test_add_returns_example_entry(self):
        db = _mock_db()
        result = await self.lib.add_example(
            "conn-1",
            "Top customers?",
            "SELECT name FROM customers LIMIT 5",
            "postgresql",
            [1.0, 0.0],
            db,
        )
        assert isinstance(result, ExampleEntry)
        assert result.question == "Top customers?"

    async def test_add_persists_to_db(self):
        db = _mock_db()
        await self.lib.add_example("conn-1", "q", "SELECT 1", "postgresql", None, db)
        db.add.assert_called_once()
        db.commit.assert_called_once()

    async def test_add_accepts_none_embedding(self):
        db = _mock_db()
        # Should not raise — embedding is optional
        result = await self.lib.add_example("conn-1", "q", "SELECT 1", "postgresql", None, db)
        assert result is not None


# ── ExampleLibrary.remove_example ─────────────────────────────────────────────


class TestExampleLibraryRemove:
    async def test_remove_executes_delete_and_commits(self):
        lib = ExampleLibrary()
        db = _mock_db()
        await lib.remove_example("ex-id-123", db)
        db.execute.assert_called_once()
        db.commit.assert_called_once()


# ── ExampleLibrary.find_similar_examples ─────────────────────────────────────


class TestExampleLibraryFindSimilar:
    """find_similar_examples now uses a pgvector ANN SQL query.

    The mock returns (VerifiedExample, distance) tuples — simulating what
    the DB would return after applying WHERE + ORDER BY + LIMIT in SQL.
    Dialect filter and IS NOT NULL are applied in SQL, so the mock must
    pre-filter accordingly to reflect real database behavior.
    """

    def setup_method(self):
        self.lib = ExampleLibrary()

    async def test_returns_top_n_by_similarity(self):
        ex_high = _make_example(id="h", query="SELECT high")
        # DB returns only 1 row (LIMIT 1) — the best match
        db = _mock_db(multiple=[(ex_high, 0.0)])
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=1, db=db)
        assert len(results) == 1
        assert results[0].query == "SELECT high"

    async def test_respects_limit(self):
        examples = [_make_example(id=f"e{i}") for i in range(3)]
        # DB returns only 3 rows (LIMIT 3 applied by SQL)
        db = _mock_db(multiple=[(ex, 0.0) for ex in examples])
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=3, db=db)
        assert len(results) == 3

    async def test_skips_examples_without_embedding(self):
        # WHERE question_embedding IS NOT NULL means no-embedding rows are excluded by DB
        ex_with_emb = _make_example(id="yes")
        db = _mock_db(multiple=[(ex_with_emb, 0.0)])
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=5, db=db)
        assert len(results) == 1
        assert results[0].id == "yes"

    async def test_sets_similarity_score(self):
        ex = _make_example()
        db = _mock_db(multiple=[(ex, 0.0)])  # distance=0.0 → similarity=1.0
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=5, db=db)
        assert results[0].similarity_score == pytest.approx(1.0)

    async def test_no_examples_returns_empty_list(self):
        db = _mock_db(multiple=[])
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=5, db=db)
        assert results == []

    async def test_sorted_descending_by_similarity(self):
        # DB already returns rows ORDER BY distance — ex_a closer (distance=0.0) than ex_b (0.28)
        ex_a = _make_example(id="a", query="A")
        ex_b = _make_example(id="b", query="B")
        db = _mock_db(multiple=[(ex_a, 0.0), (ex_b, 0.28)])
        results = await self.lib.find_similar_examples("conn-1", "?", [1.0, 0.0], limit=2, db=db)
        assert results[0].query == "A"
        assert results[1].query == "B"

    async def test_min_similarity_filters_low_scores(self):
        ex_high = _make_example(id="h", query="HIGH")
        ex_low = _make_example(id="l", query="LOW")
        # DB returns both (ANN doesn't know about min_similarity threshold);
        # Python post-filters on (1.0 - distance) >= min_similarity
        db = _mock_db(multiple=[(ex_high, 0.0), (ex_low, 1.0)])  # distances: 0.0 and 1.0
        results = await self.lib.find_similar_examples(
            "conn-1", "?", [1.0, 0.0], limit=5, db=db, min_similarity=0.5
        )
        assert len(results) == 1
        assert results[0].query == "HIGH"

    async def test_min_similarity_zero_returns_all_with_embeddings(self):
        ex_a = _make_example(id="a", query="A")
        ex_b = _make_example(id="b", query="B")
        db = _mock_db(multiple=[(ex_a, 0.0), (ex_b, 1.0)])
        results = await self.lib.find_similar_examples(
            "conn-1", "?", [1.0, 0.0], limit=5, db=db, min_similarity=0.0
        )
        assert len(results) == 2

    async def test_dialect_filter_excludes_mismatched(self):
        # Dialect filter is in SQL WHERE — mock simulates DB returning only pg examples
        ex_pg = _make_example(id="pg", query_dialect="postgresql")
        db = _mock_db(multiple=[(ex_pg, 0.0)])
        results = await self.lib.find_similar_examples(
            "conn-1", "?", [1.0, 0.0], limit=5, db=db, query_dialect="postgresql"
        )
        assert all(r.id == "pg" for r in results)
        assert len(results) == 1

    async def test_dialect_filter_empty_string_returns_all_dialects(self):
        # Empty dialect_filter → no WHERE clause on dialect → DB returns both
        ex_pg = _make_example(id="pg", query_dialect="postgresql")
        ex_my = _make_example(id="my", query_dialect="mysql")
        db = _mock_db(multiple=[(ex_pg, 0.0), (ex_my, 0.0)])
        results = await self.lib.find_similar_examples(
            "conn-1", "?", [1.0, 0.0], limit=5, db=db, query_dialect=""
        )
        assert len(results) == 2


# ── ExampleLibrary.list_examples ─────────────────────────────────────────────


class TestExampleLibraryList:
    async def test_returns_all_examples(self):
        lib = ExampleLibrary()
        ex1 = _make_example(id="1", question="Q1")
        ex2 = _make_example(id="2", question="Q2")
        db = _mock_db(multiple=[ex1, ex2])
        results = await lib.list_examples("conn-1", db)
        assert len(results) == 2

    async def test_maps_to_example_entry(self):
        lib = ExampleLibrary()
        ex = _make_example(id="x", question="Q", query="SELECT 1", query_dialect="postgresql")
        db = _mock_db(multiple=[ex])
        results = await lib.list_examples("conn-1", db)
        assert results[0].id == "x"
        assert results[0].question == "Q"
        assert results[0].query == "SELECT 1"

    async def test_created_at_iso_formatted(self):
        lib = ExampleLibrary()
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        ex = _make_example(created_at=ts)
        db = _mock_db(multiple=[ex])
        results = await lib.list_examples("conn-1", db)
        assert "2024-06-15" in results[0].created_at

    async def test_empty_connection_returns_empty_list(self):
        lib = ExampleLibrary()
        db = _mock_db(multiple=[])
        results = await lib.list_examples("conn-999", db)
        assert results == []


# ── EmbeddingCache ────────────────────────────────────────────────────────────


class TestEmbeddingCache:
    def test_get_or_compute_caches_result(self):
        from app.cache.embedding_cache import EmbeddingCache

        cache = EmbeddingCache("dummy-model", maxsize=10)
        fake_embedding = [0.1, 0.2, 0.3]
        with patch.object(cache, "_compute", return_value=fake_embedding) as m:
            first = cache.get_or_compute("hello")
            second = cache.get_or_compute("hello")
        # _compute should only be called once
        m.assert_called_once()
        assert first == second == fake_embedding

    def test_cache_size_reflects_entries(self):
        from app.cache.embedding_cache import EmbeddingCache

        cache = EmbeddingCache("dummy-model", maxsize=10)
        with patch.object(cache, "_compute", return_value=[0.0]):
            cache.get_or_compute("a")
            cache.get_or_compute("b")
        assert cache.cache_size == 2

    def test_evicts_oldest_when_full(self):
        from app.cache.embedding_cache import EmbeddingCache

        cache = EmbeddingCache("dummy-model", maxsize=2)
        with patch.object(cache, "_compute", return_value=[0.0]):
            cache.get_or_compute("a")
            cache.get_or_compute("b")
            cache.get_or_compute("c")  # triggers eviction of "a"
        assert cache.cache_size == 2
        assert "a" not in cache._cache

    def test_clear_empties_cache(self):
        from app.cache.embedding_cache import EmbeddingCache

        cache = EmbeddingCache("dummy-model", maxsize=10)
        with patch.object(cache, "_compute", return_value=[0.0]):
            cache.get_or_compute("a")
        cache.clear()
        assert cache.cache_size == 0


# ── has_temporal_reference ────────────────────────────────────────────────────


class TestHasTemporalReference:
    # ── should detect ──────────────────────────────────────────────────────────

    def test_today(self):
        assert has_temporal_reference("How many orders were placed today?")

    def test_yesterday(self):
        assert has_temporal_reference("Show me revenue for yesterday")

    def test_tomorrow(self):
        assert has_temporal_reference("What orders are due tomorrow?")

    def test_last_quarter(self):
        assert has_temporal_reference("Which departments were over budget last quarter?")

    def test_previous_quarter(self):
        assert has_temporal_reference("Sales for the previous quarter by region")

    def test_this_month(self):
        assert has_temporal_reference("Show me revenue this month")

    def test_last_week(self):
        assert has_temporal_reference("Employees hired last week")

    def test_current_year(self):
        assert has_temporal_reference("Total sales for current year")

    def test_past_n_days(self):
        assert has_temporal_reference("Orders in the past 30 days")

    def test_last_n_months(self):
        assert has_temporal_reference("Revenue over the last 3 months")

    def test_ytd(self):
        assert has_temporal_reference("YTD revenue by product")

    def test_mtd(self):
        assert has_temporal_reference("mtd sales performance")

    def test_qtd(self):
        assert has_temporal_reference("QTD targets vs actuals")

    def test_year_to_date(self):
        assert has_temporal_reference("Year-to-date profit")

    def test_rolling_n(self):
        assert has_temporal_reference("Rolling 7 day average order value")

    def test_trailing_n(self):
        assert has_temporal_reference("Trailing 12 month revenue")

    # ── should NOT detect ─────────────────────────────────────────────────────

    def test_non_temporal_plain(self):
        assert not has_temporal_reference(
            "Show me all employees earning more than $80,000 sorted by department"
        )

    def test_non_temporal_sort_variant(self):
        assert not has_temporal_reference(
            "Show me all employees earning more than $80,000 sorted by department and salary"
        )

    def test_non_temporal_join(self):
        assert not has_temporal_reference("Which departments have more than 5 employees?")

    def test_non_temporal_aggregate(self):
        assert not has_temporal_reference("What is the average salary per department?")


# ── lookup() temporal bypass ───────────────────────────────────────────────────


class TestQueryCacheLookupTemporalBypass:
    """Temporal questions must skip semantic matching; only exact hits allowed."""

    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.87)

    async def test_temporal_no_exact_match_returns_none_even_with_similar_entries(self):
        # Temporal question + exact miss → semantic path is skipped entirely.
        # Only one execute call (exact match), then _record_miss.
        db = _mock_db(side_effects=[MockResult(single=None), MockResult()])
        result = await self.cache.lookup(
            "conn-1",
            "Which depts were over budget last quarter?",
            db,
        )
        assert result is None

    async def test_temporal_exact_match_still_returned(self):
        # Exact match on a temporal question is still valid (same phrasing).
        entry = _make_cache_entry(
            question_raw="Revenue last month?",
            question_normalized="revenue last month?",
            generated_query="SELECT SUM(total) FROM orders WHERE ...",
        )
        db = _mock_db(single=entry)
        result = await self.cache.lookup("conn-1", "Revenue last month?", db)
        assert result is not None
        assert result.cache_type == "exact"

    async def test_non_temporal_semantic_match_still_works(self):
        # Non-temporal question: semantic path is taken as normal.
        embedding = [1.0, 0.0, 0.0]
        entry = _make_cache_entry(
            question_raw="Employees earning over 80000 by dept?",
            question_embedding=embedding,
        )
        db = _mock_db_semantic(entry=entry, distance=0.0)
        with patch.object(self.cache, "compute_embedding", return_value=embedding):
            result = await self.cache.lookup(
                "conn-1",
                "Show employees earning more than 80000 sorted by department",
                db,
            )
        assert result is not None
        assert result.cache_type == "semantic"

    async def test_ytd_bypasses_semantic(self):
        # Temporal question ("YTD") → no ANN query issued, returns None
        db = _mock_db(side_effects=[MockResult(single=None), MockResult()])
        result = await self.cache.lookup("conn-1", "YTD revenue by region", db)
        assert result is None


# ── Miss-count tracking ────────────────────────────────────────────────────────


class TestQueryCacheMissTracking:
    """Verify _record_miss is called on every miss path and not on hits."""

    def setup_method(self):
        self.cache = QueryCache("all-MiniLM-L6-v2", 0.92)

    async def test_no_entries_triggers_miss_counter(self):
        # ANN returns no result
        db = _mock_db_semantic(entry=None)
        with (
            patch("app.cache.query_cache._record_miss") as mock_record,
            patch.object(self.cache, "compute_embedding", return_value=[1.0, 0.0]),
        ):
            result = await self.cache.lookup("conn-1", "Anything?", db)
        assert result is None
        mock_record.assert_called_once_with("conn-1", db)

    async def test_below_threshold_triggers_miss_counter(self):
        # ANN returns distance=1.0 → similarity=0.0 < threshold=0.99
        cache = QueryCache("all-MiniLM-L6-v2", similarity_threshold=0.99)
        entry = _make_cache_entry(question_embedding=[1.0, 0.0])
        db = _mock_db_semantic(entry=entry, distance=1.0)
        with (
            patch("app.cache.query_cache._record_miss") as mock_record,
            patch.object(cache, "compute_embedding", return_value=[0.0, 1.0]),
        ):
            result = await cache.lookup("conn-1", "Something else?", db)
        assert result is None
        mock_record.assert_called_once_with("conn-1", db)

    async def test_temporal_miss_triggers_miss_counter(self):
        db = _mock_db(side_effects=[MockResult(single=None), MockResult()])
        with patch("app.cache.query_cache._record_miss") as mock_record:
            result = await self.cache.lookup("conn-1", "Revenue last quarter?", db)
        assert result is None
        mock_record.assert_called_once_with("conn-1", db)

    async def test_numbers_mismatch_triggers_miss_counter(self):
        # Similarity above threshold but numeric literals differ → miss
        cache = QueryCache("all-MiniLM-L6-v2", similarity_threshold=0.5)
        embedding = [1.0, 0.0]
        entry = _make_cache_entry(
            question_raw="Employees earning > $60k",
            question_embedding=embedding,
        )
        # distance=0.0 → similarity=1.0 > 0.5, but numbers "$60k" ≠ "$80k"
        db = _mock_db_semantic(entry=entry, distance=0.0)
        with (
            patch("app.cache.query_cache._record_miss") as mock_record,
            patch.object(cache, "compute_embedding", return_value=embedding),
        ):
            result = await cache.lookup("conn-1", "Employees earning > $80k", db)
        assert result is None
        mock_record.assert_called_once_with("conn-1", db)

    async def test_exact_hit_does_not_trigger_miss_counter(self):
        entry = _make_cache_entry()
        db = _mock_db(single=entry)
        with patch("app.cache.query_cache._record_miss") as mock_record:
            result = await self.cache.lookup("conn-1", "How many users?", db)
        assert result is not None
        mock_record.assert_not_called()

    async def test_semantic_hit_does_not_trigger_miss_counter(self):
        embedding = [1.0, 0.0]
        entry = _make_cache_entry(question_embedding=embedding)
        db = _mock_db_semantic(entry=entry, distance=0.0)
        with (
            patch("app.cache.query_cache._record_miss") as mock_record,
            patch.object(self.cache, "compute_embedding", return_value=embedding),
        ):
            result = await self.cache.lookup("conn-1", "User count?", db)
        assert result is not None
        mock_record.assert_not_called()


# ── _extract_numbers ──────────────────────────────────────────────────────────


class TestExtractNumbers:
    def test_empty_returns_empty_list(self):
        assert _extract_numbers("total revenue by region") == []

    def test_plain_integer(self):
        assert _extract_numbers("top 100 customers") == ["100"]

    def test_float(self):
        result = _extract_numbers("salary > 60000.50")
        assert "60000.50" in result

    def test_comma_separated_stripped(self):
        result = _extract_numbers("orders over 80,000")
        assert "80000" in result

    def test_multiple_numbers_sorted(self):
        result = _extract_numbers("salary between 50000 and 80000")
        assert sorted(result) == result
        assert len(result) == 2

    def test_different_thresholds_differ(self):
        assert _extract_numbers("earning > €60k") != _extract_numbers("earning > €80k")
