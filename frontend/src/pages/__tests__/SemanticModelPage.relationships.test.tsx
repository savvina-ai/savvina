// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../../api/semantic', () => ({
  semanticApi: {
    get: vi.fn(),
    getDrift: vi.fn(),
    getLastDrift: vi.fn(),
    getTombstones: vi.fn(),
    restoreTombstone: vi.fn(),
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

import { semanticApi } from '../../api/semantic';
import { connectionsApi } from '../../api/connections';
import SemanticModelPage from '../SemanticModelPage';

const col = (name: string) => ({
  display_name: name,
  description: null,
  value_mappings: [],
  is_sensitive: false,
});

// `store.orders` has only `id` annotated; the schema also has `customer_id`.
const MODEL = {
  tables: {
    'store.orders': {
      display_name: 'Orders',
      description: null,
      default_filters: [],
      columns: { id: col('id') },
    },
    'store.customers': {
      display_name: 'Customers',
      description: null,
      default_filters: [],
      columns: { id: col('id') },
    },
    'store.audit': {
      display_name: 'Audit',
      description: null,
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

const schemaTable = (name: string, columns: string[]) => ({
  catalog: null,
  schema_name: 'store',
  name,
  table_type: 'table',
  columns: columns.map((c) => ({ name: c })),
  row_count_approx: null,
  description: null,
});

const SCHEMA = {
  tables: [
    schemaTable('orders', ['id', 'customer_id']),
    schemaTable('customers', ['id', 'name']),
  ],
};

let qc: QueryClient;

function renderPage() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

async function openAddForm() {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole('tab', { name: /Relationships/ }));
  await user.click(screen.getByRole('button', { name: /Add Manual Relationship/ }));
  return user;
}

function optionValues(select: HTMLElement) {
  return within(select)
    .getAllByRole('option')
    .map((o) => (o as HTMLOptionElement).value)
    .filter(Boolean);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(semanticApi.getLastDrift).mockResolvedValue({ report: null, monitor_interval_minutes: 0 } as never);
  vi.mocked(semanticApi.getTombstones).mockResolvedValue([] as never);
  vi.mocked(semanticApi.get).mockResolvedValue(MODEL as never);
  vi.mocked(semanticApi.getDriftHistory).mockResolvedValue([] as never);
  vi.mocked(semanticApi.getSuggestions).mockResolvedValue([] as never);
  vi.mocked(connectionsApi.getSchema).mockResolvedValue(SCHEMA as never);
});

describe('SemanticModelPage — manual relationship columns', () => {
  it('offers the schema columns of the chosen table, not free text', async () => {
    const user = await openAddForm();
    await user.selectOptions(screen.getByLabelText('From Table'), 'store.orders');
    const from = screen.getByLabelText('From Column');
    await vi.waitFor(() => expect(optionValues(from)).toEqual(['id', 'customer_id']));
    expect(from.tagName).toBe('SELECT');

    await user.selectOptions(from, 'customer_id');
    await user.selectOptions(screen.getByLabelText('To Table'), 'store.customers');
    await user.selectOptions(screen.getByLabelText('To Column'), 'id');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(screen.getByText('store.orders.customer_id')).toBeInTheDocument();
    expect(screen.getByText('store.customers.id')).toBeInTheDocument();
  });

  it('clears the column when the new table does not have it', async () => {
    const user = await openAddForm();
    await user.selectOptions(screen.getByLabelText('From Table'), 'store.orders');
    const from = screen.getByLabelText('From Column');
    await vi.waitFor(() => expect(optionValues(from)).toContain('customer_id'));
    await user.selectOptions(from, 'customer_id');

    await user.selectOptions(screen.getByLabelText('From Table'), 'store.customers');
    expect(screen.getByLabelText('From Column')).toHaveValue('');
  });

  it('keeps a chosen column the schema no longer lists visible, marked, and blocks Add', async () => {
    const user = await openAddForm();
    await user.selectOptions(screen.getByLabelText('From Table'), 'store.orders');
    const from = screen.getByLabelText('From Column');
    await vi.waitFor(() => expect(optionValues(from)).toContain('customer_id'));
    await user.selectOptions(from, 'customer_id');
    await user.selectOptions(screen.getByLabelText('To Table'), 'store.customers');
    await user.selectOptions(screen.getByLabelText('To Column'), 'id');
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled();

    // A drift check refreshes the schema while the form is open; the column is gone.
    vi.mocked(connectionsApi.getSchema).mockResolvedValue({
      tables: [schemaTable('orders', ['id']), schemaTable('customers', ['id', 'name'])],
    } as never);
    await act(() => qc.invalidateQueries({ queryKey: ['schema', 'conn-1'] }));

    const after = screen.getByLabelText('From Column');
    expect(
      await within(after).findByRole('option', { name: 'customer_id (not in schema)' }),
    ).toBeInTheDocument();
    expect(after).toHaveValue('customer_id');
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    expect(
      screen.getByText('customer_id is not in the schema of store.orders. Pick another column.'),
    ).toBeInTheDocument();

    await user.selectOptions(after, 'id');
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled();
  });

  it('takes free text, suggesting the annotated columns, for a table the schema does not list', async () => {
    const user = await openAddForm();
    await user.selectOptions(screen.getByLabelText('From Table'), 'store.audit');
    const from = screen.getByLabelText('From Column');
    expect(from.tagName).toBe('INPUT');
  });

  it('lets a real but unannotated column be typed when no schema is cached (404)', async () => {
    vi.mocked(connectionsApi.getSchema).mockRejectedValue({ response: { status: 404 } });
    const user = await openAddForm();
    await user.selectOptions(screen.getByLabelText('From Table'), 'store.orders');
    const from = screen.getByLabelText('From Column');
    expect(from.tagName).toBe('INPUT');
    const listId = from.getAttribute('list');
    expect(listId).toBeTruthy();
    const suggestions = Array.from(
      document.getElementById(listId!)!.querySelectorAll('option'),
    ).map((o) => o.value);
    expect(suggestions).toEqual(['id']);

    await user.type(from, 'customer_id');
    await user.selectOptions(screen.getByLabelText('To Table'), 'store.customers');
    await user.type(screen.getByLabelText('To Column'), 'id');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(screen.getByText('store.orders.customer_id')).toBeInTheDocument();
    expect(screen.getByText('store.customers.id')).toBeInTheDocument();
  });
});
