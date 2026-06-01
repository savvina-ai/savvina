# Adding an LLM Provider

This guide explains how to add a new LLM provider to Savvina AI so it appears in the provider dropdown and can generate SQL queries.

---

## How Providers Are Registered

Like data sources, providers use a decorator-based registry. On startup, `main.py` imports the `providers` package, which triggers all `@register_provider(...)` decorators. Each one stores the class in a global `_REGISTRY` dict.

```
backend/app/providers/
├── base.py                      ← BaseLLMProvider ABC + LLMResponse + parse_llm_response()
├── registry.py                  ← register_provider(), create_provider()
├── __init__.py                  ← imports all providers (triggers registration)
├── claude_provider.py           ← Anthropic Claude (reference)
├── openai_provider.py           ← OpenAI GPT
├── openai_compatible_provider.py ← Generic OpenAI-compatible base
├── groq_provider.py             ← Groq (extends OpenAICompatibleProvider)
├── gemini_provider.py           ← Google Gemini (extends OpenAICompatibleProvider)
├── cerebras_provider.py         ← Cerebras (extends OpenAICompatibleProvider)
├── mistral_provider.py          ← Mistral (extends OpenAICompatibleProvider)
└── ollama_provider.py           ← Ollama local (extends OpenAIProvider)
```

---

## Choosing an Approach

**Option A — Extend `OpenAICompatibleProvider`** (recommended for most new providers)

If the new provider exposes an OpenAI-compatible Chat Completions API (most modern providers do), extend `OpenAICompatibleProvider` and override only the base URL and model list. See `GroqProvider` for the simplest example.

**Option B — Implement `BaseLLMProvider` directly**

For providers with non-standard APIs (e.g., AWS Bedrock, Cohere), implement the abstract methods from scratch. Use `ClaudeProvider` as a reference.

---

## Option A: Extending `OpenAICompatibleProvider`

This is a ~50-line file. Use `GroqProvider` as a template:

```python
# backend/app/providers/myprovider_provider.py
"""My Provider — via OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

import logging

from .base import ModelInfo
from .openai_provider import OpenAIProvider, _parse_openai_models_response
from .registry import register_provider

logger = logging.getLogger(__name__)

_MY_EXCLUDE = frozenset(["embedding", "whisper"])  # non-chat model keywords to drop


@register_provider("myprovider")
class MyProvider(OpenAIProvider):
    """My Provider's fast inference via its OpenAI-compatible endpoint."""

    provider_name = "myprovider"
    display_name = "My Provider"
    default_model = "my-model-70b"  # surfaced in the UI and used when no model is configured

    _BASE_URL = "https://api.myprovider.com/v1"
    _DEFAULT_MODEL = "my-model-70b"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        default_model: str = "",
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url or self._BASE_URL,
            default_model=default_model or self._DEFAULT_MODEL,
            verify_ssl=verify_ssl,
        )

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        if not api_key:
            return []
        import httpx

        url = f"{(base_url or cls._BASE_URL).rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                resp.raise_for_status()
                data: list[dict] = resp.json().get("data", [])
            return _parse_openai_models_response(data, _MY_EXCLUDE)
        except Exception:
            logger.warning("fetch_available_models failed for myprovider")
            return []
```

`OpenAIProvider` provides `generate_response()` and `health_check()` via the `openai` Python client. `_parse_openai_models_response` handles standard OpenAI-format model lists: it filters out inactive models, models with `context_window < 4096`, and any model whose ID contains a keyword from the exclusion set.

If the provider's models endpoint uses a non-standard response format (like Mistral's `capabilities` object or `max_context_length`), write a custom parser method instead of using `_parse_openai_models_response`. See `MistralProvider._parse_mistral_models()` for an example.

---

## Option B: Implementing `BaseLLMProvider` Directly

### The interface

