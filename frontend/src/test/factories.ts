// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import type { Connection, ChatMessage, ChatResponse, ChatSession, QueryResults, ProviderStatus } from '../types';

export function makeConnection(overrides?: Partial<Connection>): Connection {
  return {
    id: 'conn-1',
    name: 'Test Connection',
    source_type: 'postgresql',
    execution_mode: 'auto_execute',
    is_active: true,
    schema_cached_at: null,
    semantic_model_updated_at: null,
    created_at: '2026-01-01T00:00:00Z',
...overrides,
  };
}

export function makeQueryResults(overrides?: Partial<QueryResults>): QueryResults {
  return {
    columns: ['id', 'name'],
    column_types: ['integer', 'text'],
    rows: [
      [1, 'Alice'],
      [2, 'Bob'],
      [3, 'Carol'],
    ],
    row_count: 3,
    truncated: false,
    bytes_scanned: null,
    ...overrides,
  };
}

export function makeChatMessage(overrides?: Partial<ChatMessage>): ChatMessage {
  return {
    id: 'msg-1',
    role: 'assistant',
    content: 'Here is the result.',
    query_generated: 'SELECT 1',
    query_dialect: 'postgresql',
    results_json: null,
    execution_time_ms: null,
    token_count: null,
    input_tokens: null,
    output_tokens: null,
    status: 'executed',
    cache_hit: false,
    feedback: null,
    error: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

export function makeChatResponse(overrides?: Partial<ChatResponse>): ChatResponse {
  return {
    session_id: 'session-1',
    message_id: 'msg-1',
    query: 'SELECT 1',
    query_dialect: 'postgresql',
    explanation: 'This returns 1.',
    results: makeQueryResults(),
    execution_time_ms: 42,
    token_count: null,
    input_tokens: null,
    output_tokens: null,
    status: 'executed',
    cache_hit: false,
    error: null,
    ...overrides,
  };
}

export function makeChatSession(overrides?: Partial<ChatSession>): ChatSession {
  return {
    id: 'session-1',
    connection_id: 'conn-1',
    title: 'Test Session',
    provider: 'claude',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    cache_hit_count: 0,
    ...overrides,
  };
}

export function makeProviderStatus(overrides?: Partial<ProviderStatus>): ProviderStatus {
  return {
    id: 'provider-1',
    provider_type: 'claude',
    display_name: 'Claude',
    provider_display_name: 'Anthropic Claude',
    is_active: true,
    is_configured: true,
    is_healthy: true,
    current_model: 'claude-sonnet-4-5-20250929',
    available_models: ['claude-sonnet-4-5-20250929', 'claude-haiku-4-5-20251001'],
    base_url: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}
