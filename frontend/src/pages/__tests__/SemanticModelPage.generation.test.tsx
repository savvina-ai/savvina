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
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
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
import SemanticModelRoute from '../SemanticModelRoute';

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
    omitted_table_keys: [],
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

  // The banner reads the one generation mutation's error, which TanStack clears when the
  // next run starts. A failed full run followed by a successful Finish must not leave the
  // red banner up over a model that is now complete.
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
                   tables_total: 10, batch_size: 4, omitted_table_keys: [] });
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
    // Only saved edits survive: the run drops the draft, and is refused over unsaved ones.
    expect(dialog).toHaveTextContent(/Saved edits you made by hand are kept/);
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
        omitted_table_keys: [],
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

describe('SemanticModelPage — the reseed after a failed run', () => {
  it('re-enables the editor when the refetch resolves at once', async () => {
    /*
     * The reseed is the failure handler's own fetch, not an effect that has to observe
     * the cache update after the run stopped being pending.
     */
    const user = userEvent.setup();
    vi.mocked(semanticApi.get).mockResolvedValue(PARTIAL_MODEL as never);
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
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeEnabled();
    expect(semanticApi.get).toHaveBeenCalledTimes(2);
  });

  it('does not seed the draft when the reseed lands after another run has started', async () => {
    /*
     * The draft is empty after a failure, so nothing blocks a second run while the
     * reseed is still in flight. Its late result must not re-arm the editor mid-run.
     */
    const user = userEvent.setup();
    const refetch = deferred<unknown>();
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(PARTIAL_MODEL as never)
      .mockReturnValueOnce(refetch.promise as never)
      .mockResolvedValue(PARTIAL_MODEL as never);
    vi.mocked(semanticApi.generateInit)
      .mockResolvedValueOnce({
        connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
        pending_table_keys: ['public.orders'], resumed: true,
      } as never);
    vi.mocked(semanticApi.generateBatch).mockRejectedValue(
      httpError(500, 'provider exploded') as never,
    );

    const { qc } = renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();

    // Second run, held at init while the first run's reseed lands.
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);
    await user.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(semanticApi.generateInit).toHaveBeenCalledTimes(2));

    refetch.resolve(PARTIAL_MODEL);
    // The reseed has landed in the cache; flush what it chains before asserting.
    await waitFor(() =>
      expect(qc.getQueryState(['semantic', 'conn-1'])?.dataUpdateCount).toBe(2),
    );
    await act(async () => {});
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeDisabled();
  });
});

