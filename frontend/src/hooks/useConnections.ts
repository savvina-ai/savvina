// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  connectionsApi,
  type CreateConnectionPayload,
} from '../api/connections';
import { useAppStore } from '../store/appStore';
import type { PrivacySettings, Connection } from '../types';

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

export function useTestConnection() {
  return useMutation({
    mutationFn: (id: string) => connectionsApi.test(id),
  });
}

export function useConnectionSchema(id: string | null) {
  const setSchema = useAppStore((s) => s.setSchema);

  return useQuery({
    queryKey: ['schema', id],
    queryFn: async () => {
      try {
        const schema = await connectionsApi.getSchema(id!);
        setSchema(schema);
        return schema;
      } catch (e: unknown) {
        const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
        // 404 means no schema has been cached yet — treat as empty, not an error
        if (err?.response?.status === 404) return null;
        throw e;
      }
    },
    enabled: !!id,
    staleTime: 5 * 60_000,
  });
}

export function useUpdatePrivacySettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, settings }: { id: string; settings: Partial<PrivacySettings> }) =>
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
