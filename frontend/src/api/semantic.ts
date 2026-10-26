// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type {
  ApplyDriftResponse,
  DriftApplyHistoryEntry,
  DriftReport,
  PaginatedResponse,
  SemanticModel,
  SemanticSuggestionResponse,
} from '../types';

export interface GenerateInitResult {
  connection_id: string;
  tables_total: number;
  batch_count: number;
  batch_size: number;
  pending_table_keys: string[];
  resumed: boolean;
}

/**
 * What `/generate/batch` answers with: the run's progress, never the model. The page
 * drives its own progress bar from the keys it dispatched and reads the finished model
 * from `/generate/globals`.
 */
export interface GenerateBatchResult {
  connection_id: string;
  table_keys: string[];
  tables_done: number;
  tables_total: number;
  batch_size: number;
  /** Keys of this batch the LLM returned nothing for; still pending on the server. */
  omitted_table_keys: string[];
}

export const semanticApi = {
  get: (connectionId: string) =>
    apiClient.get<SemanticModel>(`/api/v1/connections/${connectionId}/semantic`).then(r => r.data),

  generateInit: (connectionId: string, provider?: string, resume = false) =>
    apiClient
      .post<GenerateInitResult>(
        `/api/v1/connections/${connectionId}/semantic/generate/init`,
        undefined,
        { params: { ...(provider ? { provider } : {}), ...(resume ? { resume: true } : {}) } },
      )
      .then(r => r.data),

  generateBatch: (connectionId: string, tableKeys: string[], provider?: string) =>
    apiClient
      .post<GenerateBatchResult>(
        `/api/v1/connections/${connectionId}/semantic/generate/batch`,
        { table_keys: tableKeys },
        provider ? { params: { provider } } : undefined,
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

  /**
   * 404s when the connection or its semantic model does not exist. 400 when the data
   * source is unreachable and no schema has been cached yet. 409 when the connection was
   * deleted during the check; 503 (with `Retry-After`) when the check waited on a save in
   * progress past the database's lock timeout. 429 when rate limited (5/minute: every
   * check runs a live introspection).
   */
  getDrift: (connectionId: string) =>
    apiClient
      .get<DriftReport>(`/api/v1/connections/${connectionId}/semantic/drift`)
      .then(r => r.data),

  /**
   * Applies what `getDrift` reported, or with `dryRun` previews it: same summary, nothing
   * written but the refreshed schema cache, no LLM call. 404 when the connection or its
   * semantic model does not exist. 400 when the database is unreachable, when the provider
   * cannot be resolved for new tables or for an apply whose only changes are new columns,
   * or when the store rejects the result of the apply. 409 during a run, or when the
   * connection or model was deleted meanwhile. 429 when rate limited (5/minute, a preview
   * and its apply are two calls). 503 when the call waited on a save past the lock
   * timeout. A failed new-table (or globals) LLM call answers with its own error: the
   * provider's 429 or 503, 504 on a timeout, 422 or 500. An apply whose only changes are
   * new columns, and no batch has answered yet, fails with the provider's 429 or 503 too;
   * any other failed new-column batch is a warning in the summary.
   */
  applyDrift: (connectionId: string, provider?: string, dryRun = false) =>
    apiClient
      .post<ApplyDriftResponse>(
        `/api/v1/connections/${connectionId}/semantic/apply-drift`,
        undefined,
        { params: { ...(provider ? { provider } : {}), ...(dryRun ? { dry_run: true } : {}) } },
      )
      .then(r => r.data),

  /** Logged Apply Drifts, newest first. */
  getDriftHistory: (connectionId: string, limit = 10) =>
    apiClient
      .get<DriftApplyHistoryEntry[]>(
        `/api/v1/connections/${connectionId}/semantic/drift/history`,
        { params: { limit } },
      )
      .then(r => r.data),

  getSuggestions: (connectionId: string) =>
    apiClient
      .get<PaginatedResponse<SemanticSuggestionResponse>>(
        `/api/v1/connections/${connectionId}/semantic/suggestions`,
      )
      .then(r => r.data.items),

  applySuggestion: (connectionId: string, suggestionId: string) =>
    apiClient
      .post<SemanticModel>(
        `/api/v1/connections/${connectionId}/semantic/suggestions/${suggestionId}/apply`,
      )
      .then(r => r.data),
};
