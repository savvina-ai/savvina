# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Verified example library — question → query pairs for few-shot prompting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import delete, select

from ..models.example import VerifiedExample

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Intentionally low: loosely related examples are better than none; the LLM discards
# unhelpful ones.  Raising this too high leaves the few-shot prompt empty for novel questions.
_MIN_EXAMPLE_SIMILARITY: float = 0.4


@dataclass
class ExampleEntry:
    """A single verified example, optionally annotated with a similarity score."""

    id: str
    question: str
    query: str
    query_dialect: str
    similarity_score: float | None = None  # set during similarity search
    created_at: str | None = None


class ExampleLibrary:
    """
    Stores user-verified question → query pairs for few-shot prompting.

    When a user gives thumbs-up feedback on a generated query it is added here.
    The :meth:`find_similar_examples` method retrieves the most relevant
    examples to include in the LLM system prompt, improving query accuracy
    over time as the library grows.
    """

    async def add_example(
        self,
        connection_id: str,
        question: str,
        query: str,
        query_dialect: str,
        embedding: list[float] | None,
        db: AsyncSession,
    ) -> ExampleEntry:
        """
        Add a verified example.

        *embedding* should be pre-computed by the caller (using
        :meth:`QueryCache.compute_embedding`) so that the library itself
        does not need an embedding model.
        """
        example = VerifiedExample(
            id=str(uuid.uuid4()),
            connection_id=connection_id,
            question=question,
            question_embedding=embedding,
            query=query,
            query_dialect=query_dialect,
        )
        db.add(example)
        await db.commit()
        return ExampleEntry(
            id=example.id,
            question=example.question,
            query=example.query,
            query_dialect=example.query_dialect,
        )

    async def remove_example(self, example_id: str, db: AsyncSession) -> None:
        """Delete a verified example by ID."""
        await db.execute(delete(VerifiedExample).where(VerifiedExample.id == example_id))
        await db.commit()

    async def find_similar_examples(
        self,
        connection_id: str,
        question: str,  # kept for logging / future exact-match use
        embedding: list[float],
        db: AsyncSession,
        limit: int = 3,
        query_dialect: str = "",
        min_similarity: float = _MIN_EXAMPLE_SIMILARITY,
    ) -> list[ExampleEntry]:
        """Return up to *limit* examples most similar to *question*.

        Similarity is computed as cosine similarity between *embedding* and
        each stored ``question_embedding``.  Examples without a stored
        embedding, below *min_similarity*, or with a mismatched *query_dialect*
        (when non-empty) are skipped.
        """
        dist_col = VerifiedExample.question_embedding.cosine_distance(embedding)
        where_clauses = [
            VerifiedExample.connection_id == connection_id,
            VerifiedExample.question_embedding.is_not(None),
        ]
        if query_dialect:
            where_clauses.append(VerifiedExample.query_dialect == query_dialect)

        result = await db.execute(
            select(VerifiedExample, dist_col.label("distance"))
            .where(*where_clauses)
            .order_by(dist_col)
            .limit(limit)
        )
        rows = result.all()

        return [
            ExampleEntry(
                id=ex.id,
                question=ex.question,
                query=ex.query,
                query_dialect=ex.query_dialect,
                similarity_score=1.0 - distance,
                created_at=ex.created_at.isoformat() if ex.created_at else None,
            )
            for ex, distance in rows
            if (1.0 - distance) >= min_similarity
        ][:limit]

    async def list_examples(self, connection_id: str, db: AsyncSession) -> list[ExampleEntry]:
        """Return all examples for *connection_id*, ordered by creation time."""
        result = await db.execute(
            select(VerifiedExample)
            .where(VerifiedExample.connection_id == connection_id)
            .order_by(VerifiedExample.created_at)
        )
        examples = result.scalars().all()
        return [
            ExampleEntry(
                id=ex.id,
                question=ex.question,
                query=ex.query,
                query_dialect=ex.query_dialect,
                created_at=ex.created_at.isoformat() if ex.created_at else None,
            )
            for ex in examples
        ]
