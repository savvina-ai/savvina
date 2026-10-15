# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def stored_semantic_model() -> Iterator[AsyncMock]:
    """Stub the chat path's read of the semantic store; the stored model is None.

    The chat pipeline loads the model with ``semantic_store.load_model_cached(db, id)``.
    Service tests drive it with a mock session, so they stub that seam rather than
    stand up the semantic tables. Set ``.return_value`` to inject a model.
    """
    with patch(
        "app.services.chat_service.semantic_store.load_model_cached",
        new=AsyncMock(return_value=None),
    ) as load_model:
        yield load_model


@pytest.fixture(autouse=True)
def _clear_semantic_filter_caches() -> None:
    """The chat path memoises the cleaned and the narrowed semantic model and the
    edge-drop warnings per process; clear all three so no test sees another's entries."""
    from app.services.schema_pruning import clear_semantic_filter_caches
    from app.services.semantic_privacy import clear_hidden_identifier_cache

    clear_semantic_filter_caches()
    clear_hidden_identifier_cache()
