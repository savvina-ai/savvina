// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * Backlog 12.5a — "Finish with what's generated", and 12.6 — Resume / Restart.
 *
 * An interrupted run leaves generation_status = 'tables_partial'. Finish calls
 * /generate/globals alone, which accepts a partial model and finalises over whatever
 * tables are present. Resume calls /generate/init?resume=true and dispatches only the
 * `pending_table_keys` it returns. Restart deletes the model first and then runs a fresh
 * init — /generate/init on its own no longer discards anything.
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
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/semantic/conn-1']}>
        <Routes>
          <Route path="/semantic/:connectionId" element={<SemanticModelPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, qc };
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
  vi.mocked(semanticApi.generateInit).mockResolvedValue({
    connection_id: 'conn-1',
    tables_total: 10,
    batch_count: 3,
    batch_size: 4,
    pending_table_keys: [],
    resumed: false,
  } as never);
  vi.mocked(semanticApi.generateBatch).mockResolvedValue({
    connection_id: 'conn-1',
    table_keys: [],
    tables_done: 0,
    tables_total: 10,
    batch_size: 4,
  } as never);
  vi.mocked(semanticApi.delete).mockResolvedValue(undefined as never);
});

describe('SemanticModelPage — finishing an interrupted generation', () => {
  it('offers Resume, Finish and Restart on the interrupted banner', async () => {
    renderPage();
    expect(await screen.findByText(/Generation was interrupted at table/)).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: /Finish with what's generated/i }),
    ).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Restart from scratch' })).toBeInTheDocument();
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

    // The toolbar button reads 'Generating…' for the duration of ANY generation run.
    const regenerate = screen.getByRole('button', { name: 'Generating…' });
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
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Restart from scratch' })).toBeEnabled();
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
      pending_table_keys: ['public.orders'],
      resumed: false,
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

describe('SemanticModelPage — resuming and restarting an interrupted generation', () => {
  const PENDING = ['public.a', 'public.b', 'public.c', 'public.d', 'public.e'];

  it('resumes by dispatching only the pending keys, with resume=true', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1',
      tables_total: 10,
      batch_count: 2,
      batch_size: 4,
      pending_table_keys: PENDING,
      resumed: true,
    } as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(semanticApi.generateInit).toHaveBeenCalledWith('conn-1', 'provider-1', true);
    expect(semanticApi.delete).not.toHaveBeenCalled();
    // Five pending keys at batch_size 4 → exactly two batches, sliced in order.
    const dispatched = vi.mocked(semanticApi.generateBatch).mock.calls.map((c) => c[1]);
    expect(dispatched).toEqual([PENDING.slice(0, 4), PENDING.slice(4)]);
    await waitFor(() =>
      expect(screen.queryByText(/Generation was interrupted at table/)).not.toBeInTheDocument(),
    );
  });

  it('restarts by deleting the model before a fresh init, never a resume', async () => {
    const user = userEvent.setup();
    const order: string[] = [];
    vi.mocked(semanticApi.delete).mockImplementation(async () => {
      order.push('delete');
    });
    vi.mocked(semanticApi.generateInit).mockImplementation(async () => {
      order.push('init');
      return {
        connection_id: 'conn-1',
        tables_total: 2,
        batch_count: 1,
        batch_size: 4,
        pending_table_keys: ['public.a', 'public.b'],
        resumed: false,
      } as never;
    });

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Restart from scratch' }));
    await user.click(await screen.findByRole('button', { name: 'Delete and regenerate' }));

    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(order).toEqual(['delete', 'init']);
    expect(semanticApi.generateInit).toHaveBeenCalledWith('conn-1', 'provider-1', false);
    expect(semanticApi.generateBatch).toHaveBeenCalledWith(
      'conn-1',
      ['public.a', 'public.b'],
      'provider-1',
    );
  });

  it('starts the bar from the tables already done on a resume', async () => {
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1',
      tables_total: 10,
      batch_count: 1,
      batch_size: 4,
      pending_table_keys: ['public.x'],
      resumed: true,
    } as never);
    vi.mocked(semanticApi.generateBatch).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    // 10 total, 1 pending → 9 already done before the first batch lands.
    expect(await screen.findByText('9 / 10')).toBeInTheDocument();
    gate.resolve({ connection_id: 'conn-1', table_keys: ['public.x'], tables_done: 10,
                   tables_total: 10, batch_size: 4 });
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalled());
  });

  it('disables Regenerate and hides the banner while a Resume run is in flight', async () => {
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    await waitFor(() =>
      expect(screen.queryByText(/Generation was interrupted at table/)).not.toBeInTheDocument(),
    );
    // The toolbar button reads 'Generating…' for the duration of ANY generation run.
    expect(screen.getByRole('button', { name: 'Generating…' })).toBeDisabled();
    gate.resolve({
      connection_id: 'conn-1', tables_total: 1, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalled());
  });
});

