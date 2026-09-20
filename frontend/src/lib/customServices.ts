// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * The OpenAI-compatible services offered by the custom-provider form.
 *
 * Only for truly custom/unknown compatible services — Gemini, Groq, Cerebras and
 * Mistral have dedicated provider types. This is the single definition: the
 * backend's OpenAICompatibleProvider.get_config_schema() used to carry a second,
 * unserved copy that had already drifted, and it was removed.
 */
export interface CustomService {
  label: string;
  base_url: string;
  default_model: string;
}

export const CUSTOM_SERVICES: CustomService[] = [
  {
    label: 'HuggingFace (Free)',
    base_url: 'https://router.huggingface.co/v1',
    default_model: 'meta-llama/Llama-3.2-3B-Instruct',
  },
  {
    label: 'Together.ai',
    base_url: 'https://api.together.xyz/v1',
    default_model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
  },
  { label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', default_model: 'openrouter/free' },
  { label: 'Custom URL', base_url: '', default_model: '' },
];
