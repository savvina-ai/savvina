# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Data-source adapter registry.

Usage pattern::

    @register_datasource("mydb")
    class MyDBDataSource(BaseDataSource):
        ...

Calling ``create_datasource("mydb")`` instantiates the class registered
under that key.  All adapter modules must be imported before the registry
is queried; the app package's ``__init__.py`` handles this via the
``from . import datasources`` trigger in ``main.py``.
"""

from __future__ import annotations

from .base import BaseDataSource

_REGISTRY: dict[str, type[BaseDataSource]] = {}


def register_datasource(source_type: str):
    """Class decorator that registers a data source adapter under *source_type*."""

    def wrapper(cls: type[BaseDataSource]) -> type[BaseDataSource]:
        _REGISTRY[source_type] = cls
        return cls

    return wrapper


def get_datasource_class(source_type: str) -> type[BaseDataSource]:
    if source_type not in _REGISTRY:
        available = list(_REGISTRY.keys())
        raise ValueError(f"Unknown data source: '{source_type}'. Available: {available}")
    return _REGISTRY[source_type]


def create_datasource(source_type: str) -> BaseDataSource:
    cls = get_datasource_class(source_type)
    return cls()


def list_available_sources() -> list[dict]:
    return [
        {
            "source_type": cls.source_type,
            "display_name": cls.display_name,
            "icon": cls.icon,
            "query_dialect": cls.query_dialect,
            "config_schema": cls.get_config_schema(),
        }
        for cls in _REGISTRY.values()
    ]
