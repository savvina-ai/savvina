// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeProviderStatus } from '../../test/factories';
import ProviderSelector from '../ProviderSelector';
import type { ProviderStatus } from '../../types';

const API = 'http://localhost:8000';

function renderSelector(items: ProviderStatus[]) {
  server.use(
    http.get(`${API}/api/v1/providers`, () =>
      HttpResponse.json({ items, total: items.length, limit: 50, offset: 0 }),
    ),
  );
  const onChange = vi.fn();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ProviderSelector value="" onChange={onChange} />
    </QueryClientProvider>,
  );
  return onChange;
}

describe('ProviderSelector', () => {
  it('lists only saved, active configs — a config-less provider row is never selectable', async () => {
    // API keys come only from saved configs, so a row with no id has nothing to
    // send a chat request with, whatever `is_configured` says.
    renderSelector([
      makeProviderStatus({ id: null, provider_type: 'groq', display_name: 'Groq', is_active: false }),
    ]);
    expect(await screen.findByText('No providers configured')).toBeInTheDocument();
  });

  it('auto-selects a saved active config by its id', async () => {
    const onChange = renderSelector([makeProviderStatus({ id: 'cfg-1', is_active: true })]);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('cfg-1'));
  });
});
