// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  providersApi,
  type UpdateProviderPayload,
  type CreateProviderPayload,
  type FetchModelsPayload,
} from '../api/providers';

export function useProviders() {
  return useQuery({
    queryKey: ['providers'],
    queryFn: providersApi.list,
  });
}

export function useCreateProvider() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: CreateProviderPayload) => providersApi.create(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
}

export function useUpdateProvider() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: UpdateProviderPayload }) =>
      providersApi.update(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
}

export function useTestProvider() {
  return useMutation({
    mutationFn: (id: string) => providersApi.test(id),
  });
}

export function useDeleteProvider() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => providersApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
}

export function useFetchModels() {
  return useMutation({
    mutationFn: (payload: FetchModelsPayload) => providersApi.fetchModels(payload),
  });
}

export function useRefreshSavedModels() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (configId: string) => providersApi.refreshSavedModels(configId),
    onSuccess: () => {
      // Invalidate so provider card gets updated available_models from cache
      queryClient.invalidateQueries({ queryKey: ['providers'] });
    },
  });
}
