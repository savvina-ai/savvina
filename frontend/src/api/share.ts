// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Share API — authenticated share-token creation and public result fetch. */

import axios from 'axios';

import apiClient from './client';
import type { QueryResults } from '../types';

export interface ShareMessageResponse {
  share_token: string;
}

export interface PublicShareResult {
  results: QueryResults;
  query_generated: string | null;
}

export interface PublicMessageSummary {
  role: string;
  content: string;
  query_generated: string | null;
  query_dialect: string | null;
  results_json: QueryResults | null;
  execution_time_ms: number | null;
  status: string;
  created_at: string;
}

export interface PublicSessionResult {
  title: string;
  messages: PublicMessageSummary[];
}

const publicBase = typeof window !== 'undefined' ? window.location.origin : '';

export const shareApi = {
  /** Create (or retrieve) a public share token for an executed message. */
  shareMessage: (messageId: string) =>
    apiClient.post<ShareMessageResponse>(`/api/v1/chat/messages/${messageId}/share`),

  /** Fetch shared results — unauthenticated, uses a plain axios instance. */
  getSharedResult: (token: string) =>
    axios.get<PublicShareResult>(`${publicBase}/api/v1/public/share/${token}`),

  /** Create (or retrieve) a public share token for an entire session. */
  shareSession: (sessionId: string) =>
    apiClient.post<ShareMessageResponse>(`/api/v1/chat/sessions/${sessionId}/share`),

  /** Fetch a shared session thread — unauthenticated. */
  getSharedSession: (token: string) =>
    axios.get<PublicSessionResult>(`${publicBase}/api/v1/public/share/session/${token}`),
};