```python
# From backend/app/providers/base.py

@dataclass
class ModelInfo:
    id: str
    context_window: int | None = None
    max_completion_tokens: int | None = None


class BaseLLMProvider(ABC):
    provider_name: str = ""
    display_name: str = ""
    default_model: str = ""  # set this in every subclass — shown in the UI and used as the runtime fallback

    @abstractmethod
    async def generate_response(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def health_check(self) -> tuple[bool, str]:
        ...

    @classmethod
    @abstractmethod
    def get_available_models(cls) -> list[str]:
        """Return [] — dynamic fetching via fetch_available_models() is preferred."""
        ...

    @classmethod
    async def fetch_available_models(
        cls,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> list[ModelInfo]:
        """Fetch live models from the provider API. Default: returns []. Override in each provider."""
        return []
```

### `generate_response()`

Sends the prompt to the LLM API and returns a parsed `LLMResponse`. The `conversation_history` is a list of `{"role": "user"|"assistant", "content": "..."}` dicts representing prior turns in the session.

**Responsibilities:**
1. Build the message list (system + history + current user message)
2. Call the LLM API
3. Extract the raw text response
4. Call `parse_llm_response(raw, model, tokens_used)` to parse the SQL and explanation
5. Return the `LLMResponse` dataclass

```python
from .base import BaseLLMProvider, LLMResponse, parse_llm_response
from .registry import register_provider
import httpx


@register_provider("myprovider")
class MyProvider(BaseLLMProvider):
    provider_name = "myprovider"
    display_name = "My Provider"
    default_model = "my-model-70b"

    def __init__(self, api_key: str, verify_ssl: bool = True,
                 default_model: str = "") -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._verify_ssl = verify_ssl

    async def generate_response(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: list[dict],
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        model = model or self._default_model
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        async with httpx.AsyncClient(verify=self._verify_ssl) as client:
            resp = await client.post(
                "https://api.myprovider.com/v1/chat",
                json={"model": model, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=120.0,
            )
            resp.raise_for_status()

        data = resp.json()
        raw = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens")
        return parse_llm_response(raw, model, tokens)

    async def health_check(self) -> tuple[bool, str]:
        try:
            resp = await self.generate_response(
                system_prompt="You are a helpful assistant.",
                user_message="Reply with the single word: OK",
                conversation_history=[],
                max_tokens=1,
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    @classmethod
    def get_available_models(cls) -> list[str]:
        return []  # models are fetched dynamically via fetch_available_models()
```

### `health_check()`

Called by `POST /api/providers/{config_id}/test`. Returns `(True, "")` on success or `(False, "error detail")` on failure. For API-based providers, send a minimal one-token request. The result is never stored — it's purely a connectivity check.

### Response parsing

**Always use `parse_llm_response()` from `base.py`** rather than writing your own parser. It handles the expected `QUERY: ... EXPLANATION:` format with multiple fallback strategies. Your job is only to get the raw text out of the API response.

---

## Step 3: Register the Provider

Add the import to `backend/app/providers/__init__.py`:

```python
# backend/app/providers/__init__.py
from . import claude_provider       # noqa: F401
from . import openai_provider       # noqa: F401
from . import openai_compatible_provider  # noqa: F401
from . import groq_provider         # noqa: F401
from . import gemini_provider       # noqa: F401
from . import cerebras_provider     # noqa: F401
from . import mistral_provider      # noqa: F401
from . import ollama_provider       # noqa: F401
from . import myprovider_provider   # noqa: F401  ← add this
```

The provider appears immediately in `GET /api/providers` and the chat toolbar dropdown.

---

## Step 4: Add the Environment Variable (Optional)

If you want users to be able to configure the API key via `.env` without going through the UI, add it to `backend/app/config.py`:

```python
# In class Settings(BaseSettings):
myprovider_api_key: str | None = Field(default=None, alias="MYPROVIDER_API_KEY")
```

Then register it in `env_api_key()`:

```python
def env_api_key(self, provider_name: str) -> str | None:
    mapping = {
        "claude": self.anthropic_api_key,
        "openai": self.openai_api_key,
        "groq": self.groq_api_key,
        "gemini": self.gemini_api_key,
        "cerebras": self.cerebras_api_key,
        "mistral": self.mistral_api_key,
        "myprovider": self.myprovider_api_key,  # ← add this
    }
    return mapping.get(provider_name)
```

