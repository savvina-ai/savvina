# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared helpers for tests that exercise Alembic revision files."""

import importlib.util
from pathlib import Path
from types import ModuleType

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def load_migration(filename: str) -> ModuleType:
    """Import one Alembic revision file as a plain module (no Alembic context)."""
    spec = importlib.util.spec_from_file_location(
        filename.removesuffix(".py"), VERSIONS_DIR / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
