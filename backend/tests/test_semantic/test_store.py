# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for the normalised semantic store."""

from sqlalchemy import select

from app.models.semantic_store import (
    SemanticModelRow,
    SemanticTableRow,
)


class TestRowShapes:
    async def test_is_user_edited_derives_from_user_edited_fields(self) -> None:
        row = SemanticTableRow(user_edited_fields=[])
        assert row.is_user_edited is False
        row.user_edited_fields = ["description"]
        assert row.is_user_edited is True

    async def test_model_row_round_trips(self, semantic_db, connection_id) -> None:
        semantic_db.add(
            SemanticModelRow(
                connection_id=connection_id,
                source_dialect="postgresql",
                notes=["budgets are quarterly"],
                time_expressions={"last quarter": "date_trunc('quarter', now())"},
            )
        )
        await semantic_db.commit()
        semantic_db.expunge_all()
        stored = await semantic_db.scalar(
            select(SemanticModelRow).where(SemanticModelRow.connection_id == connection_id)
        )
        assert stored is not None
        assert stored.version == 1
        assert stored.generation_status == "idle"
        assert stored.generation_run_id is None
        assert stored.notes == ["budgets are quarterly"]
        assert stored.time_expressions == {"last quarter": "date_trunc('quarter', now())"}
        assert stored.is_user_edited is False
