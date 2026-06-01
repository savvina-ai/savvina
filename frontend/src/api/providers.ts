// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type { PaginatedResponse, ProviderStatus } from '../types';

export interface UpdateProviderPayload {
  api_key?: string;
  model?: string;
  temperature?: number;
  is_active?: boolean;
  base_url?: string;
  display_name?: string;
}

export interface CreateProviderPayload extends UpdateProviderPayload {
  provider_type: string;
}

export interface TestNewProviderPayload {
  provider_type: string;
  api_key?: string;
  model?: string;
  base_url?: string;
}

export interface FetchModelsPayload {
  provider_type: string;
  api_key?: string;
  base_url?: string;
}

export const providersApi = {
  list: () =>
    apiClient
      .get<PaginatedResponse<ProviderStatus>>('/api/v1/providers')
      .then(r => r.data.items),

  create: ({ provider_type, ...rest }: CreateProviderPayload) =>
    apiClient
      .post<ProviderStatus>(`/api/v1/providers/${provider_type}`, rest)
      .then(r => r.data),

  update: (providerId: string, payload: UpdateProviderPayload) =>
    apiClient
      .put<ProviderStatus>(`/api/v1/providers/${providerId}/config`, payload)
      .then(r => r.data),

  test: (providerId: string) =>
    apiClient
      .post<{ success: boolean; message: string; latency_ms?: number }>(
        `/api/v1/providers/${providerId}/test`,
      )
      .then(r => r.data),

  testNew: (payload: TestNewProviderPayload) =>
    apiClient
      .post<{ success: boolean; message: string }>('/api/v1/providers/test', payload)
      .then(r => r.data),

  delete: (providerId: string) =>
    apiClient.delete(`/api/v1/providers/${providerId}`).then(r => r.data),

  fetchModels: (payload: FetchModelsPayload) =>
    apiClient
      .post<string[]>('/api/v1/providers/models', payload)
      .then(r => r.data),

  refreshSavedModels: (configId: string) =>
    apiClient
      .post<string[]>(`/api/v1/providers/${configId}/models`)
      .then(r => r.data),
};
