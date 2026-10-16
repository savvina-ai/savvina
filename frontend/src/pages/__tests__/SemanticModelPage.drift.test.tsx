// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../../api/semantic', () => ({
  semanticApi: {
    get: vi.fn(),
    getDrift: vi.fn(),
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

// No provider unless a test configures one.
const providers = vi.hoisted(() => ({ data: [] as unknown[] }));
vi.mock('../../hooks/useProviders', () => ({
  useProviders: () => providers,
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
  has_actionable_changes: false,
};

/** Pass the client of an earlier render to remount the page over the same caches. */
function renderPage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
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

async function clickCheckDrift(qc?: QueryClient) {
  const user = userEvent.setup();
  const view = renderPage(qc);
  await user.click(await screen.findByRole('button', { name: 'Check Drift' }));
  return view;
}

const PROVIDER = {
  id: 'provider-1',
  provider_type: 'claude',
  display_name: 'Claude',
  is_configured: true,
  current_model: 'claude-sonnet-4-5',
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

beforeEach(() => {
  vi.clearAllMocks();
  providers.data = [];
  vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
  vi.mocked(semanticApi.getDriftHistory).mockResolvedValue([] as never);
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
    // Focus stays on Check Drift, so the outcome is spoken from the status line.
    expect(screen.getByRole('status')).toHaveTextContent(
      'No drift: the model matches the live schema.',
    );
  });

  it('announces a repeat check again', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(REPORT as never);
    await clickCheckDrift();
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('No drift: the model matches'),
    );

    let finish!: (v: unknown) => void;
    vi.mocked(semanticApi.getDrift).mockImplementationOnce(
      (() => new Promise((resolve) => (finish = resolve))) as never,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    // Cleared while checking, so the same sentence is a change when it comes back.
    expect(screen.getByRole('status')).toHaveTextContent('');
    await act(async () => {
      finish(REPORT);
    });
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(
        'No drift: the model matches the live schema.',
      ),
    );
  });

  it('does not reload the schema after a failed re-check', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(REPORT as never);
    await clickCheckDrift();
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2));

    vi.mocked(semanticApi.getDrift).mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 400, data: { detail: 'unreachable' } },
    });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    expect(await screen.findByText(/Drift check failed: unreachable/)).toBeInTheDocument();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    // The failed check refreshed nothing; the live report it kept is the earlier one's.
    expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2);
  });

  it('says to wait when the check is rate limited', async () => {
    // slowapi's 429 body has `error`, not `detail`.
    vi.mocked(semanticApi.getDrift).mockRejectedValue({
      isAxiosError: true,
      response: { status: 429, data: { error: 'Rate limit exceeded: 5 per 1 minute' } },
    });
    await clickCheckDrift();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Drift check failed: Too many requests (Rate limit exceeded: 5 per 1 minute) — try again later',
    );
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
    // Not actionable here (has_actionable_changes is false), so no Preview & apply.
    expect(screen.getByRole('status')).toHaveTextContent(/^Schema drift detected: 1 warning\.$/);
  });

  it('says why Preview & apply is held instead of offering it', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('checkbox', { name: 'Reviewed by user' }));
    await user.click(screen.getByRole('button', { name: 'Check Drift' }));

    await screen.findByRole('button', { name: 'Preview & apply' });
    expect(screen.getByRole('status')).toHaveTextContent(
      'Schema drift detected: 1 warning. Preview & apply is not available yet: Save your changes first — applying drift discards unsaved edits.',
    );
    expect(screen.getByRole('status')).not.toHaveTextContent(/is available/);
  });

  it('does not offer Preview & apply on a report from the cached schema', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue({
      ...DRIFTED,
      schema_source: 'cache',
      schema_cached_at: '2026-09-24T07:16:41Z',
      refresh_error: 'connection refused',
    } as never);
    await clickCheckDrift();

    expect(await screen.findByText(/Could not reach the database/)).toBeInTheDocument();
    // An apply introspects live; with the database unreachable it can only fail.
    const preview = screen.getByRole('button', { name: 'Preview & apply' });
    expect(preview).toBeDisabled();
    expect(preview).toHaveAttribute(
      'title',
      'The database could not be reached — check drift again once it is back',
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      `Schema drift detected: 1 warning against the schema cached at ${new Date('2026-09-24T07:16:41Z').toLocaleString()}; the database could not be reached.`,
    );
    expect(screen.getByRole('status')).not.toHaveTextContent(/Preview & apply/);
  });

  it('announces when a drift can be previewed and applied', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue({
      ...REPORT,
      warnings: ['a', 'b'],
      warning_count: 2,
      has_actionable_changes: true,
    } as never);
    await clickCheckDrift();

    await screen.findByRole('button', { name: 'Preview & apply' });
    expect(screen.getByRole('status')).toHaveTextContent(
      'Schema drift detected: 2 warnings. Preview & apply is available.',
    );
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
    expect(screen.getByRole('status')).toHaveTextContent(
      `No drift against the schema cached at ${new Date('2026-09-24T07:16:41Z').toLocaleString()}; the database could not be reached.`,
    );
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

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Drift check failed: Could not reach the data source to check drift: connection refused/,
    );
  });
});

