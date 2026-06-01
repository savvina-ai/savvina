# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""LLM provider registry.

Usage pattern::

    @register_provider("myprovider")
    class MyProvider(BaseLLMProvider):
        ...

Calling ``create_provider("myprovider", api_key=...)`` instantiates the class
registered under that key.  All provider modules must be imported before the
registry is queried; the app package's ``__init__.py`` handles this via the
``from . import providers`` trigger in ``main.py``.
"""

from __future__ import annotations

from .base import BaseLLMProvider

_REGISTRY: dict[str, type[BaseLLMProvider]] = {}


def register_provider(name: str):
    """Class decorator that registers an LLM provider under *name*."""

    def wrapper(cls: type[BaseLLMProvider]) -> type[BaseLLMProvider]:
        _REGISTRY[name] = cls
        return cls

    return wrapper


def get_provider_class(name: str) -> type[BaseLLMProvider]:
    if name not in _REGISTRY:
        available = list(_REGISTRY.keys())
        raise ValueError(f"Unknown provider: '{name}'. Available: {available}")
    return _REGISTRY[name]


def create_provider(name: str, **kwargs) -> BaseLLMProvider:
    """Instantiate a registered provider by name, forwarding *kwargs* to its constructor."""
    cls = get_provider_class(name)
    return cls(**kwargs)


def list_available_providers() -> list[dict]:
    """Return static metadata for all registered providers."""
    return [
        {
            "name": cls.provider_name,
            "display_name": cls.display_name,
            "available_models": cls.get_available_models(),
        }
        for cls in _REGISTRY.values()
    ]
