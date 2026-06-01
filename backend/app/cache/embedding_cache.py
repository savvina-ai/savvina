# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""In-memory LRU cache for text embeddings.

Avoids redundant fastembed TextEmbedding.embed() calls for identical inputs.
"""

from __future__ import annotations

import asyncio


class EmbeddingCache:
    """
    Thin in-memory wrapper that caches embedding vectors by input text.

    The underlying fastembed ONNX model is lazy-loaded on first use
    so that importing this module does not trigger a model download.
    """

    def __init__(self, model_name: str, maxsize: int = 1000) -> None:
        self._model_name = model_name
        self._model = None
        self._maxsize = maxsize
        # Ordered dict behaviour comes from Python 3.7+ insertion-ordered dicts
        self._cache: dict[str, list[float]] = {}
        # Per-key in-flight futures: if a coroutine is already computing an
        # embedding for a given text, later arrivals wait on the same future
        # instead of launching duplicate thread-pool tasks.
        self._in_flight: dict[str, asyncio.Future[list[float]]] = {}

    def get_or_compute(self, text: str) -> list[float]:
        """Return cached embedding or compute and cache it."""
        if text in self._cache:
            return self._cache[text]
        embedding = self._compute(text)
        # Evict oldest entry if at capacity
        if len(self._cache) >= self._maxsize:
            self._cache.pop(next(iter(self._cache)))
        self._cache[text] = embedding
        return embedding

    def _compute(self, text: str) -> list[float]:
        if self._model is None:
            from fastembed import TextEmbedding  # lazy import

            self._model = TextEmbedding(model_name=self._model_name)
        return next(self._model.embed([text])).tolist()

    async def get_or_compute_async(self, text: str) -> list[float]:
        """Async-safe wrapper: runs the CPU-bound embed() in a thread pool.

        Concurrent coroutines requesting the same text share a single in-flight
        future, preventing duplicate thread dispatches and write races.
        """
        if text in self._cache:
            return self._cache[text]

        if text in self._in_flight:
            return await self._in_flight[text]

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[float]] = loop.create_future()
        self._in_flight[text] = fut
        try:
            result = await asyncio.to_thread(self.get_or_compute, text)
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(text, None)

    def clear(self) -> None:
        """Evict all cached embeddings (e.g. after a model change)."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._cache)
