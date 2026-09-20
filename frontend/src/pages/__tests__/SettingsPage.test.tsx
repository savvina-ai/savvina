// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeAppSettings, makeProviderStatus } from '../../test/factories';
import { useAppStore } from '../../store/appStore';
import SettingsPage from '../SettingsPage';
import type { AppSettings } from '../../types';

const API = 'http://localhost:8000';

function LocationProbe() {
  return <span data-testid="search">{useLocation().search}</span>;
}

function renderPage(entry = '/settings') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 0 }, mutations: { retry: 0 } },
  });
  // The page reads its active tab from ?tab=, so it needs a router in the tree.
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={queryClient}>
        <SettingsPage />
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function openTab(name: RegExp) {
  await userEvent.click(screen.getByRole('tab', { name }));
}

/** Register a PUT that records each body and echoes it back, as the real endpoint does. */
function recordPuts() {
  const puts: Record<string, unknown>[] = [];
  server.use(
    http.put(`${API}/api/v1/settings`, async ({ request }) => {
      const body = (await request.json()) as Partial<AppSettings>;
      puts.push(body);
      return HttpResponse.json(makeAppSettings(body));
    }),
  );
  return puts;
}

async function findToggle(label: string) {
  const row = (await screen.findByText(label)).closest('div')!.parentElement!;
  return within(row).getByRole('button');
}

beforeEach(() => {
  // The Examples Library tab needs one; SettingsPage passes `activeConnectionId ?? ''`.
  useAppStore.setState({ activeConnectionId: 'conn-1' });
  server.use(
    http.get(`${API}/api/v1/providers`, () =>
      HttpResponse.json({ items: [makeProviderStatus()], total: 1, limit: 50, offset: 0 }),
    ),
    http.get(`${API}/api/v1/settings`, () => HttpResponse.json(makeAppSettings())),
  );
});

describe('AI & Optimization tab', () => {
  it('stages both toggles until Save, then sends them together', async () => {
    // A toggle that wrote on click saved half the form behind the user's back: the
    // sliders beside it stayed unsaved while the page showed "✓ Saved".
    const puts = recordPuts();
    renderPage();
    await openTab(/AI & Optimization/);

    await userEvent.click(await findToggle('Enable Query Cache'));
    await userEvent.click(await findToggle('Enable Schema Pruning'));
    expect(puts).toHaveLength(0);

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('✓ Saved');
    expect(puts).toHaveLength(1);
    // makeAppSettings() has both enabled, so one click each flips both to false.
    expect(puts[0]).toMatchObject({ cache_enabled: false, schema_pruning_enabled: false });
  });

  it('keeps Save disabled until something changes', async () => {
    renderPage();
    await openTab(/AI & Optimization/);
    await screen.findByText('Enable Query Cache');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    await userEvent.click(await findToggle('Enable Query Cache'));
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it('hides "✓ Saved" as soon as a saved form is edited again', async () => {
    // The mutation's isSuccess stays true after the first save, so without a dirty
    // check the badge sat beside a toggle the server had never seen.
    recordPuts();
    renderPage();
    await openTab(/AI & Optimization/);

    await userEvent.click(await findToggle('Enable Query Cache'));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('✓ Saved');

    await userEvent.click(await findToggle('Enable Query Cache'));
    expect(screen.queryByText('✓ Saved')).toBeNull();
  });
});

describe('Query Execution tab', () => {
  it('shows a 422 validation error instead of silently keeping the unsaved value', async () => {
    server.use(
      http.put(`${API}/api/v1/settings`, () =>
        HttpResponse.json(
          { detail: [{ loc: ['body', 'default_query_timeout'], msg: 'too big' }] },
          { status: 422 },
        ),
      ),
    );
    renderPage();
    await openTab(/Query Execution/);
    const [timeoutSlider] = await screen.findAllByRole('slider');

    fireEvent.change(timeoutSlider, { target: { value: '60' } });
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText(/default_query_timeout: too big/)).toBeInTheDocument();
    expect(screen.queryByText('✓ Saved')).toBeNull();
  });
});

describe('SettingsPage tab persistence', () => {
  it('opens the tab named in the URL, which is what survives a refresh', async () => {
    renderPage('/settings?tab=system');
    expect(await screen.findByRole('tab', { name: /System & Security/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('falls back to the first tab when ?tab= is absent or unknown', async () => {
    const { unmount } = renderPage();
    expect(await screen.findByRole('tab', { name: /LLM Providers/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    unmount();
    renderPage('/settings?tab=nonsense');
    expect(await screen.findByRole('tab', { name: /LLM Providers/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('writes the picked tab into the URL', async () => {
    renderPage();
    await openTab(/Query Execution/);
    expect(await screen.findByTestId('search')).toHaveTextContent('?tab=execution');
  });
});

describe('ProviderCard editor', () => {
  it('discards a typed API key when the edit is cancelled', async () => {
    // The key is not derived from the provider, so without an explicit clear it
    // survived into the next Edit and would be submitted on the following Save.
    renderPage();
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));

    const keyField = screen.getByPlaceholderText(/API key \(leave blank/);
    await userEvent.type(keyField, 'sk-should-not-survive');
    expect(keyField).toHaveValue('sk-should-not-survive');

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByPlaceholderText(/API key \(leave blank/)).toHaveValue('');
  });
});