Add to `.env.example`:

```
MYPROVIDER_API_KEY=your-key-here
```

---

## Step 5: Write Tests

Create `backend/tests/test_providers/test_myprovider.py`:

```python
from unittest.mock import AsyncMock, patch
import pytest

from app.providers.myprovider_provider import MyProvider
from app.providers.base import LLMResponse


class TestMyProviderHealthCheck:
    async def test_healthy_returns_true(self):
        provider = MyProvider(api_key="test-key")
        with patch.object(
            provider, "generate_response",
            new_callable=AsyncMock,
            return_value=LLMResponse(
                query="", explanation="", raw_response="OK",
                model="my-model-70b", tokens_used=1,
            ),
        ):
            ok, err = await provider.health_check()
        assert ok is True
        assert err == ""

    async def test_unhealthy_returns_false(self):
        provider = MyProvider(api_key="bad-key")
        with patch.object(
            provider, "generate_response",
            new_callable=AsyncMock,
            side_effect=Exception("401 Unauthorized"),
        ):
            ok, err = await provider.health_check()
        assert ok is False
        assert "401" in err


class TestMyProviderGenerateResponse:
    async def test_returns_llm_response(self):
        provider = MyProvider(api_key="test-key")
        mock_response = {
            "choices": [{"message": {"content":
                "QUERY:\n```sql\nSELECT 1\n```\nEXPLANATION:\nTest"
            }}],
            "usage": {"total_tokens": 42},
        }
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value.json.return_value = mock_response
            mock_post.return_value.raise_for_status = lambda: None
            resp = await provider.generate_response(
                system_prompt="You are an expert.",
                user_message="SELECT 1",
                conversation_history=[],
            )
        assert resp.query == "SELECT 1"
        assert resp.tokens_used == 42


class TestMyProviderModels:
    def test_get_available_models_returns_empty(self):
        # Hardcoded list is intentionally empty; models are fetched dynamically.
        assert MyProvider.get_available_models() == []

    async def test_fetch_available_models_with_mock(self):
        mock_data = {
            "data": [
                {"id": "my-model-70b", "context_window": 131072, "active": True},
                {"id": "my-model-7b", "context_window": 8192, "active": True},
            ]
        }
        import httpx
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status = lambda: None

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            models = await MyProvider.fetch_available_models(api_key="test-key")

        assert len(models) == 2
        assert models[0].id == "my-model-70b"

    async def test_fetch_available_models_no_key_returns_empty(self):
        models = await MyProvider.fetch_available_models(api_key=None)
        assert models == []
```

Run tests:

```bash
.venv/bin/pytest backend/tests/ -v
```

---

## Checklist

- [ ] Provider file created in `backend/app/providers/`
- [ ] `@register_provider("myprovider")` decorator applied
- [ ] `default_model = "..."` class attribute set (shown in the UI and used as the runtime fallback when no model is configured)
- [ ] `generate_response()` implemented and calls `parse_llm_response()`
- [ ] `health_check()` returns `(bool, str)` tuple
- [ ] `get_available_models()` returns `[]` (no hardcoded list)
- [ ] `fetch_available_models()` overridden — calls the provider's `/models` endpoint and returns `list[ModelInfo]`
- [ ] `__init__.py` updated with new import
- [ ] (Optional) `config.py` updated with env var support
- [ ] Tests pass: `.venv/bin/pytest backend/tests/ -v`
- [ ] `GET /api/providers` shows `provider_name` in available providers list and `current_model` matches `default_model`
- [ ] `POST /api/providers/test` returns `{"success": true}` with a real API key
- [ ] `POST /api/providers/models` returns a non-empty list with a real API key
- [ ] Provider appears in the chat toolbar provider dropdown
- [ ] A full chat request using this provider returns a valid SQL query
