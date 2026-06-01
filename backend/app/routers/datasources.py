# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Datasources router — lists registered data source adapters with config schemas."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.dependencies import get_current_active_user
from ..datasources.registry import list_available_sources
from ..models.user import User

router = APIRouter(prefix="/datasources", tags=["datasources"])


@router.get("")
async def list_datasources(
    _: User = Depends(get_current_active_user),
) -> list[dict]:
    """Return all registered datasource adapters."""
    return list_available_sources()