const DRIFTED = {
  ...REPORT,
  warnings: ['Table "ecommerce.coupons" referenced in semantic model no longer exists in the database'],
  warning_count: 1,
  has_actionable_changes: true,
};

const SUMMARY = {
  dry_run: false,
  changed: true,
  tables_added: [],
  tables_removed: ['ecommerce.coupons'],
  columns_removed: {},
  metrics_added: [],
  metrics_removed: ['Coupon uses'],
  joins_added: [],
  joins_removed: [],
  derived_columns_added: [],
  derived_columns_removed: [],
  segments_added: [],
  segments_removed: [],
  relationships_added: [],
  relationships_removed: [],
  tables_not_annotated: [],
  warnings: [],
};

const PREVIEW = { ...SUMMARY, dry_run: true };

/** The preview call answers PREVIEW; the real apply answers what `real` returns. */
function mockApply(
  real: () => Promise<unknown> = () => Promise.resolve({ semantic_model: MODEL, summary: SUMMARY }),
) {
  vi.mocked(semanticApi.applyDrift).mockImplementation(((
    _id: string,
    _provider?: string,
    dryRun?: boolean,
  ) => (dryRun ? Promise.resolve({ semantic_model: MODEL, summary: PREVIEW }) : real())) as never);
}

async function previewFromBanner() {
  await clickCheckDrift();
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Preview & apply' }));
  return user;
}

/** Check, preview and confirm; returns the render so a test can unmount it. */
async function renderAndApply(qc: QueryClient) {
  const view = await clickCheckDrift(qc);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Preview & apply' }));
  await user.click(await screen.findByRole('button', { name: 'Apply these changes' }));
  return view;
}

/** A run with nothing pending: init, then globals answers the model. */
function mockRun() {
  vi.mocked(semanticApi.generateInit).mockResolvedValue({
    connection_id: 'conn-1',
    tables_total: 0,
    batch_count: 0,
    batch_size: 4,
    pending_table_keys: [],
    resumed: false,
  } as never);
  vi.mocked(semanticApi.generateGlobals).mockResolvedValue(MODEL as never);
}

/** Regenerate from the toolbar and confirm it in the dialog. */
async function regenerate() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /^Regenerate$/ }));
  // The dialog hides the page behind it, so its own button is the one left.
  await user.click(screen.getAllByRole('button', { name: /^Regenerate$/ })[0]);
}

async function applyFromBanner() {
  const user = await previewFromBanner();
  await user.click(await screen.findByRole('button', { name: 'Apply these changes' }));
  return user;
}

