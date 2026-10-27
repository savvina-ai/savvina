// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeConnection } from '../../test/factories';
import ConnectionPage from '../ConnectionPage';

const API = 'http://localhost:8000';
const DETAIL = "Row filter must be a single condition without ';'";

function renderPage() {
  const conn = makeConnection();
  const modePuts: unknown[] = [];
  const privacyPuts: unknown[] = [];
  server.use(
    http.get(`${API}/api/v1/connections`, () =>
      HttpResponse.json({ items: [conn], total: 1, limit: 50, offset: 0 }),
    ),
    http.get(`${API}/api/v1/connections/${conn.id}`, () =>
      HttpResponse.json({ ...conn, privacy_settings: null, schema_cache: null }),
    ),
    http.get(`${API}/api/v1/datasources`, () => HttpResponse.json([])),
    http.put(`${API}/api/v1/connections/${conn.id}/privacy`, async ({ request }) => {
      privacyPuts.push(await request.json());
      return HttpResponse.json({ detail: DETAIL }, { status: 422 });
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

describe('ConnectionPage edit settings panel', () => {
  it('shows a rejected row filter and does not change the execution mode', async () => {
    const { modePuts, privacyPuts } = renderPage();

    await screen.findByText('Test Connection');
    // The ··· menu trigger is the only button in the card without an accessible name.
    const trigger = screen
      .getAllByRole('button')
      .find((b) => !b.textContent?.trim() && b.getAttribute('aria-haspopup') === 'menu')!;
    await userEvent.click(trigger);
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Edit settings' }));

    await userEvent.click(await screen.findByRole('button', { name: 'Save' }));

    expect(await screen.findByText(DETAIL)).toBeInTheDocument();
    expect(privacyPuts).toHaveLength(1);
    expect(modePuts).toHaveLength(0);
    // The panel stays open so the user can fix the filter.
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Privacy Settings/ })).toBeInTheDocument();
  });
});
