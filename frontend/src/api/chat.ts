// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type {
  CacheEntry,
  CacheStats,
  ChatMessage,
  ChatRequest,
  ChatResponse,
  ChatSession,
  PaginatedResponse,
  QueryResults,
  VerifiedExample,
} from '../types';

export const chatApi = {
  sendMessage: (payload: ChatRequest) =>
    apiClient.post<ChatResponse>('/api/v1/chat', payload).then(r => r.data),

  executePending: (messageId: string) =>
    apiClient.post<ChatResponse>(`/api/v1/chat/execute/${messageId}`).then(r => r.data),

  editAndExecute: (messageId: string, query: string) =>
    apiClient
      .post<ChatResponse>(`/api/v1/chat/edit/${messageId}`, {
        message_id: messageId,
        edited_query: query,
      })
      .then(r => r.data),

  sortResults: (messageId: string, sortColumn: string, sortOrder: 'ASC' | 'DESC') =>
    apiClient
      .post<QueryResults>(`/api/v1/chat/sort/${messageId}`, {
        sort_column: sortColumn,
        sort_order: sortOrder,
      })
      .then(r => r.data),

  submitFeedback: (messageId: string, feedback: 'positive' | 'negative') =>
    apiClient
      .post(`/api/v1/chat/feedback/${messageId}`, {
        message_id: messageId,
        feedback: feedback === 'positive' ? 'thumbs_up' : 'thumbs_down',
      })
      .then(r => r.data),

  retractFeedback: (messageId: string) =>
    apiClient
      .delete(`/api/v1/chat/feedback/${messageId}`)
      .then(r => r.data),

  getSessions: () =>
    apiClient
      .get<PaginatedResponse<ChatSession>>('/api/v1/chat/sessions')
      .then(r => r.data.items),

  getHistory: (sessionId: string) =>
    apiClient
      .get<PaginatedResponse<ChatMessage>>(`/api/v1/chat/sessions/${sessionId}/history`)
      .then(r => r.data.items),

  deleteSession: (sessionId: string) =>
    apiClient.delete(`/api/v1/chat/sessions/${sessionId}`).then(r => r.data),

  getCacheStats: () =>
    apiClient.get<CacheStats>('/api/v1/chat/cache/stats').then(r => r.data),

  clearCache: (connectionId: string) =>
    apiClient.delete(`/api/v1/chat/cache/${connectionId}`).then(r => r.data),

  getCacheEntries: (connectionId: string, limit: number, offset: number, search?: string) =>
    apiClient
      .get<PaginatedResponse<CacheEntry>>(`/api/v1/chat/cache/${connectionId}/entries`, {
        params: { limit, offset, ...(search ? { search } : {}) },
      })
      .then(r => r.data),

  deleteCacheEntry: (entryId: string) =>
    apiClient.delete(`/api/v1/chat/cache/entries/${entryId}`).then(r => r.data),

  getExamples: (connectionId: string) =>
    apiClient
      .get<PaginatedResponse<VerifiedExample>>(`/api/v1/chat/examples/${connectionId}`)
      .then(r => r.data.items),

  addExample: (connectionId: string, question: string, query: string) =>
    apiClient
      .post<VerifiedExample>(`/api/v1/chat/examples/${connectionId}`, { question, query })
      .then(r => r.data),

  deleteExample: (exampleId: string) =>
    apiClient.delete(`/api/v1/chat/examples/${exampleId}`).then(r => r.data),

  updateExample: (exampleId: string, question: string, query: string) =>
    apiClient
      .put<VerifiedExample>(`/api/v1/chat/examples/${exampleId}`, { question, query })
      .then(r => r.data),
};
