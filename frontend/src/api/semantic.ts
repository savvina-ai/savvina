// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type { SemanticModel } from '../types';

interface GenerateInitResult {
  connection_id: string;
  tables_total: number;
  batch_count: number;
  batch_size: number;
}

export const semanticApi = {
  get: (connectionId: string) =>
    apiClient.get<SemanticModel>(`/api/v1/connections/${connectionId}/semantic`).then(r => r.data),

  generateInit: (connectionId: string, provider?: string) =>
    apiClient
      .post<GenerateInitResult>(
        `/api/v1/connections/${connectionId}/semantic/generate/init`,
        undefined,
        provider ? { params: { provider } } : undefined,
      )
      .then(r => r.data),

  generateBatch: (connectionId: string, batchIdx: number, provider?: string) =>
    apiClient
      .post<SemanticModel>(
        `/api/v1/connections/${connectionId}/semantic/generate/batch`,
        undefined,
        { params: { batch_idx: batchIdx, ...(provider ? { provider } : {}) } },
      )
      .then(r => r.data),

  generateGlobals: (connectionId: string, provider?: string) =>
    apiClient
      .post<SemanticModel>(
        `/api/v1/connections/${connectionId}/semantic/generate/globals`,
        undefined,
        provider ? { params: { provider } } : undefined,
      )
      .then(r => r.data),

  update: (connectionId: string, model: Partial<SemanticModel>) =>
    apiClient
      .put<SemanticModel>(`/api/v1/connections/${connectionId}/semantic`, model)
      .then(r => r.data),

  delete: (connectionId: string) =>
    apiClient.delete(`/api/v1/connections/${connectionId}/semantic`).then(r => r.data),
};
