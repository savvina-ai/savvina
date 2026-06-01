# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

from .adapters import (
    mysql,  # noqa: F401  — triggers @register_datasource("mysql")
    postgresql,  # noqa: F401  — triggers @register_datasource("postgresql")
)