describe('SemanticModelPage — a refetch already in flight when a run fails', () => {
  it('seeds the draft from a fresh fetch, not from the older refetch', async () => {
    /*
     * A refetch started before the batches landed carries a pre-batch snapshot. Reusing
     * it for the reseed would put exactly the stale values back that dropping the draft
     * exists to keep out.
     */
    const user = userEvent.setup();
    const stale = deferred<unknown>();
    const named = (display_name: string) => ({
      ...PARTIAL_MODEL,
      tables: { 'public.orders': { ...PARTIAL_MODEL.tables['public.orders'], display_name } },
    });
    vi.mocked(semanticApi.get)
      .mockResolvedValueOnce(PARTIAL_MODEL as never)
      .mockReturnValueOnce(stale.promise as never)
      .mockResolvedValue(named('Orders (fresh)') as never);
    const initGate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(initGate.promise as never);
    vi.mocked(semanticApi.generateBatch).mockRejectedValue(
      httpError(500, 'provider exploded') as never,
    );

    const { qc } = renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    // A focus-style refetch starts mid-run, before any batch has landed.
    void qc.refetchQueries({ queryKey: ['semantic', 'conn-1'] });
    await waitFor(() => expect(semanticApi.get).toHaveBeenCalledTimes(2));

    initGate.resolve({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: ['public.orders'], resumed: true,
    });
    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();
    expect(await screen.findByText('Orders (fresh)')).toBeInTheDocument();

    stale.resolve(named('Orders (stale)'));
    await act(async () => {});
    expect(screen.queryByText('Orders (stale)')).not.toBeInTheDocument();
    expect(screen.getByText('Orders (fresh)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
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
    // Expanded before the run: user-event treats everything inside the disabled fieldset
    // as disabled (browsers only disable its form controls), so it cannot expand it later.
    await user.click(await screen.findByText('Orders'));
    await user.click(screen.getByRole('button', { name: 'Resume' }));
    expect(await screen.findByText(/Generation failed: provider exploded/)).toBeInTheDocument();
    await waitFor(() =>
      expect(qc.getQueryState(['semantic', 'conn-1'])?.status).toBe('error'),
    );

    expect(screen.getByDisplayValue('Customer orders')).toBeDisabled();
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

describe('SemanticModelPage — edits during a generation run', () => {
  it('ignores input while a run is in flight and shows the result when it lands', async () => {
    /*
     * Any edit typed mid-run would be replaced by the result; rather than lose it
     * silently, the draft is dropped when the run starts (every editor ignores input
     * while it is empty, exactly as after a failed run) and re-seeded from the result.
     */
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    const reviewed = screen.getByRole('checkbox', { name: 'Reviewed by user' });
    await user.click(reviewed);
    expect(reviewed).not.toBeChecked();

    gate.resolve({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    vi.mocked(semanticApi.generateGlobals).mockResolvedValue(
      { ...COMPLETE_MODEL, is_user_reviewed: true } as never,
    );
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeChecked(),
    );
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    // The result seeds draft and cache alike, so the page is not dirty afterwards.
    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled();
    expect(screen.queryByText(/Save your changes first/)).not.toBeInTheDocument();
  });
});

describe('SemanticModelPage — tables the LLM omitted', () => {
  it('re-queues omitted keys once and counts them done on the second omission', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1', tables_total: 4, batch_count: 1, batch_size: 4,
      pending_table_keys: ['public.a', 'public.b', 'public.c', 'public.d'], resumed: true,
    } as never);
    vi.mocked(semanticApi.generateBatch).mockImplementation(async (_c, keys) => ({
      connection_id: 'conn-1',
      table_keys: keys,
      tables_done: 0,
      tables_total: 4,
      batch_size: 4,
      omitted_table_keys: keys.includes('public.d') ? ['public.d'] : [],
    }) as never);
    // Held open so the bar can be read once every batch has landed.
    const globals = deferred<unknown>();
    vi.mocked(semanticApi.generateGlobals).mockReturnValue(globals.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));

    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(vi.mocked(semanticApi.generateBatch).mock.calls.map(([, keys]) => keys)).toEqual([
      ['public.a', 'public.b', 'public.c', 'public.d'],
      ['public.d'],
    ]);
    // public.d counted once — on its second omission — never twice, never not at all.
    expect(await screen.findByText('4 / 4')).toBeInTheDocument();

    globals.resolve(COMPLETE_MODEL);
    await waitFor(() => expect(screen.queryByText('4 / 4')).not.toBeInTheDocument());
  });

  it('retries an omission even after the sibling worker has run out of batches', async () => {
    /*
     * Two batches, two workers. The short batch lands first and its worker finds the
     * queue empty and exits; the re-queued key must still be picked up by the worker
     * whose batch reported it.
     */
    const user = userEvent.setup();
    const PENDING = ['public.a', 'public.b', 'public.c', 'public.d', 'public.e'];
    vi.mocked(semanticApi.generateInit).mockResolvedValue({
      connection_id: 'conn-1', tables_total: 5, batch_count: 2, batch_size: 4,
      pending_table_keys: PENDING, resumed: true,
    } as never);
    const firstBatch = deferred<unknown>();
    const batchResult = (keys: string[], omitted: string[]) => ({
      connection_id: 'conn-1', table_keys: keys, tables_done: 0, tables_total: 5,
      batch_size: 4, omitted_table_keys: omitted,
    });
    vi.mocked(semanticApi.generateBatch).mockImplementation((async (
      _c: string,
      keys: string[],
    ) => {
      if (keys.length === 4) return firstBatch.promise;
      return batchResult(keys, []);
    }) as never);
    const globals = deferred<unknown>();
    vi.mocked(semanticApi.generateGlobals).mockReturnValue(globals.promise as never);

    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(semanticApi.generateBatch).toHaveBeenCalledTimes(2));
    // The short batch has landed and its worker is gone: 1 of 5 counted.
    expect(await screen.findByText('1 / 5')).toBeInTheDocument();

    firstBatch.resolve(batchResult(PENDING.slice(0, 4), ['public.b']));

    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalledTimes(1));
    expect(vi.mocked(semanticApi.generateBatch).mock.calls.map(([, keys]) => keys)).toEqual([
      PENDING.slice(0, 4),
      ['public.e'],
      ['public.b'],
    ]);
    expect(await screen.findByText('5 / 5')).toBeInTheDocument();
    globals.resolve(COMPLETE_MODEL);
  });
});

describe('SemanticModelPage — the editor while a run is in flight', () => {
  it('does not re-seed the draft from a refetch that lands mid-run', async () => {
    /*
     * A focus refetch mid-run would otherwise hand the editors a pre-result draft, and
     * anything typed into it would be replaced by the result without a trace.
     */
    const user = userEvent.setup();
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);

    const { qc } = renderPage();
    await user.click(await screen.findByText('Orders'));
    await user.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(screen.getByDisplayValue('Customer orders')).toBeDisabled());

    vi.mocked(semanticApi.get).mockResolvedValue({
      ...PARTIAL_MODEL,
      tables: {
        'public.orders': { ...PARTIAL_MODEL.tables['public.orders'], description: 'Refetched' },
      },
    } as never);
    await qc.refetchQueries({ queryKey: ['semantic', 'conn-1'] });
    // Rendered from the refetch (as `model`, not as a draft).
    const description = await screen.findByDisplayValue('Refetched');

    expect(description).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();

    gate.resolve({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
    // The result re-seeds the draft (generateGlobals answers with COMPLETE_MODEL).
    expect(screen.getByDisplayValue('Customer orders')).toBeEnabled();
  });

  it('disables Apply on a suggestion while a run is in flight', async () => {
    const user = userEvent.setup();
    vi.mocked(semanticApi.getSuggestions).mockResolvedValue([
      {
        id: 'sug-1',
        connection_id: 'conn-1',
        table_key: 'public.orders',
        field: 'status',
        correction_type: 'add_value_mapping',
        value: { raw_value: 'CMPLT', display_value: 'Completed' },
        is_applied: false,
        source_message_id: 'msg-1',
        created_at: '2026-01-02T10:00:00Z',
      },
    ] as never);
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);

    renderPage();
    await user.click(await screen.findByRole('tab', { name: /Suggestions \(1\)/ }));
    expect(await screen.findByRole('button', { name: 'Apply' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Apply' })).toHaveAttribute(
      'title',
      expect.stringMatching(/generation/i),
    );
    expect(semanticApi.applySuggestion).not.toHaveBeenCalled();

    gate.resolve({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled());

    // Applying seeds draft and cache alike, so the page is not dirty afterwards.
    vi.mocked(semanticApi.applySuggestion).mockResolvedValue(
      { ...COMPLETE_MODEL, is_user_reviewed: true } as never,
    );
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeChecked(),
    );
    expect(semanticApi.applySuggestion).toHaveBeenCalledWith('conn-1', 'sug-1');
    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled();
    expect(screen.queryByText(/Save your changes first/)).not.toBeInTheDocument();
  });
});

describe('SemanticModelPage — unsaved edits', () => {
  it('blocks every way to start a run until the edits are saved', async () => {
    /*
     * Every run drops the draft, and Regenerate keeps only what the server holds — so
     * starting one over unsaved edits would discard them without a word.
     */
    const user = userEvent.setup();
    vi.mocked(semanticApi.update).mockImplementation(async (_id, sent) => sent as never);

    renderPage();
    const regenerate = await screen.findByRole('button', { name: /^Regenerate$/ });
    expect(regenerate).toBeEnabled();

    await user.click(screen.getByRole('checkbox', { name: 'Reviewed by user' }));

    const reason = /Save your changes first/;
    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toHaveAttribute(
      'title',
      expect.stringMatching(reason),
    );
    for (const name of ['Resume', "Finish with what's generated", 'Restart from scratch']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
      expect(screen.getByRole('button', { name })).toHaveAttribute(
        'title',
        expect.stringMatching(reason),
      );
    }
    expect(screen.getByText(reason)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument());

    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled();
    for (const name of ['Resume', "Finish with what's generated", 'Restart from scratch']) {
      expect(screen.getByRole('button', { name })).toBeEnabled();
    }
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
  });

  it('blocks applying a suggestion until the edits are saved', async () => {
    // Applying replaces the draft with the server's result, so unsaved edits would go.
    const user = userEvent.setup();
    vi.mocked(semanticApi.update).mockImplementation(async (_id, sent) => sent as never);
    vi.mocked(semanticApi.getSuggestions).mockResolvedValue([
      {
        id: 'sug-1',
        connection_id: 'conn-1',
        table_key: 'public.orders',
        field: 'status',
        correction_type: 'add_value_mapping',
        value: { raw_value: 'CMPLT', display_value: 'Completed' },
        is_applied: false,
        source_message_id: 'msg-1',
        created_at: '2026-01-02T10:00:00Z',
      },
    ] as never);

    renderPage();
    await user.click(await screen.findByRole('tab', { name: /Suggestions \(1\)/ }));
    expect(await screen.findByRole('button', { name: 'Apply' })).toBeEnabled();

    await user.click(screen.getByRole('checkbox', { name: 'Reviewed by user' }));
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Apply' })).toHaveAttribute(
      'title',
      'Save your changes first — applying a suggestion discards unsaved edits',
    );

    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
    expect(semanticApi.applySuggestion).not.toHaveBeenCalled();
  });

  it('is not dirty when an edit is undone', async () => {
    const user = userEvent.setup();
    renderPage();
    const reviewed = await screen.findByRole('checkbox', { name: 'Reviewed by user' });
    await user.click(reviewed);
    await user.click(reviewed);
    expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled();
  });
});

describe('SemanticModelPage — switching connections mid-run', () => {
  it("remounts per connection, so a late result lands only in its own connection's cache", async () => {
    /*
     * The route element is reused when only `:connectionId` changes. Unkeyed, the draft,
     * the progress bar and the pending run carried over to the next connection, and
     * TanStack runs a pending mutation's onSuccess with the latest options — so A's
     * result was written into B's cache and draft.
     */
    const user = userEvent.setup();
    const MODEL_B = {
      ...COMPLETE_MODEL,
      tables: {
        'public.customers': {
          display_name: 'Customers',
          description: 'People who buy',
          default_filters: [],
          columns: {},
        },
      },
    };
    const RESULT_A = {
      ...COMPLETE_MODEL,
      tables: {
        'public.orders': { ...BASE_MODEL.tables['public.orders'], display_name: 'Orders (final)' },
      },
    };
    vi.mocked(semanticApi.get).mockImplementation(
      async (id: string) => (id === 'conn-2' ? MODEL_B : PARTIAL_MODEL) as never,
    );
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateGlobals).mockReturnValue(gate.promise as never);

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/semantic/conn-1']}>
          <Link to="/semantic/conn-2">Go to B</Link>
          <Routes>
            <Route path="/semantic/:connectionId" element={<SemanticModelRoute />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('button', { name: /Finish with what's generated/i }));
    expect(await screen.findByText('Building metrics and joins…')).toBeInTheDocument();

    await user.click(screen.getByRole('link', { name: 'Go to B' }));
    expect(await screen.findByText('Customers')).toBeInTheDocument();
    // A's run does not follow the user to B.
    expect(screen.queryByText('Building metrics and joins…')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );

    gate.resolve(RESULT_A);
    await waitFor(() => expect(qc.getQueryData(['semantic', 'conn-1'])).toEqual(RESULT_A));
    expect(qc.getQueryData(['semantic', 'conn-2'])).toEqual(MODEL_B);
    expect(screen.getByText('Customers')).toBeInTheDocument();
    expect(screen.queryByText('Orders (final)')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });
});

describe('SemanticModelPage — returning to a connection whose run is still going', () => {
  it('keeps the editor inert until that run ends, then seeds from its result', async () => {
    /*
     * Leaving mid-run unmounts the page that owns the run, but the run itself keeps
     * going. The page mounted on return has its own mutation, so without looking at the
     * mutation cache it would think nothing runs and let the user edit, save or start a
     * second run over the first.
     */
    const user = userEvent.setup();
    const RESULT_A = { ...COMPLETE_MODEL, is_user_reviewed: true };
    let serverA: unknown = PARTIAL_MODEL;
    vi.mocked(semanticApi.get).mockImplementation(
      async (id: string) => (id === 'conn-2' ? COMPLETE_MODEL : serverA) as never,
    );
    const gate = deferred<unknown>();
    vi.mocked(semanticApi.generateInit).mockReturnValue(gate.promise as never);
    vi.mocked(semanticApi.generateGlobals).mockImplementation(async () => {
      serverA = RESULT_A;
      return RESULT_A as never;
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/semantic/conn-1']}>
          <Link to="/semantic/conn-1">Go to A</Link>
          <Link to="/semantic/conn-2">Go to B</Link>
          <Routes>
            <Route path="/semantic/:connectionId" element={<SemanticModelRoute />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('button', { name: 'Resume' }));
    await user.click(screen.getByRole('link', { name: 'Go to B' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
    await user.click(screen.getByRole('link', { name: 'Go to A' }));

    expect(await screen.findByText(/Generation is running/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generating…' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();
    // Give the remount's own refetch time to land: it must not arm the editor.
    await waitFor(() => expect(semanticApi.get).toHaveBeenCalledTimes(3));
    await act(async () => {});
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeDisabled();

    gate.resolve({
      connection_id: 'conn-1', tables_total: 10, batch_count: 1, batch_size: 4,
      pending_table_keys: [], resumed: true,
    });
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeEnabled(),
    );
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
    expect(screen.queryByText(/Generation is running/)).not.toBeInTheDocument();
  });
});
