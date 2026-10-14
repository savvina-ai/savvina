// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * Backlog 12.5a — "Finish with what's generated".
 *
 * An interrupted run leaves generation_status = 'tables_partial'. The only action used to
 * be a button that called /generate/init — and init unconditionally blanks `tables`, so
 * every table already generated (and paid for) was discarded, usually into the same rate
 * limit that killed the first attempt. Finish calls /generate/globals alone, which accepts
 * a partial model and finalises over whatever tables are present.
 */

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
  connectionsApi: { getSchema: vi.fn().mockResolvedValue({ tables: [] }) },
}));

vi.mock('../../hooks/useConnections', () => ({
  useConnections: () => ({ data: [{ id: 'conn-1', name: 'Warehouse' }] }),
}));

// A configured provider, or the page never selects one and every generation control
// stays disabled for a reason unrelated to what these tests cover.
vi.mock('../../hooks/useProviders', () => ({
  useProviders: () => ({
    data: [
      {
        id: 'provider-1',
        provider_type: 'claude',
        display_name: 'Claude',
        is_configured: true,
        current_model: 'claude-sonnet-4-5',
      },
    ],
  }),
}));

vi.mock('../../store/authStore', () => ({
  useAuthStore: (selector: any) => selector({ user: { id: 'u1' } }),
}));

import { semanticApi } from '../../api/semantic';
import SemanticModelPage from '../SemanticModelPage';

const BASE_MODEL = {
  tables: {
    'public.orders': {
      display_name: 'Orders',
      description: 'Customer orders',
      default_filters: [],
      columns: {},
    },
  },
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

const PARTIAL_MODEL = {
  ...BASE_MODEL,
  generation_status: 'tables_partial',
  generation_progress: { tables_done: 3, tables_total: 10, batch_size: 4 },
};

const COMPLETE_MODEL = {
  ...BASE_MODEL,
  generation_status: 'complete',
  generation_progress: null,
};

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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

/** A promise the test releases by hand, to hold a mocked call in flight. */
function deferred<T>() {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(semanticApi.get).mockResolvedValue(PARTIAL_MODEL as never);
  vi.mocked(semanticApi.getSuggestions).mockResolvedValue([] as never);
  vi.mocked(semanticApi.generateGlobals).mockResolvedValue(COMPLETE_MODEL as never);
});

describe('SemanticModelPage — finishing an interrupted generation', () => {
  it('offers both Finish and Restart on the interrupted banner', async () => {
    renderPage();
    expect(await screen.findByText(/Generation was interrupted at table/)).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: /Finish with what's generated/i }),
    ).toBeEnabled();
    expect(screen.getByRole('button', { name: /^Restart$/ })).toBeInTheDocument();
  });

  it('calls only globals — never init or batch — and retires the banner', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));

    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(semanticApi.generateGlobals).toHaveBeenCalledWith('conn-1', 'provider-1');
    // The whole point of 12.5a: the destructive endpoints are never touched.
    expect(semanticApi.generateInit).not.toHaveBeenCalled();
    expect(semanticApi.generateBatch).not.toHaveBeenCalled();
    // The onSuccess cache write is what removes the banner.
    await waitFor(() =>
      expect(screen.queryByText(/Generation was interrupted at table/)).not.toBeInTheDocument(),
    );
  });

  it('labels the globals phase while the finish call is in flight', async () => {
    const user = userEvent.setup();
    const gate = deferred<typeof COMPLETE_MODEL>();
    vi.mocked(semanticApi.generateGlobals).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));

    expect(await screen.findByText('Building metrics and joins…')).toBeInTheDocument();
    expect(screen.queryByText('Analysing tables…')).not.toBeInTheDocument();
    // The banner must go while the call is in flight, or the user can double-fire it.
    expect(screen.queryByText(/Generation was interrupted at table/)).not.toBeInTheDocument();

    gate.resolve(COMPLETE_MODEL);
    await waitFor(() =>
      expect(screen.queryByText('Building metrics and joins…')).not.toBeInTheDocument(),
    );
  });

  it('disables Regenerate while a Finish run is in flight', async () => {
    /*
     * Regenerate calls /generate/init, which blanks `tables`. Leaving it live during a
     * Finish run reopens exactly the data loss 12.5a exists to prevent — one click
     * destroys the tables the Finish is racing to finalise.
     */
    const user = userEvent.setup();
    const gate = deferred<typeof COMPLETE_MODEL>();
    vi.mocked(semanticApi.generateGlobals).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));
    await screen.findByText('Building metrics and joins…');

    const regenerate = screen.getByRole('button', { name: /^Regenerate$/ });
    expect(regenerate).toBeDisabled();
    // Belt and braces: a disabled button ignores the click, and init stays untouched.
    await user.click(regenerate);
    expect(semanticApi.generateInit).not.toHaveBeenCalled();

    gate.resolve(COMPLETE_MODEL);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled(),
    );
  });

  it('disables Finish when no tables were generated at all', async () => {
    vi.mocked(semanticApi.get).mockResolvedValue({
      ...PARTIAL_MODEL,
      tables: {},
      generation_progress: { tables_done: 0, tables_total: 10, batch_size: 4 },
    } as never);

    renderPage();
    await screen.findByText(/Generation was interrupted at table/);
    // Globals over an empty table set is an LLM call that cannot produce anything.
    expect(screen.getByRole('button', { name: /Finish with what's generated/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Restart$/ })).toBeEnabled();
  });

  it('surfaces a failed finish in the error banner', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateGlobals).mockRejectedValue({
      response: { status: 502, data: { detail: 'Provider unavailable' } },
    } as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));

    expect(await screen.findByText(/Generation failed: Provider unavailable/)).toBeInTheDocument();
    // A permanent failure is thrown straight through — no retry loop burning calls.
    expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1);
  });

  // The banner reads generate.error || finishGeneration.error, and TanStack holds an
  // error until that same mutation re-runs or is reset. A failed full run followed by a
  // successful Finish must not leave the red banner up over a model that is now complete.
  it("clears a failed run's error banner once Finish succeeds", async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1',
      tables_total: 10,
      batch_count: 3,
      batch_size: 4,
    } as never);
    vi.mocked(semanticApi.generateBatch).mockRejectedValue({
      response: { status: 500, data: { detail: 'provider exploded' } },
    } as never);

    renderPage();

    // The page already holds a model, so the toolbar reads "Regenerate" and routes
    // through the confirm dialog, whose confirm button carries the same label.
    await user.click(await screen.findByRole('button', { name: /^Regenerate$/ }));
    await user.click(screen.getAllByRole('button', { name: /^Regenerate$/ })[0]);

    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();

    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));

    await waitFor(() =>
      expect(screen.queryByText(/Generation failed:/)).not.toBeInTheDocument(),
    );
  });
});
