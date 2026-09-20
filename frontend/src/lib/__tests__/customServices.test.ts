// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { CUSTOM_SERVICES } from '../customServices';

// This list is the only place the offered OpenAI-compatible services are defined.
// It used to be duplicated by OpenAICompatibleProvider.get_config_schema(), which
// nothing ever served and which had already drifted; the guards that lived in its
// tests moved here with it.
describe('CUSTOM_SERVICES', () => {
  it('offers exactly the supported services', () => {
    expect(CUSTOM_SERVICES.map((s) => s.label)).toEqual([
      'HuggingFace (Free)',
      'Together.ai',
      'OpenRouter',
      'Custom URL',
    ]);
  });

  it('does not offer the retired GitHub Models endpoints', () => {
    // GitHub Models was fully retired on 2026-07-30; both of its endpoints
    // (models.inference.ai.azure.com and models.github.ai) now return errors.
    const urls = CUSTOM_SERVICES.map((s) => s.base_url).join(' ');
    expect(urls).not.toMatch(/azure\.com|models\.github\.ai|github/i);
  });

  it('gives every named service a base URL and a default model', () => {
    for (const s of CUSTOM_SERVICES.filter((s) => s.label !== 'Custom URL')) {
      expect(s.base_url).toMatch(/^https:\/\//);
      expect(s.default_model).not.toBe('');
    }
  });
});
