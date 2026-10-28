// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { useConnectionTableNames } from '../useConnections';

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

describe('useConnectionTableNames', () => {
  it('returns schema.table names without empty parts or duplicates', async () => {
    server.use(
      http.get('http://localhost:8000/api/v1/connections/conn-1/schema', () =>
        HttpResponse.json({
          tables: [
            { schema_name: 'public', name: 'orders', columns: [] },
            { schema_name: '', name: 'ghost', columns: [] },
            { schema_name: 'public', name: '', columns: [] },
            { schema_name: 'public', name: 'orders', columns: [] },
            { schema_name: 'sales', name: 'items', columns: [] },
          ],
        }),
      ),
    );

    const { result } = renderHook(() => useConnectionTableNames('conn-1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(['public.orders', 'sales.items']);
  });
});
