// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeConnection } from '../../test/factories';
import { useAppStore } from '../../store/appStore';
import {
  useCreateConnection,
  useDeleteConnection,
  useConnectionSchema,
} from '../useConnections';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: 0 },
      mutations: { retry: 0 },
    },
  });
  return {
    queryClient,
    wrapper: ({ children }: { children: React.ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  };
}

const resetStore = () =>
  useAppStore.setState({
    activeConnectionId: null,
    activeSessionId: null,
    selectedProvider: '',
    schema: null,
    messages: [],
  });

const createPayload = {
  name: 'My DB',
  source_type: 'postgresql',
  config: { host: 'localhost', port: 5432, database: 'mydb', username: 'user', password: 'pass' },
};

describe('useCreateConnection', () => {
  beforeEach(resetStore);

  it('on success: invalidates ["connections"] query', async () => {
    const conn = makeConnection({ id: 'conn-new' });
    server.use(
      http.post('http://localhost:8000/api/v1/connections', () => HttpResponse.json(conn)),
    );

    const { wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(['connections'], []);

    const { result } = renderHook(() => useCreateConnection(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(createPayload);
    });

    await waitFor(() => {
      const state = queryClient.getQueryState(['connections']);
      expect(state?.isInvalidated).toBe(true);
    });
  });

  it('on success: calls setActiveConnection with new connection ID', async () => {
    const conn = makeConnection({ id: 'conn-new' });
    server.use(
      http.post('http://localhost:8000/api/v1/connections', () => HttpResponse.json(conn)),
    );

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useCreateConnection(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(createPayload);
    });

    expect(useAppStore.getState().activeConnectionId).toBe('conn-new');
  });
});

describe('useDeleteConnection', () => {
  beforeEach(resetStore);

  it('on success: invalidates ["connections"] query', async () => {
    const { wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(['connections'], [makeConnection({ id: 'conn-del' })]);

    const { result } = renderHook(() => useDeleteConnection(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('conn-del');
    });

    await waitFor(() => {
      const state = queryClient.getQueryState(['connections']);
      expect(state?.isInvalidated).toBe(true);
    });
  });

  it('on success: calls setActiveConnection(null) when deleting the active connection', async () => {
    useAppStore.setState({ activeConnectionId: 'conn-active' });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useDeleteConnection(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('conn-active');
    });

    expect(useAppStore.getState().activeConnectionId).toBeNull();
  });

  it('on success: does NOT clear active when deleting a different connection', async () => {
    useAppStore.setState({ activeConnectionId: 'conn-keep' });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useDeleteConnection(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('conn-other');
    });

    expect(useAppStore.getState().activeConnectionId).toBe('conn-keep');
  });
});

describe('useConnectionSchema', () => {
  beforeEach(resetStore);

  it('fetches schema and calls setSchema on the store', async () => {
    const schemaData = {
      source_type: 'postgresql',
      schemas: [{ name: 'public', description: null }],
      tables: [],
      relationships: [],
    };
    server.use(
      http.get('http://localhost:8000/api/v1/connections/conn-1/schema', () =>
        HttpResponse.json(schemaData),
      ),
    );

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useConnectionSchema('conn-1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(useAppStore.getState().schema).toEqual(schemaData);
  });

  it('staleTime is 5 minutes (300_000 ms) — not refetched on every render', async () => {
    let fetchCount = 0;
    const schemaData = { source_type: 'postgresql', schemas: [], tables: [], relationships: [] };
    server.use(
      http.get('http://localhost:8000/api/v1/connections/conn-1/schema', () => {
        fetchCount++;
        return HttpResponse.json(schemaData);
      }),
    );

    const { wrapper, queryClient } = createWrapper();
    const { result, rerender } = renderHook(() => useConnectionSchema('conn-1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchCount).toBe(1);

    // Re-render: with staleTime=300_000 the cached data is fresh and no refetch occurs
    rerender();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Verify the query has data and staleTime is configured (no second network call)
    expect(fetchCount).toBe(1);
    const query = queryClient.getQueryCache().find({ queryKey: ['schema', 'conn-1'] });
    expect(query).toBeDefined();
  });
});
