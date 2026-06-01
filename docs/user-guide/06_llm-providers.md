# LLM Providers

Savvina AI supports multiple LLM providers through a unified adapter interface. You can configure multiple providers simultaneously and switch between them per chat session.

---

## Provider Types

| Type | Class | Notes |
|---|---|---|
| `claude` | `ClaudeProvider` | Anthropic Claude models |
| `openai` | `OpenAIProvider` | OpenAI GPT models |
| `openai_compatible` | `OpenAICompatibleProvider` | Any OpenAI-compatible API (GitHub Models, HuggingFace, Together.ai, OpenRouter, custom) |
| `groq` | `GroqProvider` | Groq |
| `gemini` | `GeminiProvider` | Google Gemini |
| `cerebras` | `CerebrasProvider` | Cerebras |
| `mistral` | `MistralProvider` | Mistral |
| `ollama` | `OllamaProvider` | Local Ollama server — no API key required |

---

## Adding a Provider

### Via the UI

1. Go to **Settings → Providers**
2. Click the **+ Add \<Provider\> config** button for the provider type you want
3. Fill in the form:
   - **API Key** — if the key is already set via an environment variable, the label shows **✓ env key configured — leave blank to use it** and the field is optional
   - **Display Name** — label shown in the provider dropdown (e.g., "Groq — Free Tier")
   - **Base URL** — required only for `openai_compatible`; pre-filled for named providers
   - **Temperature** — default `0.0` (deterministic, recommended for SQL generation)
   - **Max Tokens** — default `4096`
4. Once you've entered an API key (or an env key is present), click **Fetch Models** to pull the live model list from the provider's API. A dropdown appears with all available models sorted alphabetically.
5. Select a model from the dropdown (or type one manually if Fetch Models was skipped). The model is pre-filled with the provider's default if one is available.
6. Click **Test** to verify connectivity
7. Click **Add** to save the config

For **Custom Providers** (OpenRouter, HuggingFace, Together.ai, GitHub Models, custom URL), click **+ Add Custom Provider**. Enter the base URL and API key first, then click **Fetch Models** to populate the model dropdown. Click **Cancel** to dismiss the form without saving.

### Via Environment Variables

Set the provider's API key in `.env` (see [Configuration](../getting-started/02_configuration.md) for variable names). When a key is present but no saved UI config exists for that provider:

- The settings page shows a **green "Configured via environment variable · default model: X"** banner for that provider type.
- The backend uses the env key and the provider's hardcoded default model for all queries — no UI action is required for the provider to work.
- You can still click **+ Add config** to create a saved config (e.g., to select a different model or set a display name). The API key field is optional when an env key is detected; leave it blank and the env key is used.