describe('SemanticModelPage — saving around a generation run', () => {
  it('disables Save Changes while a generation run is in flight', async () => {
    /*
     * The draft is a snapshot from before the run. Saving it mid-run would overwrite the
     * values being regenerated and mark them user-edited, so no later regenerate would
     * touch them again.
     */
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);

    renderPage();
    const save = await screen.findByRole('button', { name: 'Save Changes' });
    expect(save).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Resume' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Save Changes' })).toHaveAttribute(
      'title',
      expect.stringMatching(/generation/i),
    );
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(semanticApi.update).not.toHaveBeenCalled();

    gate.resolve({
      connection_id: 'conn-1', tables_total: 1, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
  });

  it('refreshes the draft from what the server stored after a save', async () => {
    // The store normalises and fits values; the editor must show the stored version,
    // not the draft that was sent.
    const user = userEvent.setup();
    vi.mocked(semanticApi.get).mockResolvedValue(COMPLETE_MODEL as never);
    vi.mocked(semanticApi.update).mockResolvedValue({
      ...COMPLETE_MODEL,
      tables: {
        'public.orders': {
          ...COMPLETE_MODEL.tables['public.orders'],
          display_name: 'Orders (stored)',
        },
      },
    } as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(semanticApi.update).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Orders (stored)')).toBeInTheDocument();
  });
});

/** An axios-shaped rejection: what `withRetry` and `apiErrorMessage` both read. */
function httpError(status: number, detail: string) {
  return { isAxiosError: true, response: { status, data: { detail } } };
}

describe('SemanticModelPage — confirming a restart', () => {
  it('asks before deleting, and cancel calls nothing', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Restart from scratch' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/deletes the semantic model, including your edits/);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(semanticApi.delete).not.toHaveBeenCalled();
    expect(semanticApi.generateInit).not.toHaveBeenCalled();
  });

  it('no longer warns that Regenerate overwrites manual edits', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: /^Regenerate$/ }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).not.toHaveTextContent(/overwritten/i);
    expect(dialog).toHaveTextContent(/edited by hand are kept/);
    // The phased path batches tables and does no column fingerprinting.
    expect(dialog).toHaveTextContent(/batches of up to 4, one LLM call per batch/);
    expect(dialog).not.toHaveTextContent(/fingerprint|invoked once/i);
  });

  it('never inits when the DELETE fails', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.delete).mockRejectedValue(httpError(500, 'delete failed') as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Restart from scratch' }));
    await user.click(await screen.findByRole('button', { name: 'Delete and regenerate' }));

    expect(await screen.findByText(/Generation failed: delete failed/)).toBeInTheDocument();
    expect(semanticApi.generateInit).not.toHaveBeenCalled();
  });

  it('shows a true empty state when init fails after the DELETE', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(PARTIAL_MODEL as never)
      .mockRejectedValue(httpError(404, 'No semantic model') as never);
    vi.mocked(semanticApi.generateInit).mockRejectedValue(
      httpError(500, 'init exploded') as never,
    );

    renderPage();
    expect(await screen.findByText('Orders')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Restart from scratch' }));
    await user.click(await screen.findByRole('button', { name: 'Delete and regenerate' }));

    expect(await screen.findByText(/Generation failed: init exploded/)).toBeInTheDocument();
    expect(await screen.findByText('No semantic model yet.')).toBeInTheDocument();
    // Neither the deleted model's banner nor its draft survives.
    expect(screen.queryByText(/Generation was interrupted at table/)).not.toBeInTheDocument();
    expect(screen.queryByText('Orders')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save Changes' })).not.toBeInTheDocument();
  });
});

