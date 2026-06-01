# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

# Import all providers to trigger @register_provider decorators
from . import (  # noqa: F401
    cerebras_provider,
    claude_provider,
    gemini_provider,
    groq_provider,
    mistral_provider,
    ollama_provider,
    openai_compatible_provider,
    openai_provider,
)