For key priority rules (env var vs. saved config) and model resolution details, see [Configuration — LLM Provider Keys](../getting-started/02_configuration.md#llm-provider-keys).

---

## Fetching Available Models

All providers support live model fetching. Click **Fetch Models** in the add/edit form to pull the current model list directly from the provider's API. The list is sorted alphabetically and filtered to remove non-chat models (embeddings, speech, image, moderation) and models with a context window too small for NL-to-SQL.

For saved provider configs, a **refresh icon** next to the model dropdown fetches a fresh list and updates the cached copy in the database.

If fetching fails (invalid key, network issue), the dropdown falls back to any previously cached list or a free-text input.

---

## Provider-Specific Notes

### Anthropic Claude

- **Default model:** `claude-sonnet-4-6`
- Excellent reasoning; best performance for complex multi-table queries
- Models fetched from `https://api.anthropic.com/v1/models`; only `claude-*` IDs are included

### OpenAI GPT

- **Default model:** `gpt-4o`
- Models fetched from `https://api.openai.com/v1/models`; embedding, image, speech, and moderation models are excluded

### Groq

- **Default model:** `llama-3.3-70b-versatile`
- Models fetched from `https://api.groq.com/openai/v1/models`; audio/guard models are excluded

### Google Gemini

- **Default model:** `gemini-2.5-flash`
- Uses OpenAI-compatible endpoint: `https://generativelanguage.googleapis.com/v1beta/openai/`
- Models fetched from the Gemini REST API; only models that support `generateContent` are included

### Cerebras

- **Default model:** `qwen-3-235b-a22b-instruct-2507`
- Base URL: `https://api.cerebras.ai/v1`
- Models fetched from the Cerebras models endpoint

### Mistral

- **Default model:** `mistral-large-latest`
- Base URL: `https://api.mistral.ai/v1`
- Models fetched from `https://api.mistral.ai/v1/models`; only models with `capabilities.completion_chat: true` are included; embed and moderation models are excluded

### GitHub Models

- **Default model:** `DeepSeek-R1`
- Base URL: `https://models.inference.ai.azure.com`
- Uses your GitHub personal access token as the API key

### HuggingFace

- **Default model:** `Qwen/Qwen2.5-Coder-32B-Instruct`
- Base URL: `https://router.huggingface.co/v1`
- Uses HuggingFace API token

### Together.ai

- Base URL: `https://api.together.xyz/v1`

### OpenRouter

- Base URL: `https://openrouter.ai/api/v1`
- Aggregator with access to hundreds of models including free ones; Fetch Models is especially useful here due to the large model catalogue
- OpenRouter returns `context_length` instead of the standard `context_window`; the parser handles both field names

### Custom OpenAI-Compatible

Use the `openai_compatible` provider type with any custom base URL. Useful for:
- Self-hosted vLLM
- LM Studio (local)
- Any other service using the OpenAI Chat Completions API format

Fetch Models calls `GET {base_url}/models` with the supplied API key.

### Ollama (Local)

- **No API key required**
- **Default base URL:** `http://ollama:11434` (within Docker network)
- Models must be pulled separately: `docker exec <container> ollama pull llama3`
- Fetch Models calls `GET /api/tags` on the Ollama server to list pulled models
- Health check: `GET /api/tags`
- Use `--profile local-llm` with Docker Compose to start the Ollama service

---

## Multiple Configs per Provider Type

You can save multiple configurations of the same provider type. For example:
- **Groq — Fast queries** using `llama-3.3-70b-versatile`
- **Groq — Code queries** using a different model
- **Gemini** using `gemini-2.5-flash`

Each saved config has a unique UUID and display name. The provider selector in the chat UI shows all saved configs, not just provider types.

---

## Switching Providers Per Session

The chat toolbar has a provider dropdown listing all saved provider configs. Selecting a different provider affects only the current session — it does not change the connection's default.

---

## Health Checks

Click **Test** next to any saved provider config to run a live health check. The backend instantiates the provider and sends a minimal one-token completion request (`max_tokens=1`). Results:

| Result | Meaning |
|---|---|
| ✅ Healthy | API key is valid and the service is reachable |
| ❌ Unhealthy | Either the key is invalid, the model doesn't exist, or the service is down |

The health check never stores any data or affects your usage quota meaningfully (one token).

For Ollama, the health check pings `GET /api/tags` instead of making a completion request.

---

## Provider SSL/TLS

In corporate environments with TLS-intercepting proxies, set `VERIFY_SSL=false` in `backend/.env`. This applies to all provider HTTP clients. The setting is also accepted as `OPENAI_VERIFY_SSL` for backwards compatibility.

---

## How the Provider Is Selected for a Chat Request

When you send a message, the frontend sends the provider config UUID (not the type name). The backend:

1. Looks up the `ProviderConfig` by UUID in the database
2. Decrypts the stored API key (or falls back to the env var if no key is stored)
3. Constructs the provider instance with the stored model, base URL, and temperature
4. Calls `generate_response()` with the stored model as the default; if the model field is empty, the provider's hardcoded `default_model` is used automatically

If the UUID lookup fails (e.g., provider name sent instead of UUID), the backend falls back to looking up the most recently updated config matching that provider type.

The `current_model` field in `GET /api/v1/providers` always reflects the model that will actually be used — either the saved model or the provider's hardcoded default when none is configured.