describe('SemanticModelPage — after a failed run', () => {
  const TWENTY = Array.from({ length: 20 }, (_, i) => `public.t${i}`);

  it('drops the pre-run draft and re-seeds it from the refetched model', async () => {
    /*
     * Batches that landed before the failure have already been reconciled server-side.
     * Saving the pre-run draft would revert them and mark them user-edited.
     */
    const user = userEvent.setup();
    const refetch = deferred<unknown>();
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(PARTIAL_MODEL as never)
      .mockReturnValueOnce(refetch.promise as never);
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: ['public.orders'], resumed: true,
    } as never);
    vi.mocked(semanticApi.generateBatch).mockRejectedValue(
      httpError(500, 'provider exploded') as never,
    );

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();

    // Refetch still in flight: no draft, so nothing stale can be saved.
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();

    refetch.resolve({
      ...PARTIAL_MODEL,
      tables: {
        'public.orders': { ...PARTIAL_MODEL.tables['public.orders'], display_name: 'Orders (regenerated)' },
      },
    });
    expect(await screen.findByText('Orders (regenerated)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });

  it('stops dispatching batches after a 409 and brings the banner back', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1', tables_total: 20, batch_count: 5, batch_size: 4,
      pending_table_keys: TWENTY, resumed: false,
    } as never);
    vi.mocked(semanticApi.generateBatch)
      .mockRejectedValueOnce(httpError(409, 'A restart superseded this run') as never)
      .mockResolvedValue({
        connection_id: 'conn-1', table_keys: [], tables_done: 4, tables_total: 20, batch_size: 4,
      } as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    expect(
      await screen.findByText(/Generation failed: A restart superseded this run/),
    ).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Restart from scratch' })).toBeEnabled();
    // Two workers each took one batch before the 409 landed; neither takes another.
    expect(semanticApi.generateBatch).toHaveBeenCalledTimes(2);
    expect(semanticApi.generateGlobals).not.toHaveBeenCalled();
  });

  it('leaves Restart available when resume is refused because the schema changed', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateInit)
      .mockRejectedValueOnce(httpError(409, 'The schema changed since this run started') as never)
      .mockResolvedValue({
        connection_id: 'conn-1', tables_total: 1, batch_count: 1, batch_size: 4,
        pending_table_keys: ['public.orders'], resumed: false,
      } as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    expect(await screen.findByText(/Generation failed: The schema changed/)).toBeInTheDocument();
    // A 409 is permanent: no retry loop.
    expect(semanticApi.generateInit).toHaveBeenCalledTimes(1);
    expect(semanticApi.generateBatch).not.toHaveBeenCalled();

    await user.click(await screen.findByRole('button', { name: 'Restart from scratch' }));
    await user.click(await screen.findByRole('button', { name: 'Delete and regenerate' }));
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(semanticApi.delete).toHaveBeenCalledTimes(1);
    expect(semanticApi.generateInit).toHaveBeenLastCalledWith('conn-1', 'provider-1', false);
  });
});

describe('SemanticModelPage — when the reseed after a failed run also fails', () => {
  it('keeps the editor inert until a later refetch succeeds, then reseeds', async () => {
    /*
     * The draft is dropped on failure, but `model` is still the pre-run cache. If an edit
     * could seed a draft from it, Save would come back with exactly the stale values the
     * drop exists to keep out.
     */
    const user = userEvent.setup();
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(PARTIAL_MODEL as never)
      .mockRejectedValueOnce(httpError(503, 'Service unavailable') as never)
      // Structurally identical to the first load, so `model` keeps its reference.
      .mockResolvedValue(PARTIAL_MODEL as never);
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: ['public.orders'], resumed: true,
    } as never);
    vi.mocked(semanticApi.generateBatch).mockRejectedValue(
      httpError(500, 'provider exploded') as never,
    );
    vi.mocked(semanticApi.update).mockImplementation(async (_id, sent) => sent as never);

    const { qc } = renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();
    await waitFor(() =>
      expect(qc.getQueryState(['semantic', 'conn-1'])?.status).toBe('error'),
    );

    await user.click(screen.getByText('Orders'));
    await user.type(screen.getByDisplayValue('Customer orders'), ' (edited)');
    expect(screen.getByDisplayValue('Customer orders')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();

    await qc.refetchQueries({ queryKey: ['semantic', 'conn-1'] });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );

    await user.type(screen.getByDisplayValue('Customer orders'), ' (edited)');
    expect(screen.getByDisplayValue('Customer orders (edited)')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(semanticApi.update).toHaveBeenCalledTimes(1));
    const sent = vi.mocked(semanticApi.update).mock.calls[0][1] as typeof PARTIAL_MODEL;
    expect(sent.tables['public.orders'].description).toBe('Customer orders (edited)');
  });
});

describe('SemanticModelPage — edits during a save', () => {
  it('keeps an edit made while the save was in flight', async () => {
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.get).mockResolvedValue(COMPLETE_MODEL as never);
    vi.mocked(semanticApi.update).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Save Changes' }));
    const reviewed = screen.getByRole('checkbox', { name: 'Reviewed by user' });
    expect(reviewed).not.toBeChecked();
    await user.click(reviewed);
    expect(reviewed).toBeChecked();

    // The server answers with the draft as it was sent — before the edit.
    gate.resolve(COMPLETE_MODEL);
    await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument());
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeChecked();
  });
});
