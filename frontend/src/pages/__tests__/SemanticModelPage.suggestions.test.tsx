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
    getDriftHistory: vi.fn(),
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
  connectionsApi: { getSchema: vi.fn().mockResolvedValue({ tables: [] }) },
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

const SUGGESTION = {
  id: 'sug-1',
  connection_id: 'conn-1',
  table_key: 'store.orders',
  field: 'status',
  correction_type: 'add_value_mapping',
  value: { raw_value: 'CMPLT', display_value: 'Completed' },
  is_applied: false,
  source_message_id: 'msg-1',
  created_at: '2026-01-02T10:00:00Z',
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

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
  vi.mocked(semanticApi.getDriftHistory).mockResolvedValue([] as never);
  vi.mocked(semanticApi.getSuggestions).mockResolvedValue([SUGGESTION] as never);
  vi.mocked(semanticApi.applySuggestion).mockResolvedValue(MODEL as never);
});

describe('SemanticModelPage — suggestions', () => {
  it('nudges to the suggestions tab while pending corrections exist', async () => {
    renderPage();
    expect(await screen.findByText(/1 pending correction from chat feedback/)).toBeInTheDocument();
  });

  it('lists a pending suggestion and applies it', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('tab', { name: /Suggestions/ }));

    expect(
      screen.getByText('Add value mapping to store.orders.status'),
    ).toBeInTheDocument();

    await user.click(screen.getByText('Apply'));

    await waitFor(() =>
      expect(semanticApi.applySuggestion).toHaveBeenCalledWith('conn-1', 'sug-1'),
    );
  });

  it('removes a suggestion from the list when dismissed', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('tab', { name: /Suggestions/ }));
    await user.click(screen.getByText('Dismiss'));

    expect(screen.getByText(/No pending suggestions/)).toBeInTheDocument();
    expect(semanticApi.applySuggestion).not.toHaveBeenCalled();
  });

  it('shows why an apply failed and drops a suggestion applied elsewhere', async () => {
    /*
     * Another tab applied it first: the server answers 409. The list and the model are
     * refreshed, so the stale card goes and the editor shows the applied change.
     */
    const user = userEvent.setup();
    vi.mocked(semanticApi.getSuggestions)
      .mockResolvedValueOnce([SUGGESTION] as never)
      .mockResolvedValue([{ ...SUGGESTION, is_applied: true }] as never);
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(MODEL as never)
      .mockResolvedValue({ ...MODEL, notes: ['Applied elsewhere'] } as never);
    vi.mocked(semanticApi.applySuggestion).mockRejectedValue({
      isAxiosError: true,
      response: { status: 409, data: { detail: 'Suggestion already applied' } },
    } as never);

    renderPage();
    await user.click(await screen.findByRole('tab', { name: /Suggestions/ }));
    await user.click(screen.getByText('Apply'));

    expect(await screen.findByText(/Suggestion already applied/)).toBeInTheDocument();
    expect(await screen.findByText(/No pending suggestions/)).toBeInTheDocument();
    expect(semanticApi.getSuggestions).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole('tab', { name: /Notes/ }));
    expect(await screen.findByDisplayValue('Applied elsewhere')).toBeInTheDocument();
  });

  it('pauses editing while an apply is in flight', async () => {
    /*
     * Both outcomes replace the draft (success with the applied model, an error with a
     * fresh one), so anything typed during the POST would be dropped without a trace.
     */
    const user = userEvent.setup();
    let release: (v: unknown) => void = () => {};
    vi.mocked(semanticApi.applySuggestion).mockReturnValue(
      new Promise((r) => {
        release = r;
      }) as never,
    );

    renderPage();
    await user.click(await screen.findByRole('tab', { name: /Suggestions/ }));
    await user.click(screen.getByText('Apply'));

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeDisabled(),
    );
    expect(screen.getByRole('button', { name: /Dismiss/ })).toBeDisabled();

    release({ ...MODEL, notes: ['Applied'] });
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeEnabled(),
    );
    await user.click(screen.getByRole('tab', { name: /Notes/ }));
    expect(screen.getByDisplayValue('Applied')).toBeEnabled();
  });
});
