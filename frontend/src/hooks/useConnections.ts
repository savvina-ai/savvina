// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import {
  connectionsApi,
  type CreateConnectionPayload,
  type PrivacySaveRequest,
} from '../api/connections';
import { datasourcesApi } from '../api/datasources';
import { useAppStore } from '../store/appStore';
import { schemaQueryOptions } from '../lib/schemaQuery';
import type { Connection, SchemaResponse } from '../types';

export function useConnections() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
  });
}

export function useConnection(id: string | null) {
  return useQuery({
    queryKey: ['connections', id],
    queryFn: () => connectionsApi.get(id!),
    enabled: !!id,
  });
}

export function useCreateConnection() {
  const queryClient = useQueryClient();
  const setActiveConnection = useAppStore((s) => s.setActiveConnection);

  return useMutation({
    mutationFn: (payload: CreateConnectionPayload) => connectionsApi.create(payload),
    onSuccess: (conn) => {
      queryClient.invalidateQueries({ queryKey: ['connections'] });
      setActiveConnection(conn.id);
    },
  });
}

export function useDeleteConnection() {
  const queryClient = useQueryClient();
  const { activeConnectionId, setActiveConnection } = useAppStore();

  return useMutation({
    mutationFn: (id: string) => connectionsApi.delete(id),
    onSuccess: (_, id) => {
      queryClient.invalidateQueries({ queryKey: ['connections'] });
      if (activeConnectionId === id) setActiveConnection(null);
    },
  });
}

export function useTestNewConnection() {
  return useMutation({
    mutationFn: ({ sourceType, config }: { sourceType: string; config: Record<string, unknown> }) =>
      connectionsApi.testNew(sourceType, config),
  });
}

/** `POST /connections/{id}/test`. A successful test rewrites the connection's stored
 *  privilege audit, so the detail is refetched and the banner shows the new result. */
export function useTestConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => connectionsApi.test(id),
    // Returned, so the mutation stays pending until the detail refetch lands.
    onSuccess: (_data, id) => queryClient.invalidateQueries({ queryKey: ['connections', id] }),
  });
}

/** The connection's cached schema (`null` on a 404: none cached yet), also written into the
 *  app store as the chat page's current schema. The store is set from the query's data, not
 *  inside the fetch, so a schema another reader of `['schema', id]` fetched first (the
 *  semantic page) reaches it too. */
export function useConnectionSchema(id: string | null) {
  const setSchema = useAppStore((s) => s.setSchema);
  const query = useQuery(schemaQueryOptions(id));
  const { data } = query;

  useEffect(() => {
    if (data) setSchema(data);
  }, [data, setSchema]);

  return query;
}

export function useUpdatePrivacySettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, settings }: { id: string; settings: PrivacySaveRequest }) =>
      connectionsApi.updatePrivacySettings(id, settings),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: ['connections'] });
      queryClient.invalidateQueries({ queryKey: ['connections', id] });
    },
  });
}

export function useUpdateExecutionMode() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, mode }: { id: string; mode: Connection['execution_mode'] }) =>
      connectionsApi.updateExecutionMode(id, mode),
    onSuccess: (_data, { id }) => {
      queryClient.invalidateQueries({ queryKey: ['connections'] });
      queryClient.invalidateQueries({ queryKey: ['connections', id] });
    },
  });
}

/** `schema.table` names from the connection's cached schema, for the row-filter datalist.
 *  `null` before the first schema refresh (404). Separate from `useConnectionSchema`, which
 *  also writes the schema into the app store as the chat page's current schema. */
export function useConnectionTableNames(id: string | null) {
  return useQuery({
    queryKey: ['schema-tables', id],
    queryFn: async () => {
      try {
        const schema = (await connectionsApi.getSchema(id!)) as SchemaResponse;
        const names = schema.tables
          .filter((t) => t.schema_name && t.name)
          .map((t) => `${t.schema_name}.${t.name}`);
        return Array.from(new Set(names));
      } catch (e: unknown) {
        const err = e as { response?: { status?: number } };
        if (err?.response?.status === 404) return null;
        throw e;
      }
    },
    enabled: !!id,
    staleTime: 5 * 60_000,
  });
}

/** The registered datasource types (`DataSourceInfo[]`): static for the life of the app. */
export function useDataSources() {
  return useQuery({
    queryKey: ['datasources'],
    queryFn: datasourcesApi.getAvailable,
    staleTime: Infinity,
  });
}
