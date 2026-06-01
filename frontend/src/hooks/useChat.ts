// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { chatApi } from '../api/chat';
import { useAppStore } from '../store/appStore';
import type { ChatRequest } from '../types';

export function useSendMessage() {
  const { addMessage, setActiveSession } = useAppStore();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (payload: ChatRequest) => chatApi.sendMessage(payload),
    onSuccess: (response, payload) => {
      // Read live state to avoid stale closure race
      if (!useAppStore.getState().activeSessionId) {
        setActiveSession(response.session_id);
      }
      addMessage({
        id: response.message_id,
        role: 'assistant',
        content: response.explanation,
        query_generated: response.query,
        query_dialect: response.query_dialect,
        results_json: response.results,
        execution_time_ms: response.execution_time_ms,
        token_count: response.token_count ?? null,
        input_tokens: response.input_tokens ?? null,
        output_tokens: response.output_tokens ?? null,
        status: response.status,
        cache_hit: response.cache_hit,
        feedback: null,
        error: response.error,
        created_at: new Date().toISOString(),
      });
      // If schema was not yet cached (first query on new connection), invalidate so
      // the sidebar reflects the just-introspected schema.
      const existing = queryClient.getQueryData(['schema', payload.connection_id]);
      if (!existing) {
        queryClient.invalidateQueries({ queryKey: ['schema', payload.connection_id] });
      }
    },
  });
}

export function useExecutePending() {
  const { updateMessage } = useAppStore();

  return useMutation({
    mutationFn: (messageId: string) => chatApi.executePending(messageId),
    onSuccess: (response) => {
      updateMessage(response.message_id, {
        status: response.status,
        results_json: response.results,
        execution_time_ms: response.execution_time_ms,
        error: response.error,
      });
    },
  });
}

export function useEditAndExecute() {
  const { updateMessage } = useAppStore();

  return useMutation({
    mutationFn: ({ messageId, query }: { messageId: string; query: string }) =>
      chatApi.editAndExecute(messageId, query),
    onSuccess: (response) => {
      updateMessage(response.message_id, {
        query_generated: response.query,
        status: response.status,
        results_json: response.results,
        execution_time_ms: response.execution_time_ms,
        error: response.error,
      });
    },
  });
}

export function useSessions(connectionId: string | null) {
  return useQuery({
    queryKey: ['sessions', connectionId],
    queryFn: async () => {
      const sessions = await chatApi.getSessions();
      return sessions.filter((s) => s.connection_id === connectionId);
    },
    enabled: !!connectionId,
  });
}

export function useSessionHistory(sessionId: string | null) {
  const setMessages = useAppStore((s) => s.setMessages);

  const query = useQuery({
    queryKey: ['history', sessionId],
    queryFn: () => chatApi.getHistory(sessionId!),
    enabled: !!sessionId,
    staleTime: Infinity,
  });

  useEffect(() => {
    if (query.data) {
      setMessages(query.data);
    }
  }, [query.data, setMessages]);

  return query;
}

export function useCacheStats() {
  return useQuery({
    queryKey: ['cacheStats'],
    queryFn: chatApi.getCacheStats,
  });
}

export function useDeleteSession() {
  const queryClient = useQueryClient();
  const { activeSessionId, setActiveSession, clearMessages } = useAppStore();

  return useMutation({
    mutationFn: (sessionId: string) => chatApi.deleteSession(sessionId),
    onSuccess: (_, sessionId) => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
      if (activeSessionId === sessionId) {
        setActiveSession(null);
        clearMessages();
      }
    },
  });
}
