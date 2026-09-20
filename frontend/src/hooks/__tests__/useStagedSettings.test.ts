// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeAppSettings } from '../../test/factories';
import { apiErrorMessage } from '../../lib/apiError';
import { useStagedSettings } from '../useStagedSettings';
import type { AppSettings } from '../../types';

const API = 'http://localhost:8000';
const KEYS = ['default_query_timeout', 'default_row_limit'] as const;

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 0 }, mutations: { retry: 0 } },
  });
  return {
    queryClient,
    wrapper: ({ children }: { children: React.ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  };
}

function renderStaged() {
  const { wrapper, queryClient } = createWrapper();
  const { result } = renderHook(() => useStagedSettings(KEYS), { wrapper });
  return { result, queryClient };
}

beforeEach(() => {
  server.use(http.get(`${API}/api/v1/settings`, () => HttpResponse.json(makeAppSettings())));
});

describe('useStagedSettings', () => {
  it('initialises from GET with exactly the listed keys', async () => {
    const { result } = renderStaged();
    expect(result.current.values).toBeNull();
    await waitFor(() =>
      expect(result.current.values).toEqual({ default_query_timeout: 30, default_row_limit: 1000 }),
    );
    expect(result.current.dirty).toBe(false);
  });

  it('set makes it dirty; setting back to the server value makes it clean again', async () => {
    const { result } = renderStaged();
    await waitFor(() => expect(result.current.values).not.toBeNull());

    act(() => result.current.set('default_row_limit', 500));
    expect(result.current.dirty).toBe(true);
    expect(result.current.values?.default_row_limit).toBe(500);

    act(() => result.current.set('default_row_limit', 1000));
    expect(result.current.dirty).toBe(false);
  });

  it('save PUTs only the listed keys, with the staged values', async () => {
    const puts: Record<string, unknown>[] = [];
    server.use(
      http.put(`${API}/api/v1/settings`, async ({ request }) => {
        const body = (await request.json()) as Partial<AppSettings>;
        puts.push(body);
        return HttpResponse.json(makeAppSettings(body));
      }),
    );
    const { result } = renderStaged();
    await waitFor(() => expect(result.current.values).not.toBeNull());

    act(() => result.current.set('default_row_limit', 500));
    act(() => result.current.save());
    await waitFor(() => expect(result.current.update.isSuccess).toBe(true));

    expect(puts).toHaveLength(1);
    expect(Object.keys(puts[0]).sort()).toEqual([...KEYS].sort());
    expect(puts[0]).toEqual({ default_query_timeout: 30, default_row_limit: 500 });
  });

  it('a successful save clears dirty and adopts the response', async () => {
    // The PUT persists, so the invalidation refetch that follows sees the new value too.
    let serverState = makeAppSettings();
    server.use(
      http.get(`${API}/api/v1/settings`, () => HttpResponse.json(serverState)),
      http.put(`${API}/api/v1/settings`, async ({ request }) => {
        serverState = makeAppSettings((await request.json()) as Partial<AppSettings>);
        return HttpResponse.json(serverState);
      }),
    );
    const { result, queryClient } = renderStaged();
    await waitFor(() => expect(result.current.values).not.toBeNull());

    act(() => result.current.set('default_row_limit', 500));
    act(() => result.current.save());
    await waitFor(() => expect(result.current.update.isSuccess).toBe(true));

    expect(result.current.dirty).toBe(false);
    expect(result.current.values?.default_row_limit).toBe(500);
    expect(queryClient.getQueryData<AppSettings>(['settings'])?.default_row_limit).toBe(500);
  });

  it('a failed save surfaces the error and keeps the staged values', async () => {
    server.use(
      http.put(`${API}/api/v1/settings`, () =>
        HttpResponse.json({ detail: 'nope' }, { status: 500 }),
      ),
    );
    const { result } = renderStaged();
    await waitFor(() => expect(result.current.values).not.toBeNull());

    act(() => result.current.set('default_row_limit', 500));
    act(() => result.current.save());
    await waitFor(() => expect(result.current.update.isError).toBe(true));

    expect(result.current.dirty).toBe(true);
    expect(result.current.values?.default_row_limit).toBe(500);
    expect(apiErrorMessage(result.current.update.error)).toBe('nope');
  });

  it('untouched keys follow the server after a refetch; staged keys do not', async () => {
    const { result, queryClient } = renderStaged();
    await waitFor(() => expect(result.current.values).not.toBeNull());

    act(() => result.current.set('default_row_limit', 500));
    act(() => {
      queryClient.setQueryData(['settings'], makeAppSettings({ default_query_timeout: 60 }));
    });

    // Query observers are notified on a scheduler tick, not synchronously.
    await waitFor(() => expect(result.current.values?.default_query_timeout).toBe(60));
    expect(result.current.values?.default_row_limit).toBe(500);
  });

  it('save is a no-op before the GET has resolved', async () => {
    let putCalls = 0;
    server.use(
      http.get(`${API}/api/v1/settings`, () => new Promise<never>(() => {})),
      http.put(`${API}/api/v1/settings`, () => {
        putCalls += 1;
        return HttpResponse.json(makeAppSettings());
      }),
    );
    const { result } = renderStaged();
    act(() => result.current.save());
    await new Promise((r) => setTimeout(r, 20));
    expect(putCalls).toBe(0);
    expect(result.current.update.isPending).toBe(false);
  });
});
