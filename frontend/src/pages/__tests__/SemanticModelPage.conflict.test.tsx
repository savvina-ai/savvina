// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError, AxiosHeaders } from 'axios';

vi.mock('../../api/semantic', () => ({
  semanticApi: {
    get: vi.fn(),
    getDrift: vi.fn(),
    getLastDrift: vi.fn(),
    getTombstones: vi.fn(),
    restoreTombstone: vi.fn(),
    applyDrift: vi.fn(),
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
  notes: ['first'],
  time_expressions: {},
  db_timezone: null,
  schema_hash: null,
  source_dialect: 'postgres',
  generation_warnings: [],
  generated_at: '2026-01-01T00:00:00Z',
  is_user_reviewed: false,
  generation_model: 'test',
  version: 6,
};

function conflict(detail = 'The semantic model changed since this draft was loaded (version 6, now 7).') {
  return new AxiosError('Conflict', '409', undefined, undefined, {
    status: 409,
    statusText: 'Conflict',
    data: { detail },
    headers: {},
    config: { headers: new AxiosHeaders() },
  });
}

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

/** Render, make one edit (tick "reviewed"), and click Save. */
async function editAndSave() {
  const user = userEvent.setup();
  renderPage();
  const box = await screen.findByRole('checkbox', { name: /reviewed/i });
  await waitFor(() => expect(box).toBeEnabled());
  await user.click(box);
  await user.click(screen.getByRole('button', { name: 'Save Changes' }));
  return user;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(semanticApi.getLastDrift).mockResolvedValue({ report: null, monitor_interval_minutes: 0 } as never);
  vi.mocked(semanticApi.getTombstones).mockResolvedValue([] as never);
  vi.mocked(semanticApi.getDriftHistory).mockResolvedValue([] as never);
  vi.mocked(semanticApi.getSuggestions).mockResolvedValue([] as never);
  vi.mocked(connectionsApi.getSchema).mockResolvedValue({ tables: [] } as never);
});

describe('SemanticModelPage — a save refused because the model moved', () => {
  it('sends the version the draft was seeded from', async () => {
    vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
    vi.mocked(semanticApi.update).mockResolvedValue({ ...MODEL, is_user_reviewed: true, version: 7 } as never);
    await editAndSave();
    await waitFor(() => expect(semanticApi.update).toHaveBeenCalledTimes(1));
    expect(vi.mocked(semanticApi.update).mock.calls[0][2]).toBe(6);
    expect(await screen.findByText('Saved')).toBeInTheDocument();
  });

  it('merges the server change into the draft, keeps the edit and saves against the new version', async () => {
    const latest = { ...MODEL, notes: ['first', 'from the other tab'], version: 7 };
    vi.mocked(semanticApi.get).mockResolvedValueOnce(MODEL as never).mockResolvedValue(latest as never);
    vi.mocked(semanticApi.update)
      .mockRejectedValueOnce(conflict())
      .mockResolvedValue({ ...latest, is_user_reviewed: true, version: 8 } as never);
    const user = await editAndSave();

    const banner = (await screen.findByText(/The model changed elsewhere/)).closest('div')!;
    expect(banner).toHaveTextContent('The model changed elsewhere; 1 change from the server was merged into your draft.');
    expect(banner).not.toHaveTextContent('changed on both sides');
    // The edit survived the merge.
    expect(screen.getByRole('checkbox', { name: /reviewed/i })).toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(semanticApi.update).toHaveBeenCalledTimes(2));
    const [, sent, baseVersion] = vi.mocked(semanticApi.update).mock.calls[1];
    expect(baseVersion).toBe(7);
    expect(sent.is_user_reviewed).toBe(true);
    expect(sent.notes).toEqual(['first', 'from the other tab']);
    await waitFor(() => expect(screen.queryByText(/The model changed elsewhere/)).not.toBeInTheDocument());
  });

  it('reports no conflict when both sides made the same change', async () => {
    // The user ticks "reviewed"; the other tab did the same: nothing to list, nothing new
    // from the server. (A place changed differently on both sides is covered by the
    // semanticMerge tests; the page only lists the labels it is handed.)
    const latest = { ...MODEL, is_user_reviewed: true, version: 7 };
    vi.mocked(semanticApi.get).mockResolvedValueOnce(MODEL as never).mockResolvedValue(latest as never);
    vi.mocked(semanticApi.update).mockRejectedValueOnce(conflict());
    await editAndSave();

    const banner = (await screen.findByText(/The model changed elsewhere/)).closest('div')!;
    expect(banner).toHaveTextContent('your draft already carries its changes');
    expect(banner).not.toHaveTextContent('changed on both sides');
    expect(screen.getByRole('checkbox', { name: /reviewed/i })).toBeChecked();
  });

  it('Discard my changes re-seeds the draft from the server model', async () => {
    const latest = { ...MODEL, notes: ['first', 'from the other tab'], version: 7 };
    vi.mocked(semanticApi.get).mockResolvedValueOnce(MODEL as never).mockResolvedValue(latest as never);
    vi.mocked(semanticApi.update).mockRejectedValueOnce(conflict());
    const user = await editAndSave();
    await screen.findByText(/The model changed elsewhere/);

    await user.click(screen.getByRole('button', { name: 'Discard my changes' }));
    expect(screen.queryByText(/The model changed elsewhere/)).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /reviewed/i })).not.toBeChecked();
    // Re-seeded from the server's latest model, not the one the draft started from: the
    // next save is against version 7.
    vi.mocked(semanticApi.update).mockResolvedValue({ ...latest, is_user_reviewed: true, version: 8 } as never);
    await user.click(screen.getByRole('checkbox', { name: /reviewed/i }));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(semanticApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(semanticApi.update).mock.calls[1][2]).toBe(7);
  });

  it('merges against the draft as it is when the fetch lands, edits typed meanwhile included', async () => {
    const latest = { ...MODEL, notes: ['first', 'from the other tab'], version: 7 };
    let resolveLatest!: (m: unknown) => void;
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(MODEL as never)
      .mockImplementationOnce((() => new Promise((r) => (resolveLatest = r))) as never)
      .mockResolvedValue(latest as never);
    vi.mocked(semanticApi.update).mockRejectedValueOnce(conflict());
    const user = await editAndSave(); // reviewed: true, then the 409
    await waitFor(() => expect(semanticApi.get).toHaveBeenCalledTimes(2));
    // Typed while the merge fetch is in flight: untick again.
    await user.click(screen.getByRole('checkbox', { name: /reviewed/i }));
    await act(async () => {
      resolveLatest(latest);
    });
    await screen.findByText(/The model changed elsewhere/);
    expect(screen.getByRole('checkbox', { name: /reviewed/i })).not.toBeChecked();
  });

  it('shows a 409 that is not a stale draft as an error and merges nothing', async () => {
    vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
    vi.mocked(semanticApi.update).mockRejectedValueOnce(conflict('Row public.orders moved on'));
    await editAndSave();
    expect(await screen.findByText('Row public.orders moved on')).toBeInTheDocument();
    expect(screen.queryByText(/The model changed elsewhere/)).not.toBeInTheDocument();
    expect(semanticApi.get).toHaveBeenCalledTimes(1);
  });

  it('shows the generic error for any other failure', async () => {
    vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
    vi.mocked(semanticApi.update).mockRejectedValueOnce(new Error('boom'));
    await editAndSave();
    expect(await screen.findByText('Failed to save — please try again')).toBeInTheDocument();
    expect(screen.queryByText(/The model changed elsewhere/)).not.toBeInTheDocument();
  });
});
