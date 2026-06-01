// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type { Connection, ConnectionDetail, PaginatedResponse, PrivacySettings } from '../types';

export interface CreateConnectionPayload {
  name: string;
  source_type: string;
  config: Record<string, unknown>;
  privacy_settings?: Partial<PrivacySettings>;
  execution_mode?: Connection['execution_mode'];
}

export const connectionsApi = {
  list: () =>
    apiClient
      .get<PaginatedResponse<Connection>>('/api/v1/connections')
      .then(r => r.data.items),

  create: (payload: CreateConnectionPayload) =>
    apiClient.post<Connection>('/api/v1/connections', payload).then(r => r.data),

  get: (id: string) =>
    apiClient.get<ConnectionDetail>(`/api/v1/connections/${id}`).then(r => r.data),

  delete: (id: string) =>
    apiClient.delete(`/api/v1/connections/${id}`).then(r => r.data),

  testNew: (sourceType: string, config: Record<string, unknown>) =>
    apiClient
      .post<{ success: boolean; message: string; server_version?: string }>(
        '/api/v1/connections/test',
        { source_type: sourceType, config },
      )
      .then(r => r.data),

  test: (id: string) =>
    apiClient
      .post<{ success: boolean; message: string; server_version?: string }>(
        `/api/v1/connections/${id}/test`,
      )
      .then(r => r.data),

  getSchema: (id: string) =>
    apiClient.get(`/api/v1/connections/${id}/schema`).then(r => r.data),

  refreshSchema: (id: string) =>
    apiClient.post(`/api/v1/connections/${id}/schema/refresh`).then(r => r.data),

  updatePrivacySettings: (id: string, settings: Partial<PrivacySettings>) =>
    apiClient
      .put<PrivacySettings>(`/api/v1/connections/${id}/privacy`, settings)
      .then(r => r.data),

  updateExecutionMode: (id: string, mode: Connection['execution_mode']) =>
    apiClient
      .put<Connection>(`/api/v1/connections/${id}/execution-mode`, { execution_mode: mode })
      .then(r => r.data),
};
