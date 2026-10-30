// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { http, HttpResponse, delay } from 'msw';
import { server } from '../../test/server';
import { makeConnection } from '../../test/factories';
import ConnectionPage from '../ConnectionPage';

const API = 'http://localhost:8000';
const DETAIL = "Row filter must be a single condition without ';'";
const LEGACY_DETAIL =
  'row_filters: this connection still has the old whole-query filter. Define per-table ' +
  'filters, or send clear_legacy_row_filter to remove it.';
const LEGACY = {
  privacy_settings: { row_filter_sql: 'tenant_id = 42' },
  row_filter_needs_migration: true,
};

interface RenderOptions {
  detail?: Record<string, unknown>;
  /** Status of GET /connections/{id}; anything but 200 makes the detail fail to load. */
  detailStatus?: number;
  /** Answers PUT /privacy; defaults to a 422 with DETAIL. May change `state.detail`. */
  privacyResponse?: (state: { detail: Record<string, unknown> }) => Response;
}

function renderPage(options: RenderOptions = {}) {
  const conn = makeConnection();
  const modePuts: unknown[] = [];
  const privacyPuts: unknown[] = [];
  const state = {
    detail: {
      ...conn,
      privacy_settings: null,
      schema_cache: null,
      row_filter_needs_migration: false,
      privilege_audit: null,
      ...options.detail,
    } as Record<string, unknown>,
  };
  server.use(
    http.get(`${API}/api/v1/connections`, () =>
      HttpResponse.json({ items: [conn], total: 1, limit: 50, offset: 0 }),
    ),
    http.get(`${API}/api/v1/connections/${conn.id}`, () =>
      options.detailStatus && options.detailStatus !== 200
        ? HttpResponse.json({ detail: 'boom' }, { status: options.detailStatus })
        : HttpResponse.json(state.detail),
    ),
    http.get(`${API}/api/v1/connections/${conn.id}/schema`, () =>
      HttpResponse.json({ detail: 'Not found' }, { status: 404 }),
    ),
    http.get(`${API}/api/v1/datasources`, () => HttpResponse.json([])),
    http.put(`${API}/api/v1/connections/${conn.id}/privacy`, async ({ request }) => {
      privacyPuts.push(await request.json());
      return (
        options.privacyResponse?.(state) ??
        HttpResponse.json({ detail: DETAIL }, { status: 422 })
      );
    }),
    http.put(`${API}/api/v1/connections/${conn.id}/execution-mode`, async ({ request }) => {
      modePuts.push(await request.json());
      return HttpResponse.json(conn);
    }),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 0 }, mutations: { retry: 0 } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ConnectionPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { modePuts, privacyPuts };
}

async function openEditPanel() {
  await screen.findByText('Test Connection');
  // The ··· menu trigger is the only button in the card without an accessible name.
  const trigger = screen
    .getAllByRole('button')
    .find((b) => !b.textContent?.trim() && b.getAttribute('aria-haspopup') === 'menu')!;
  await userEvent.click(trigger);
  await userEvent.click(await screen.findByRole('menuitem', { name: 'Edit settings' }));
}