describe('SemanticModelPage — Apply Drift', () => {
  it('offers it only when something can be applied', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue({
      ...REPORT,
      warnings: [
        "1 table(s) in the semantic model exist in the database but this connection's database role cannot read them: public.payroll. Their annotations are kept; grant the role access to use them.",
      ],
      warning_count: 1,
    } as never);
    await clickCheckDrift();

    expect(await screen.findByText(/Schema drift detected \(1 warning\)/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Preview & apply' })).not.toBeInTheDocument();
  });

  it('previews the change and writes nothing until confirmed', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    await previewFromBanner();

    expect(await screen.findByText('Apply Drift will:')).toBeInTheDocument();
    expect(screen.getByText('Will remove 1 table')).toBeInTheDocument();
    expect(screen.getByText('Will remove 1 metric')).toBeInTheDocument();
    expect(semanticApi.applyDrift).toHaveBeenCalledTimes(1);
    expect(semanticApi.applyDrift).toHaveBeenCalledWith('conn-1', undefined, true);
    // Preview & apply unmounted under the click; focus moves to the preview itself, so its
    // name and summary are read, and no stray Space can apply. Cancel is one Tab on, the
    // destructive confirm two.
    const panel = screen.getByRole('region', { name: 'Apply Drift will:' });
    expect(panel).toHaveFocus();
    expect(panel).toHaveAccessibleDescription('Will remove 1 table · Will remove 1 metric');
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Apply these changes' })).toHaveFocus();
    // Focus carried the news; the page's status line stays quiet.
    expect(screen.getByRole('status')).toHaveTextContent('');
  });

  it('cancelling the preview applies nothing', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    const user = await previewFromBanner();
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));

    expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument();
    expect(semanticApi.applyDrift).toHaveBeenCalledTimes(1);
    // Focus goes back to the control that opened the preview, not to the page body.
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toHaveFocus();
  });

  it('drops the preview and an earlier failure on the next Check Drift', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply(() => Promise.reject({ isAxiosError: true, response: { status: 400, data: { detail: 'boom' } } }));
    const user = await applyFromBanner();
    expect(await screen.findByText(/Apply drift failed: boom/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Check Drift' }));
    expect(await screen.findByRole('button', { name: 'Preview & apply' })).toBeInTheDocument();
    expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument();
    expect(screen.queryByText(/Apply drift failed/)).not.toBeInTheDocument();
  });

  it('applies on confirm, shows what changed and clears the report', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    await applyFromBanner();

    await waitFor(() => expect(semanticApi.applyDrift).toHaveBeenCalledWith('conn-1', undefined));
    expect(await screen.findByText('Schema drift applied')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 table')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 metric')).toBeInTheDocument();
    expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument();
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
    // The confirm unmounted with the preview; focus moves to the result, not the body.
    const result = screen.getByRole('region', { name: 'Schema drift applied' });
    expect(result).toHaveFocus();
    expect(result).toHaveAccessibleDescription('Removed 1 table · Removed 1 metric');
  });

  it('pauses editing while the apply runs', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    mockApply(() => new Promise((resolve) => (finish = resolve)));
    await applyFromBanner();

    expect(
      await screen.findByText(/Applying schema drift\. Editing is paused until it finishes\./),
    ).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Save Changes' });
    expect(save).toBeDisabled();
    // The apply's wait, not a generation run's.
    expect(save).toHaveAttribute(
      'title',
      'Wait for the drift apply to finish — saving now would overwrite the applied values',
    );
    expect(screen.getByRole('button', { name: 'Applying…' })).toBeDisabled();
    // A report taken mid-apply would be stale the moment the apply lands.
    expect(screen.getByRole('button', { name: 'Check Drift' })).toBeDisabled();
    // Not "Generating…": no generation run is in flight, only the apply.
    expect(screen.getByRole('button', { name: 'Regenerate' })).toBeInTheDocument();

    finish({ semantic_model: MODEL, summary: SUMMARY });
    expect(await screen.findByText('Schema drift applied')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
  });

  it('keeps the result until it is dismissed', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    const user = await applyFromBanner();

    await screen.findByText('Schema drift applied');
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText('Schema drift applied')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check Drift' })).toHaveFocus();
  });

  it('shows why an apply failed and keeps the preview', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply(() =>
      Promise.reject({
        isAxiosError: true,
        response: {
          status: 400,
          data: { detail: 'Could not reach the data source to apply drift: connection refused' },
        },
      }),
    );
    await applyFromBanner();

    // Beside the preview's buttons, to retry or cancel.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Apply drift failed: Could not reach the data source to apply drift/,
    );
    expect(screen.getByRole('button', { name: 'Apply these changes' })).toBeInTheDocument();
    expect(screen.queryByText('Schema drift applied')).not.toBeInTheDocument();
    // The failed apply reseeds the draft, so editing resumes.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );
  });

  it('dismisses a preview that found nothing to apply', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.applyDrift).mockResolvedValue({
      semantic_model: MODEL,
      summary: { ...SUMMARY, dry_run: true, changed: false, tables_removed: [], metrics_removed: [] },
    } as never);
    const user = await previewFromBanner();

    expect(await screen.findByText(/Nothing to apply/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply these changes' })).not.toBeInTheDocument();
    const panel = screen.getByRole('region', { name: 'Apply Drift will:' });
    expect(panel).toHaveFocus();
    expect(panel).toHaveAccessibleDescription(/Nothing to apply/);
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(/Nothing to apply/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check Drift' })).toHaveFocus();
  });

  it('needs a provider to apply a preview that adds tables', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.applyDrift).mockResolvedValue({
      semantic_model: MODEL,
      summary: { ...PREVIEW, tables_added: ['public.events'] },
    } as never);
    await previewFromBanner();

    // The test setup configures no provider; annotating the new table needs one.
    const apply = await screen.findByRole('button', { name: 'Apply these changes' });
    expect(apply).toBeDisabled();
    expect(apply).toHaveAttribute('title', 'Configure an LLM provider in Settings first');
    expect(screen.getByRole('region', { name: 'Apply Drift will:' })).toHaveFocus();
  });

  it('holds Preview & apply over unsaved edits', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    await clickCheckDrift();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('checkbox', { name: 'Reviewed by user' }));

    const preview = screen.getByRole('button', { name: 'Preview & apply' });
    expect(preview).toBeDisabled();
    expect(preview).toHaveAttribute(
      'title',
      'Save your changes first — applying drift discards unsaved edits',
    );
  });

  it('drops a shown preview once a save changes the model', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.update).mockResolvedValue({ ...MODEL, is_user_reviewed: true } as never);
    mockApply();
    const user = await previewFromBanner();
    await screen.findByText('Apply Drift will:');

    await user.click(screen.getByRole('checkbox', { name: 'Reviewed by user' }));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toBeInTheDocument();
  });

  it('leaves focus alone when a preview lands after the user moved on', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    vi.mocked(semanticApi.applyDrift).mockImplementation(
      (() => new Promise((resolve) => (finish = resolve))) as never,
    );
    await previewFromBanner();
    await screen.findByRole('button', { name: 'Preparing preview…' });

    const reviewed = screen.getByRole('checkbox', { name: 'Reviewed by user' });
    act(() => reviewed.focus());
    await act(async () => {
      finish({ semantic_model: MODEL, summary: PREVIEW });
      await new Promise((r) => setTimeout(r, 0));
    });

    // The user moved on; the preview landing must not pull focus into it, so the page's
    // status line says what landed instead.
    await screen.findByRole('button', { name: 'Apply these changes' });
    expect(screen.getByRole('region', { name: 'Apply Drift will:' })).not.toHaveFocus();
    expect(reviewed).toHaveFocus();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Apply Drift will: Will remove 1 table · Will remove 1 metric',
    );
  });

  it('leaves focus alone when an apply lands after the user moved on', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    mockApply(() => new Promise((resolve) => (finish = resolve)));
    await applyFromBanner();
    await screen.findByRole('button', { name: 'Applying…' });

    // Editing is paused mid-apply, but the provider picker still takes focus.
    const provider = screen.getByRole('combobox', { name: 'LLM provider' });
    act(() => provider.focus());
    await act(async () => {
      finish({ semantic_model: MODEL, summary: SUMMARY });
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(await screen.findByText('Schema drift applied')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Schema drift applied' })).not.toHaveFocus();
    expect(provider).toHaveFocus();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Schema drift applied: Removed 1 table · Removed 1 metric',
    );
  });

  it('ignores a preview that lands after the next Check Drift', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    vi.mocked(semanticApi.applyDrift).mockImplementation(
      (() => new Promise((resolve) => (finish = resolve))) as never,
    );
    const user = await previewFromBanner();
    await screen.findByRole('button', { name: 'Preparing preview…' });

    await user.click(screen.getByRole('button', { name: 'Check Drift' }));
    await waitFor(() => expect(semanticApi.getDrift).toHaveBeenCalledTimes(2));
    await act(async () => {
      finish({ semantic_model: MODEL, summary: PREVIEW });
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Preview & apply' })).toBeEnabled();
  });

  it('keeps a remounted page paused while an apply it did not start runs', async () => {
    const APPLIED = { ...MODEL, is_user_reviewed: true };
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    mockApply(() => new Promise((resolve) => (finish = resolve)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = await renderAndApply(qc);
    await screen.findByRole('button', { name: 'Applying…' });

    // Leaving and coming back mounts a new page instance over the same caches.
    unmount();
    renderPage(qc);
    expect(
      await screen.findByText(/Applying schema drift\. Editing is paused until it finishes\./),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toHaveAttribute(
      'title',
      'Wait for the drift apply to finish',
    );
    expect(screen.getByRole('button', { name: 'Check Drift' })).toBeDisabled();

    vi.mocked(semanticApi.get).mockResolvedValue(APPLIED as never);
    finish({ semantic_model: APPLIED, summary: SUMMARY });

    // The owner's handlers ran on the unmounted instance; this one reseeds itself.
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeEnabled(),
    );
    expect(screen.getByRole('checkbox', { name: 'Reviewed by user' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled();
  });

  it('holds Apply these changes while a save is in flight', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finishSave!: (v: unknown) => void;
    vi.mocked(semanticApi.update).mockImplementation(
      (() => new Promise((resolve) => (finishSave = resolve))) as never,
    );
    mockApply();
    const user = await previewFromBanner();
    await screen.findByText('Apply Drift will:');

    // A clean draft can be saved too; the save landing mid-apply would drop the preview,
    // and with it the only place a failed apply shows its error.
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    const apply = screen.getByRole('button', { name: 'Apply these changes' });
    expect(apply).toBeDisabled();
    expect(apply).toHaveAttribute('title', 'Wait for the save to finish');

    await act(async () => {
      finishSave(MODEL);
      await new Promise((r) => setTimeout(r, 0));
    });
    // The save changed the stored model, so the preview is dropped as stale.
    expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toBeEnabled();
  });

  it('hides the previous report when a re-check fails', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(DRIFTED as never);
    await clickCheckDrift();
    await screen.findByRole('button', { name: 'Preview & apply' });

    vi.mocked(semanticApi.getDrift).mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 400, data: { detail: 'unreachable' } },
    });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));

    expect(await screen.findByText(/Drift check failed: unreachable/)).toBeInTheDocument();
    // TanStack keeps the last data on a refetch error; it is not this check's result.
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Preview & apply' })).not.toBeInTheDocument();
  });

  it('shows why a preview failed', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.applyDrift).mockRejectedValue({
      isAxiosError: true,
      response: { status: 409, data: { detail: 'A generation run is in progress' } },
    });
    await previewFromBanner();

    expect(
      await screen.findByText(/Preview failed: A generation run is in progress/),
    ).toBeInTheDocument();

    // A fresh report starts clean.
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    expect(await screen.findByRole('button', { name: 'Preview & apply' })).toBeEnabled();
    expect(screen.queryByText(/Preview failed/)).not.toBeInTheDocument();
  });

  it('does not carry a failed apply over to a fresh preview after Cancel', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply(() => Promise.reject({ isAxiosError: true, response: { status: 400, data: { detail: 'boom' } } }));
    const user = await applyFromBanner();
    expect(await screen.findByText(/Apply drift failed: boom/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Preview & apply' }));
    expect(await screen.findByText('Apply Drift will:')).toBeInTheDocument();
    expect(screen.queryByText(/Apply drift failed/)).not.toBeInTheDocument();
  });

  it('does not carry a failed apply over to a preview started after a save', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.update).mockResolvedValue({ ...MODEL, is_user_reviewed: true } as never);
    mockApply(() => Promise.reject({ isAxiosError: true, response: { status: 400, data: { detail: 'boom' } } }));
    const user = await applyFromBanner();
    expect(await screen.findByText(/Apply drift failed: boom/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save Changes' })).toBeEnabled(),
    );

    // A save drops the preview without a Cancel or a Check Drift: only the next
    // preview's start can clear the failed apply.
    await user.click(screen.getByRole('checkbox', { name: 'Reviewed by user' }));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(screen.queryByText('Apply Drift will:')).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: 'Preview & apply' }));
    expect(await screen.findByText('Apply Drift will:')).toBeInTheDocument();
    expect(screen.queryByText(/Apply drift failed/)).not.toBeInTheDocument();
  });

  it('does not carry a failed apply over to the preview of a fresh check', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply(() => Promise.reject({ isAxiosError: true, response: { status: 400, data: { detail: 'boom' } } }));
    const user = await applyFromBanner();
    expect(await screen.findByText(/Apply drift failed: boom/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Check Drift' }));
    await user.click(await screen.findByRole('button', { name: 'Preview & apply' }));
    expect(await screen.findByText('Apply Drift will:')).toBeInTheDocument();
    expect(screen.queryByText(/Apply drift failed/)).not.toBeInTheDocument();
  });

  it('reloads the schema after a preview and again after an apply', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    const user = await previewFromBanner();
    await screen.findByText('Apply Drift will:');
    // Mount, the live check, then the preview.
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(3));

    // The apply introspected live too, and the purge keeps that schema cached.
    await user.click(screen.getByRole('button', { name: 'Apply these changes' }));
    await screen.findByText('Schema drift applied');
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(4));
  });

  it('holds Preview & apply while a re-check is in flight', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(DRIFTED as never);
    await clickCheckDrift();
    await screen.findByRole('button', { name: 'Preview & apply' });

    vi.mocked(semanticApi.getDrift).mockImplementationOnce((() => new Promise(() => {})) as never);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    await screen.findByRole('button', { name: 'Checking…' });
    // An apply landing first would have its result followed by this check's stale news.
    const preview = screen.getByRole('button', { name: 'Preview & apply' });
    expect(preview).toBeDisabled();
    expect(preview).toHaveAttribute('title', 'Wait for the drift check to finish');
  });

  it('says nothing of a check whose report a run dropped while it was in flight', async () => {
    providers.data = [PROVIDER];
    mockRun();
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(DRIFTED as never);
    await clickCheckDrift();
    await screen.findByText(/Schema drift detected \(/);
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2));

    let finish!: (v: unknown) => void;
    vi.mocked(semanticApi.getDrift).mockImplementationOnce(
      (() => new Promise((resolve) => (finish = resolve))) as never,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    await screen.findByRole('button', { name: 'Checking…' });
    // The run's start drops the report the check is refetching, so the check is not this
    // page's to report any more.
    await regenerate();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled(),
    );
    await act(async () => {
      finish(DRIFTED);
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(screen.getByRole('status')).not.toHaveTextContent(/Schema drift detected/);
    expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2);
  });

  it('does not announce or reload for a check whose report was removed mid-flight', async () => {
    // Apply and run are both held while a check is in flight, so the removal they would
    // cause is simulated directly on the shared client.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(semanticApi.getDrift).mockResolvedValueOnce(DRIFTED as never);
    await clickCheckDrift(qc);
    await screen.findByText(/Schema drift detected \(/);
    await waitFor(() => expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2));

    let finish!: (v: unknown) => void;
    vi.mocked(semanticApi.getDrift).mockImplementationOnce(
      (() => new Promise((resolve) => (finish = resolve))) as never,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    await screen.findByRole('button', { name: 'Checking…' });
    await act(async () => {
      qc.removeQueries({ queryKey: ['semantic-drift', 'conn-1'] });
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByRole('status')).toHaveTextContent(/^$/);
    expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2);

    await act(async () => {
      finish(DRIFTED);
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.getByRole('status')).toHaveTextContent(/^$/);
    expect(connectionsApi.getSchema).toHaveBeenCalledTimes(2);
  });

  it('drops the drift report when a run starts, not when it ends', async () => {
    providers.data = [PROVIDER];
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockRun();
    let finishGlobals!: (v: unknown) => void;
    vi.mocked(semanticApi.generateGlobals).mockImplementation(
      (() => new Promise((resolve) => (finishGlobals = resolve))) as never,
    );
    await clickCheckDrift();
    await screen.findByText(/Schema drift detected \(/);

    await regenerate();
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalled());
    // Mid-run: the report described the model the run is replacing.
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Preview & apply' })).not.toBeInTheDocument();

    await act(async () => {
      finishGlobals(MODEL);
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled(),
    );
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
  });

  it('does not bring the drift report back after a failed run', async () => {
    providers.data = [PROVIDER];
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockRun();
    vi.mocked(semanticApi.generateGlobals).mockRejectedValue({
      isAxiosError: true,
      response: { status: 500, data: { detail: 'globals failed' } },
    });
    await clickCheckDrift();
    await screen.findByText(/Schema drift detected \(/);

    await regenerate();
    expect(await screen.findByText(/Generation failed: globals failed/)).toBeInTheDocument();
    // The failed run still reconciled what landed; the old report is no truer of that.
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Preview & apply' })).not.toBeInTheDocument();
  });

  it('holds Check Drift while a run is in flight', async () => {
    providers.data = [PROVIDER];
    mockRun();
    vi.mocked(semanticApi.generateGlobals).mockImplementation(
      (() => new Promise(() => {})) as never,
    );
    renderPage();
    await screen.findByRole('button', { name: 'Check Drift' });

    await regenerate();
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalled());
    // A report taken mid-run would outlive the run with Preview & apply enabled.
    const check = screen.getByRole('button', { name: 'Check Drift' });
    expect(check).toBeDisabled();
    expect(check).toHaveAttribute('title', 'Wait for the current run to finish');
  });

  it('shows the result of an apply an earlier mount started', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let finish!: (v: unknown) => void;
    mockApply(() => new Promise((resolve) => (finish = resolve)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = await renderAndApply(qc);
    await screen.findByRole('button', { name: 'Applying…' });
    unmount();
    renderPage(qc);
    await screen.findByText(/Applying schema drift/);

    await act(async () => {
      finish({ semantic_model: MODEL, summary: SUMMARY });
    });
    expect(await screen.findByText('Schema drift applied')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 table')).toBeInTheDocument();
    expect(screen.queryByText(/Schema drift detected/)).not.toBeInTheDocument();
    // Nothing on this mount had focus to move; the status line says it.
    expect(screen.getByRole('status')).toHaveTextContent(
      'Schema drift applied: Removed 1 table · Removed 1 metric',
    );
  });

  it('shows the failure of an apply an earlier mount started', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let fail!: (e: unknown) => void;
    mockApply(() => new Promise((_resolve, reject) => (fail = reject)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = await renderAndApply(qc);
    await screen.findByRole('button', { name: 'Applying…' });
    unmount();
    renderPage(qc);
    await screen.findByText(/Applying schema drift/);

    await act(async () => {
      fail({ isAxiosError: true, response: { status: 400, data: { detail: 'boom' } } });
    });
    // No preview panel on this mount to hold it, so it stands on its own.
    expect(await screen.findByRole('alert')).toHaveTextContent('Apply drift failed: boom');
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toBeEnabled();

    // A fresh check clears it.
    await userEvent.setup().click(screen.getByRole('button', { name: 'Check Drift' }));
    await waitFor(() => expect(screen.queryByText(/Apply drift failed/)).not.toBeInTheDocument());
  });

  it('does not re-show an apply that settled before this mount', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = await renderAndApply(qc);
    await screen.findByText('Schema drift applied');
    unmount();

    renderPage(qc);
    await screen.findByRole('button', { name: 'Check Drift' });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.queryByText('Schema drift applied')).not.toBeInTheDocument();
  });

  it('does not show the failure of a preview a save superseded', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.update).mockResolvedValue({ ...MODEL, is_user_reviewed: true } as never);
    let failPreview!: (e: unknown) => void;
    vi.mocked(semanticApi.applyDrift).mockImplementation(
      (() => new Promise((_resolve, reject) => (failPreview = reject))) as never,
    );
    const user = await previewFromBanner();
    await screen.findByRole('button', { name: 'Preparing preview…' });

    // The save changes the model the preview was taken of; the report stays on screen.
    await user.click(screen.getByRole('checkbox', { name: 'Reviewed by user' }));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await screen.findByText('Saved');
    await act(async () => {
      failPreview({
        isAxiosError: true,
        response: { status: 409, data: { detail: 'A generation run is in progress' } },
      });
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.getByText(/Schema drift detected/)).toBeInTheDocument();
    expect(screen.queryByText(/Preview failed/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview & apply' })).toBeEnabled();
  });

  it('does not show the failure of a preview a run superseded', async () => {
    providers.data = [PROVIDER];
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    let failPreview!: (e: unknown) => void;
    vi.mocked(semanticApi.applyDrift).mockImplementation(
      (() => new Promise((_resolve, reject) => (failPreview = reject))) as never,
    );
    mockRun();
    await previewFromBanner();
    await screen.findByRole('button', { name: 'Preparing preview…' });

    await regenerate();
    await waitFor(() => expect(semanticApi.generateGlobals).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^Regenerate$/ })).toBeEnabled(),
    );
    await act(async () => {
      failPreview({
        isAxiosError: true,
        response: { status: 409, data: { detail: 'A generation run is in progress' } },
      });
      // The error reaches the page through TanStack's batched notify (a timer), so give it
      // more than one tick, or this would pass before it could fail.
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(screen.queryByText(/Preview failed/)).not.toBeInTheDocument();
  });

  it('holds Preview & apply and Regenerate while a suggestion is being applied', async () => {
    providers.data = [PROVIDER];
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.getSuggestions).mockResolvedValue([SUGGESTION] as never);
    vi.mocked(semanticApi.applySuggestion).mockImplementation(
      (() => new Promise(() => {})) as never,
    );
    await clickCheckDrift();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: /Suggestions/ }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    const wait = 'Wait for the suggestion to finish applying';
    const preview = screen.getByRole('button', { name: 'Preview & apply' });
    await waitFor(() => expect(preview).toBeDisabled());
    expect(preview).toHaveAttribute('title', wait);
    const regenerate = screen.getByRole('button', { name: 'Regenerate' });
    expect(regenerate).toBeDisabled();
    expect(regenerate).toHaveAttribute('title', wait);
  });

  it('holds Apply these changes while a suggestion is being applied', async () => {
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    vi.mocked(semanticApi.getSuggestions).mockResolvedValue([SUGGESTION] as never);
    vi.mocked(semanticApi.applySuggestion).mockImplementation(
      (() => new Promise(() => {})) as never,
    );
    mockApply();
    const user = await previewFromBanner();
    await screen.findByText('Apply Drift will:');
    await user.click(screen.getByRole('tab', { name: /Suggestions/ }));
    await user.click(screen.getByRole('button', { name: 'Apply' }));

    const apply = screen.getByRole('button', { name: 'Apply these changes' });
    await waitFor(() => expect(apply).toBeDisabled());
    expect(apply).toHaveAttribute('title', 'Wait for the suggestion to finish applying');
  });

  it('sends the selected provider with the preview and the apply', async () => {
    providers.data = [PROVIDER];
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    await applyFromBanner();

    await screen.findByText('Schema drift applied');
    expect(semanticApi.applyDrift).toHaveBeenNthCalledWith(1, 'conn-1', 'provider-1', true);
    expect(semanticApi.applyDrift).toHaveBeenNthCalledWith(2, 'conn-1', 'provider-1');
  });
});

describe('SemanticModelPage — Drift history', () => {
  it('lists logged applies and reloads them after an apply', async () => {
    vi.mocked(semanticApi.getDriftHistory).mockResolvedValue([
      { applied_at: '2026-10-16T09:30:00Z', applied_by: 'u1', summary: SUMMARY },
    ] as never);
    vi.mocked(semanticApi.getDrift).mockResolvedValue(DRIFTED as never);
    mockApply();
    await applyFromBanner();

    expect(await screen.findByText('Drift history (1)')).toBeInTheDocument();
    // Once on load, once more after the apply invalidated it.
    await waitFor(() => expect(semanticApi.getDriftHistory).toHaveBeenCalledTimes(2));
    expect(semanticApi.getDriftHistory).toHaveBeenCalledWith('conn-1', 10);
  });
});
