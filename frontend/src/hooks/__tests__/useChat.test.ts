// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { http, HttpResponse } from 'msw';
import { server } from '../../test/server';
import { makeChatResponse } from '../../test/factories';
import { useAppStore } from '../../store/appStore';
import {
  useSendMessage,
  useExecutePending,
  useEditAndExecute,
  useDeleteSession,
} from '../useChat';
import type { ChatRequest } from '../../types';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: 0 },
      mutations: { retry: 0 },
    },
  });
  return {
    queryClient,
    wrapper: ({ children }: { children: React.ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  };
}

const resetStore = () =>
  useAppStore.setState({
    activeConnectionId: null,
    activeSessionId: null,
    selectedProvider: '',
    schema: null,
    messages: [],
  });

const sendPayload: ChatRequest = {
  connection_id: 'conn-1',
  session_id: null,
  message: 'Top 5 customers',
  provider: 'claude',
  options: { show_query: true, max_rows: 100, explain_results: false },
};

describe('useSendMessage', () => {
  beforeEach(resetStore);

  it('on success: adds assistant message to store', async () => {
    const response = makeChatResponse({ message_id: 'msg-99', session_id: 'sess-1' });
    server.use(
      http.post('http://localhost:8000/api/v1/chat', () => HttpResponse.json(response)),
    );
    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useSendMessage(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(sendPayload);
    });

    const messages = useAppStore.getState().messages;
    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe('msg-99');
    expect(messages[0].role).toBe('assistant');
  });

  it('on success: sets activeSessionId if currently null', async () => {
    const response = makeChatResponse({ session_id: 'sess-new' });
    server.use(
      http.post('http://localhost:8000/api/v1/chat', () => HttpResponse.json(response)),
    );
    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useSendMessage(), { wrapper });

    expect(useAppStore.getState().activeSessionId).toBeNull();

    await act(async () => {
      await result.current.mutateAsync(sendPayload);
    });

    expect(useAppStore.getState().activeSessionId).toBe('sess-new');
  });

  it('on success: does NOT call setActiveSession if session already set', async () => {
    useAppStore.setState({ activeSessionId: 'sess-existing' });
    const response = makeChatResponse({ session_id: 'sess-new' });
    server.use(
      http.post('http://localhost:8000/api/v1/chat', () => HttpResponse.json(response)),
    );
    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useSendMessage(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync(sendPayload);
    });

    // Session ID should remain unchanged
    expect(useAppStore.getState().activeSessionId).toBe('sess-existing');
  });
});

describe('useExecutePending', () => {
  beforeEach(resetStore);

  it('on success: calls updateMessage with new status and results', async () => {
    const response = makeChatResponse({
      message_id: 'msg-pending',
      status: 'executed',
      results: { columns: ['n'], column_types: ['int'], rows: [[1]], row_count: 1, truncated: false, bytes_scanned: null },
      execution_time_ms: 77,
    });
    server.use(
      http.post('http://localhost:8000/api/v1/chat/execute/:id', () =>
        HttpResponse.json(response),
      ),
    );
    // Seed store with pending message
    useAppStore.setState({
      messages: [
        {
          id: 'msg-pending',
          role: 'assistant',
          content: 'explain',
          query_generated: 'SELECT 1',
          query_dialect: 'postgresql',
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'pending_approval',
          cache_hit: false,
          feedback: null,
          error: null,
          created_at: new Date().toISOString(),
        },
      ],
    });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useExecutePending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('msg-pending');
    });

    const updated = useAppStore.getState().messages.find((m) => m.id === 'msg-pending');
    expect(updated?.status).toBe('executed');
    expect(updated?.execution_time_ms).toBe(77);
    expect(updated?.results_json).not.toBeNull();
  });

  it('on success: propagates error field from response', async () => {
    const response = makeChatResponse({
      message_id: 'msg-err',
      status: 'error',
      error: 'Syntax error',
      results: null,
    });
    server.use(
      http.post('http://localhost:8000/api/v1/chat/execute/:id', () =>
        HttpResponse.json(response),
      ),
    );
    useAppStore.setState({
      messages: [
        {
          id: 'msg-err',
          role: 'assistant',
          content: '',
          query_generated: 'SELECT bad',
          query_dialect: 'postgresql',
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'pending_approval',
          cache_hit: false,
          feedback: null,
          error: null,
          created_at: new Date().toISOString(),
        },
      ],
    });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useExecutePending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('msg-err');
    });

    const updated = useAppStore.getState().messages.find((m) => m.id === 'msg-err');
    expect(updated?.error).toBe('Syntax error');
  });
});

describe('useEditAndExecute', () => {
  beforeEach(resetStore);

  it('on success: calls updateMessage with updated query and results', async () => {
    const editedQuery = 'SELECT id FROM users LIMIT 5';
    const response = makeChatResponse({
      message_id: 'msg-edit',
      query: editedQuery,
      status: 'executed',
    });
    server.use(
      http.post('http://localhost:8000/api/v1/chat/edit/:id', () =>
        HttpResponse.json(response),
      ),
    );
    useAppStore.setState({
      messages: [
        {
          id: 'msg-edit',
          role: 'assistant',
          content: '',
          query_generated: 'SELECT * FROM users',
          query_dialect: 'postgresql',
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'pending_approval',
          cache_hit: false,
          feedback: null,
          error: null,
          created_at: new Date().toISOString(),
        },
      ],
    });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useEditAndExecute(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ messageId: 'msg-edit', query: editedQuery });
    });

    const updated = useAppStore.getState().messages.find((m) => m.id === 'msg-edit');
    expect(updated?.query_generated).toBe(editedQuery);
    expect(updated?.status).toBe('executed');
    expect(updated?.results_json).not.toBeNull();
  });
});

describe('useDeleteSession', () => {
  beforeEach(() => {
    resetStore();
  });

  it('on success: invalidates ["sessions"] query', async () => {
    const { wrapper, queryClient } = createWrapper();
    // Seed the query cache
    queryClient.setQueryData(['sessions'], []);

    const { result } = renderHook(() => useDeleteSession(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('session-other');
    });

    await waitFor(() => {
      const state = queryClient.getQueryState(['sessions']);
      expect(state?.isInvalidated).toBe(true);
    });
  });

  it('on success: calls setActiveSession(null) and clearMessages() when deleting active session', async () => {
    useAppStore.setState({
      activeSessionId: 'session-active',
      messages: [
        {
          id: 'msg-1',
          role: 'user',
          content: 'hello',
          query_generated: null,
          query_dialect: null,
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'executed',
          cache_hit: false,
          feedback: null,
          error: null,
          created_at: new Date().toISOString(),
        },
      ],
    });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useDeleteSession(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('session-active');
    });

    expect(useAppStore.getState().activeSessionId).toBeNull();
    expect(useAppStore.getState().messages).toHaveLength(0);
  });

  it('on success: does NOT clear store when deleting a different session', async () => {
    useAppStore.setState({
      activeSessionId: 'session-keep',
      messages: [
        {
          id: 'msg-1',
          role: 'user',
          content: 'hello',
          query_generated: null,
          query_dialect: null,
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'executed',
          cache_hit: false,
          feedback: null,
          error: null,
          created_at: new Date().toISOString(),
        },
      ],
    });

    const { wrapper } = createWrapper();
    const { result } = renderHook(() => useDeleteSession(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync('session-other');
    });

    expect(useAppStore.getState().activeSessionId).toBe('session-keep');
    expect(useAppStore.getState().messages).toHaveLength(1);
  });
});