describe('ConnectionPage edit settings panel', () => {
  it('shows a rejected row filter and does not change the execution mode', async () => {
    const { modePuts, privacyPuts } = renderPage();

    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(await screen.findByText(DETAIL)).toBeInTheDocument();
    expect(privacyPuts).toHaveLength(1);
    expect(modePuts).toHaveLength(0);
    // The panel stays open so the user can fix the filter.
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Privacy Settings/ })).toBeInTheDocument();
  });

  it('sends row_filters on every save', async () => {
    const { privacyPuts } = renderPage();
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(privacyPuts[0]).toMatchObject({ row_filters: {} });
    expect(privacyPuts[0]).not.toHaveProperty('row_filter_sql');
  });

  it('a save on a legacy connection sends the map and not the old filter', async () => {
    const { privacyPuts } = renderPage({
      detail: LEGACY,
    });
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(privacyPuts[0]).toMatchObject({ row_filters: {} });
    expect(privacyPuts[0]).not.toHaveProperty('row_filter_sql');
  });

  it('shows the migration banner and removes the old filter on request', async () => {
    const { privacyPuts } = renderPage({
      detail: LEGACY,
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await openEditPanel();

    expect(
      await screen.findByText(/rejected until the row filter is redefined per table/),
    ).toBeInTheDocument();
    expect(screen.getByText('tenant_id = 42')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Remove the old filter' }));

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(privacyPuts).toEqual([{ row_filters: {}, clear_legacy_row_filter: true }]);
    confirm.mockRestore();
  });

  it('does nothing when the removal is not confirmed', async () => {
    const { privacyPuts } = renderPage({
      detail: LEGACY,
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Remove the old filter' }));

    expect(privacyPuts).toEqual([]);
    confirm.mockRestore();
  });

  it('disables Save when the connection detail did not load', async () => {
    const { privacyPuts } = renderPage({ detailStatus: 500 });
    await openEditPanel();

    const save = await screen.findByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    expect(screen.getByText("Could not load this connection's settings.")).toBeVisible();
    await userEvent.click(save);
    expect(privacyPuts).toEqual([]);
  });

  it('disables Save while the row filters are invalid', async () => {
    renderPage();
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });

    expect(screen.getByText('Choose a table for this condition')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public.orders' } });

    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it('explains a disabled Save and keeps the privacy section open while rows are invalid', async () => {
    renderPage();
    await openEditPanel();
    const hint = 'Fix the row filters in Privacy Settings to save.';

    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).not.toHaveAttribute('aria-describedby');
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });
    expect(screen.getByText(hint)).toBeVisible();
    const describedBy = screen.getByRole('button', { name: 'Save' }).getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(hint);

    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));

    expect(screen.getByText(hint)).toBeVisible();
    expect(screen.getByLabelText('Table 1')).toBeVisible();

    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public.orders' } });
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).not.toHaveAttribute('aria-describedby');
  });

  it('disables Save and shows an error when two rows name the same table', async () => {
    renderPage({
      detail: { privacy_settings: { row_filters: { 'public.orders': 'tenant_id = 42' } } },
    });
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 2'), { target: { value: 'public.orders' } });

    expect(screen.getByText('This table already has a filter')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('starts with the privacy section open on a legacy connection only', async () => {
    renderPage({ detail: LEGACY });
    await openEditPanel();

    expect(await screen.findByRole('button', { name: 'Add filter' })).toBeVisible();
  });

  it('starts with the privacy section closed on a normal connection', async () => {
    renderPage();
    await openEditPanel();

    await screen.findByRole('button', { name: 'Save' });
    expect(screen.queryByRole('button', { name: 'Add filter' })).not.toBeInTheDocument();
  });

  it('shows the 422 detail inline when an unrelated save hits a legacy connection', async () => {
    const { modePuts } = renderPage({
      detail: LEGACY,
      privacyResponse: () => HttpResponse.json({ detail: LEGACY_DETAIL }, { status: 422 }),
    });
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(await screen.findByText(LEGACY_DETAIL)).toBeInTheDocument();
    expect(modePuts).toHaveLength(0);
  });

  it('removing the old filter saves the typed rows with the flag and clears the banner', async () => {
    const { privacyPuts } = renderPage({
      detail: LEGACY,
      privacyResponse: (state) => {
        state.detail = {
          ...state.detail,
          privacy_settings: { row_filters: { 'public.orders': 'tenant_id = 7' } },
          row_filter_needs_migration: false,
        };
        return HttpResponse.json(state.detail);
      },
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public.orders' } });
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });
    await userEvent.click(screen.getByRole('button', { name: 'Remove the old filter' }));

    expect(confirm).toHaveBeenCalledWith(
      'Remove the old whole-query filter? The per-table filters in the form are saved with it; tables without one run unfiltered.',
    );
    expect(privacyPuts).toEqual([
      { row_filters: { 'public.orders': 'tenant_id = 7' }, clear_legacy_row_filter: true },
    ]);
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove the old filter' })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/rejected until the row filter is redefined/)).not.toBeInTheDocument();
    // The typed row stays in the form.
    expect(screen.getByLabelText('Table 1')).toHaveValue('public.orders');
    confirm.mockRestore();
  });

  it('disables the removal button while the row filters are invalid', async () => {
    renderPage({ detail: LEGACY });
    await openEditPanel();

    await userEvent.click(await screen.findByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });

    expect(screen.getByRole('button', { name: 'Remove the old filter' })).toBeDisabled();
  });

  it('shows a stored PostgreSQL key with the quotes the next save needs', async () => {
    renderPage({
      detail: { privacy_settings: { row_filters: { 'public.Orders': 'tenant_id = 42' } } },
    });
    // msw answers with the most recently added handler: this one must come after
    // renderPage, which registers a datasources handler that returns [].
    server.use(
      http.get(`${API}/api/v1/datasources`, () =>
        HttpResponse.json([
          {
            source_type: 'postgresql',
            display_name: 'PostgreSQL',
            icon: '',
            query_dialect: 'PostgreSQL',
            config_schema: { fields: [] },
          },
        ]),
      ),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(await screen.findByLabelText('Table 1')).toHaveValue('public."Orders"');
  });

  it('waits for a slow datasource list before showing the stored keys', async () => {
    renderPage({
      detail: { privacy_settings: { row_filters: { 'public.Orders': 'tenant_id = 42' } } },
    });
    server.use(
      http.get(`${API}/api/v1/datasources`, async () => {
        await delay(150);
        return HttpResponse.json([
          {
            source_type: 'postgresql',
            display_name: 'PostgreSQL',
            icon: '',
            query_dialect: 'PostgreSQL',
            config_schema: { fields: [] },
          },
        ]);
      }),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(await screen.findByLabelText('Table 1')).toHaveValue('public."Orders"');
  });

  it('saves a stored PostgreSQL key quoted when only another setting changed', async () => {
    // The form shows the key quoted; the save must send what the form shows, or the server
    // folds public.Orders to public.orders for a filter the admin never touched.
    const { privacyPuts } = renderPage({
      detail: { privacy_settings: { row_filters: { 'public.Orders': 'tenant_id = 42' } } },
      privacyResponse: () => HttpResponse.json(makeConnection()),
    });
    server.use(
      http.get(`${API}/api/v1/datasources`, () =>
        HttpResponse.json([
          {
            source_type: 'postgresql',
            display_name: 'PostgreSQL',
            icon: '',
            query_dialect: 'PostgreSQL',
            config_schema: { fields: [] },
          },
        ]),
      ),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(await screen.findByLabelText('Table 1')).toHaveValue('public."Orders"');
    await userEvent.click(screen.getByRole('checkbox', { name: /Include row counts/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(privacyPuts).toHaveLength(1));
    expect(privacyPuts[0]).toMatchObject({
      include_row_counts: true,
      row_filters: { 'public."Orders"': 'tenant_id = 42' },
    });
  });

  it('saves a stored PostgreSQL key quoted even when the datasource list fails to load', async () => {
    // Without the list there is no query_dialect; the connection's own source_type stands in,
    // or an untouched save would send public.Orders and the server would fold it.
    const { privacyPuts } = renderPage({
      detail: { privacy_settings: { row_filters: { 'public.Orders': 'tenant_id = 42' } } },
      privacyResponse: () => HttpResponse.json(makeConnection()),
    });
    server.use(
      http.get(`${API}/api/v1/datasources`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(await screen.findByLabelText('Table 1')).toHaveValue('public."Orders"');
    await userEvent.click(screen.getByRole('checkbox', { name: /Include row counts/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(privacyPuts).toHaveLength(1));
    expect(privacyPuts[0]).toMatchObject({
      include_row_counts: true,
      row_filters: { 'public."Orders"': 'tenant_id = 42' },
    });
  });
});

const SUPERUSER_AUDIT = {
  checked_at: '2026-10-29T10:00:00+00:00',
  role: 'savvina',
  status: 'ok',
  error: null,
  findings: [{ code: 'superuser', tables: [], functions: [], more_tables: 0, remediation: 'ALTER ROLE "savvina" NOSUPERUSER;' }],
  rls_in_use: true,
  rls_effective: { 'public.orders': false },
};

describe('ConnectionPage privilege audit banner', () => {
  it('is rendered below the migration banner when both apply', async () => {
    renderPage({ detail: { ...LEGACY, privilege_audit: SUPERUSER_AUDIT } });
    await openEditPanel();
    const migration = await screen.findByText(/Queries on this connection are rejected/);
    const audit = await screen.findByText(
      "Row-level security in the database does not apply to this connection's role.",
    );
    expect(migration.compareDocumentPosition(audit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText('ALTER ROLE "savvina" NOSUPERUSER;')).toBeInTheDocument();
  });

  it('Check again posts to /test and refetches the detail', async () => {
    const tests: string[] = [];
    const conn = makeConnection();
    const state: { audit: Record<string, unknown> } = {
      audit: { ...SUPERUSER_AUDIT, status: 'unavailable', findings: [], error: 'refused' },
    };
    renderPage({ detail: { privilege_audit: state.audit } });
    server.use(
      http.post(`${API}/api/v1/connections/${conn.id}/test`, () => {
        tests.push('test');
        state.audit = SUPERUSER_AUDIT;
        return HttpResponse.json({ success: true, message: 'Connected' });
      }),
      http.get(`${API}/api/v1/connections/${conn.id}`, () =>
        HttpResponse.json({
          ...conn,
          privacy_settings: null,
          schema_cache: null,
          row_filter_needs_migration: false,
          privilege_audit: state.audit,
        }),
      ),
    );
    await openEditPanel();
    expect(await screen.findByText('Privileges could not be checked: refused.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(
      await screen.findByText(
        "Row-level security in the database does not apply to this connection's role.",
      ),
    ).toBeInTheDocument();
    expect(tests).toEqual(['test']);
  });

  it('a failed Check again shows the error in the panel', async () => {
    const conn = makeConnection();
    renderPage({ detail: { privilege_audit: { ...SUPERUSER_AUDIT, status: 'unavailable', findings: [], error: 'refused' } } });
    server.use(
      http.post(`${API}/api/v1/connections/${conn.id}/test`, () =>
        HttpResponse.json({ detail: 'password authentication failed' }, { status: 400 }),
      ),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Check again' }));
    expect(await screen.findByText('password authentication failed')).toBeInTheDocument();
    expect(screen.getByText('Privileges could not be checked: refused.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled();
  });

  it('shows Checking… and disables the button while the test is pending', async () => {
    const conn = makeConnection();
    renderPage({ detail: { privilege_audit: { ...SUPERUSER_AUDIT, status: 'unavailable', findings: [], error: 'refused' } } });
    server.use(
      http.post(`${API}/api/v1/connections/${conn.id}/test`, async () => {
        await delay(300);
        return HttpResponse.json({ success: true, message: 'Connected' });
      }),
    );
    await openEditPanel();
    await userEvent.click(await screen.findByRole('button', { name: 'Check again' }));
    expect(await screen.findByRole('button', { name: 'Checking…' })).toBeDisabled();
    expect(await screen.findByRole('button', { name: 'Check again' })).toBeEnabled();
  });

  it('shows the checked line for a clean audit', async () => {
    renderPage({ detail: { privilege_audit: { ...SUPERUSER_AUDIT, findings: [], rls_in_use: false } } });
    await openEditPanel();
    // The Privacy Settings section starts collapsed (`hidden`).
    expect(await screen.findByText(/Privileges checked/)).not.toBeVisible();
    await userEvent.click(await screen.findByRole('button', { name: /Privacy Settings/ }));
    expect(await screen.findByText(/Privileges checked/)).toBeVisible();
  });
});
