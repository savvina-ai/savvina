// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../../api/semantic', () => ({
  semanticApi: {
    get: vi.fn(),
    getDrift: vi.fn(),
    getSuggestions: vi.fn(),
    applySuggestion: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    generateInit: vi.fn(),
    generateBatch: vi.fn(),
    generateGlobals: vi.fn(),
  },
}));

vi.mock('../../api/connections', () => ({
  connectionsApi: { getSchema: vi.fn() },
}));

vi.mock('../../hooks/useConnections', () => ({
  useConnections: () => ({ data: [{ id: 'conn-1', name: 'Warehouse' }] }),
}));

vi.mock('../../hooks/useProviders', () => ({
  useProviders: () => ({ data: [] }),
}));

vi.mock('../../store/authStore', () => ({
  useAuthStore: (selector: (s: unknown) => unknown) => selector({ user: { id: 'u1' } }),
}));

import { connectionsApi } from '../../api/connections';
import { semanticApi } from '../../api/semantic';
import SemanticModelPage from '../SemanticModelPage';

const MODEL = {
  tables: {},
  business_metrics: [],
  common_joins: [],
  relationships: [],
  derived_columns: [],
  segments: [],
  notes: [],
  time_expressions: {},
  db_timezone: null,
  schema_hash: null,
  source_dialect: 'postgres',
  generation_warnings: [],
  generated_at: '2026-01-01T00:00:00Z',
  is_user_reviewed: false,
  generation_model: 'test',
};

const REPORT = {
  connection_id: 'conn-1',
  warnings: [],
  warning_count: 0,
  checked_at: '2026-10-15T12:00:00Z',
  schema_source: 'live',
  schema_cached_at: null,
  refresh_error: null,
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/semantic/conn-1']}>
        <Routes>
          <Route path="/semantic/:connectionId" element={<SemanticModelPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function clickCheckDrift() {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole('button', { name: 'Check Drift' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
  vi.mocked(semanticApi.getSuggestions).mockResolvedValue([] as never);
  vi.mocked(connectionsApi.getSchema).mockResolvedValue({ tables: [] } as never);
});

describe('SemanticModelPage — Check Drift', () => {
  it('confirms a clean live check and reloads the refreshed schema', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(REPORT as never);
    await clickCheckDrift();

    expect(await screen.findByText(/No drift — the model matches the live schema/)).toBeInTheDocument();
    // Once on mount, once more after the live check refreshed the cache.
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2));
  });

  it('lists the warnings of a live check', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue({
      ...REPORT,
      warnings: ['Table "ecommerce.coupons" referenced in semantic model no longer exists in the database'],
      warning_count: 1,
    } as never);
    await clickCheckDrift();

    expect(await screen.findByText(/Schema drift detected \(1 warning\)/)).toBeInTheDocument();
    expect(screen.getByText(/ecommerce\.coupons/)).toBeInTheDocument();
  });

  it('says when the database was unreachable and the cache was used', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue({
      ...REPORT,
      schema_source: 'cache',
      schema_cached_at: '2026-09-24T07:16:41Z',
      refresh_error: 'connection refused',
    } as never);
    await clickCheckDrift();

    expect(
      await screen.findByText(/Could not reach the database \(connection refused\)/),
    ).toBeInTheDocument();
    expect(screen.getByText(/No drift — the model matches the cached schema/)).toBeInTheDocument();
    // The cache was not refreshed, so the schema is not reloaded.
    expect(connectionsApi.getSchema).toHaveBeenCalledTimes(1);
  });

  it('shows why a check failed instead of doing nothing', async () => {
    vi.mocked(semanticApi.getDrift).mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 400,
        data: { detail: 'Could not reach the data source to check drift: connection refused' },
      },
    });
    await clickCheckDrift();

    expect(
      await screen.findByText(
        /Drift check failed: Could not reach the data source to check drift: connection refused/,
      ),
    ).toBeInTheDocument();
  });
});
