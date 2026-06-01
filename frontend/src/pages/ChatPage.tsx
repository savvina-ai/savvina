// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Share2, Check, PanelRight } from 'lucide-react';
import WorkspacePanel from '../components/WorkspacePanel';
import logoImg from '@/assets/logo.png';
import { useQueryClient } from '@tanstack/react-query';
import ChatMessageBubble from '../components/ChatMessage';
import ChatInput from '../components/ChatInput';
import ProviderSelector from '../components/ProviderSelector';
import ExecutionModeSelector from '../components/ExecutionModeSelector';
import QueryHighlight from '../components/QueryHighlight';
import ResultsView from '../components/ResultsView';
import StreamingProgress from '../components/StreamingProgress';
import TokenUsageBadge from '../components/TokenUsageBadge';
import { useAppStore } from '../store/appStore';
import { useSessionHistory } from '../hooks/useChat';
import { useStreamChat } from '../hooks/useStreamChat';
import { useConnections, useUpdateExecutionMode } from '../hooks/useConnections';
import { shareApi } from '../api/share';
import type { QueryResults } from '../types';

export default function ChatPage() {
  const navigate = useNavigate();
  const {
    activeConnectionId,
    activeSessionId,
    messages,
    selectedProvider,
    setSelectedProvider,
    clearMessages,
    setActiveSession,
  } = useAppStore();

  const { data: connections } = useConnections();
  const activeConn = connections?.find((c) => c.id === activeConnectionId);

  const updateExecMode = useUpdateExecutionMode();
  const queryClient = useQueryClient();
  const bottomRef = useRef<HTMLDivElement>(null);
  const [inputText, setInputText] = useState('');

  const { streamingState, send, stop } = useStreamChat();

  // Invalidate schema query cache once the stream completes (matches old behaviour)
  const wasStreaming = useRef(false);
  useEffect(() => {
    if (wasStreaming.current && !streamingState.isStreaming) {
      const connId = useAppStore.getState().activeConnectionId;
      if (connId && !queryClient.getQueryData(['schema', connId])) {
        queryClient.invalidateQueries({ queryKey: ['schema', connId] });
      }
    }
    wasStreaming.current = streamingState.isStreaming;
  }, [streamingState.isStreaming, queryClient]);

  // Load history when resuming an existing session
  const isResumingSession = !!activeSessionId && messages.length === 0;
  useSessionHistory(isResumingSession ? activeSessionId : null);

  useEffect(() => {
    return () => { stop(); };
  }, [stop]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingState.rowCount, streamingState.sql]);

  useEffect(() => {
    if (!activeConnectionId) {
      navigate('/connect');
    }
  }, [activeConnectionId, navigate]);

  const handleSend = useCallback(async (text: string, bypassCache = false, forceRefresh = false) => {
    const store = useAppStore.getState();
    if (!store.activeConnectionId || !store.selectedProvider || streamingState.isStreaming) return;

    store.addMessage({
      id: `user-${Date.now()}`,
      role: 'user',
      content: text,
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
    });

    await send({
      connection_id: store.activeConnectionId,
      session_id: store.activeSessionId,
      message: text,
      provider: store.selectedProvider,
      options: { show_query: true, max_rows: 100, explain_results: true, bypass_cache: bypassCache, force_refresh: forceRefresh },
    });
  }, [send, streamingState.isStreaming]);

  const handleRetry = useCallback(
    (userMsg: string): void => { void handleSend(userMsg).catch(console.error); },
    [handleSend],
  );

  const handleStop = () => { stop(); };

  const handleNewChat = () => {
    clearMessages();
    setActiveSession(null);
  };

  const [workspaceOpen, setWorkspaceOpen] = useState(false);

  const [shareState, setShareState] = useState<'idle' | 'loading' | 'copied'>('idle');
  const [shareError, setShareError] = useState<string | null>(null);

  const handleShareSession = async () => {
    const sid = useAppStore.getState().activeSessionId;
    if (!sid || shareState !== 'idle') return;
    setShareState('loading');
    setShareError(null);
    try {
      const res = await shareApi.shareSession(sid);
      const url = `${window.location.origin}/share/session/${res.data.share_token}`;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        const el = document.createElement('textarea');
        el.value = url;
        el.style.position = 'fixed';
        el.style.opacity = '0';
        document.body.appendChild(el);
        el.focus();
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      }
      setShareState('copied');
      setTimeout(() => setShareState('idle'), 2500);
    } catch {
      setShareState('idle');
      setShareError('Failed to copy share link — please try again');
      setTimeout(() => setShareError(null), 4000);
    }
  };

  const handleExecModeChange = (mode: 'auto_execute' | 'review_first' | 'generate_only') => {
    if (!activeConnectionId) return;
    updateExecMode.mutate({ id: activeConnectionId, mode });
  };

  // Session token totals — sum across all assistant messages.
  // Prefer split (input/output) when available; fall back to the legacy total.
  const sessionTokens = messages
    .filter((m) => m.role === 'assistant')
    .reduce(
      (acc, m) => {
        const input = m.input_tokens;
        const output = m.output_tokens;
        if (input != null || output != null) {
          acc.input += input ?? 0;
          acc.output += output ?? 0;
          acc.total += (input ?? 0) + (output ?? 0);
        } else if (m.token_count != null) {
          acc.total += m.token_count;
        }
        return acc;
      },
      { total: 0, input: 0, output: 0 },
    );

  // Build partial QueryResults object for the live streaming preview
  const streamingResults: QueryResults | null =
    streamingState.columns.length > 0
      ? {
          columns: streamingState.columns,
          column_types: streamingState.columnTypes,
          rows: streamingState.rows,
          row_count: streamingState.rowCount,
          truncated: streamingState.truncated,
          bytes_scanned: null,
        }
      : null;

  if (!activeConnectionId) return null;

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-4">
        <div className="flex items-center gap-2">
          <ProviderSelector value={selectedProvider} onChange={setSelectedProvider} />
          <ExecutionModeSelector
            value={activeConn?.execution_mode ?? 'auto_execute'}
            onChange={handleExecModeChange}
          />
        </div>
        <div className="flex items-center gap-3">
          {activeSessionId && messages.length > 0 && (
            <div className="flex flex-col items-end gap-0.5">
              <button
                onClick={handleShareSession}
                disabled={shareState === 'loading'}
                className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors ${
                  shareState === 'copied'
                    ? 'text-success'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                }`}
              >
                {shareState === 'copied' ? <Check className="h-3.5 w-3.5" /> : <Share2 className="h-3.5 w-3.5" />}
                {shareState === 'copied' ? 'Link Copied' : 'Share Session'}
              </button>
              {shareError && (
                <p className="text-[10px] text-destructive">{shareError}</p>
              )}
            </div>
          )}
          {sessionTokens.total > 0 && (
            <TokenUsageBadge
              total={sessionTokens.total}
              input={sessionTokens.input > 0 ? sessionTokens.input : null}
              output={sessionTokens.output > 0 ? sessionTokens.output : null}
            />
          )}
          <button
            onClick={handleNewChat}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5" />
            New Chat
          </button>
          <button
            onClick={() => setWorkspaceOpen((o) => !o)}
            className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <PanelRight className="h-3.5 w-3.5" />
            {activeConn ? (
              <>
                <span className="font-mono">{activeConn.name}</span>
                <span className="text-muted-foreground/50">·</span>
                <span className="text-muted-foreground/70">{activeConn.source_type}</span>
              </>
            ) : (
              'Workspace'
            )}
          </button>
        </div>
      </div>


      {/* Content row: chat + workspace panel */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-6">
        {messages.length === 0 && !streamingState.isStreaming ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <h2 className="mb-3 font-display text-3xl font-bold tracking-tight text-foreground">
              Ask your data a question.
            </h2>
            <p className="max-w-sm text-sm leading-relaxed text-muted-foreground">
              Type a question in plain English and{' '}
              {activeConn && (
                <span className="font-mono text-xs font-medium text-foreground">{activeConn.name}</span>
              )}{' '}
              will write the query, run it, and return the results.
            </p>
          </div>
        ) : (
        <div className="mx-auto max-w-4xl space-y-4">
          {messages.map((msg, idx) => (
              <ChatMessageBubble
                key={msg.id}
                message={msg}
                onRetry={
                  msg.role === 'user' &&
                  messages[idx + 1]?.role === 'assistant' &&
                  messages[idx + 1]?.status === 'error'
                    ? () => handleRetry(msg.content)
                    : undefined
                }
                onEdit={msg.role === 'user' ? handleSend : undefined}
                userQuestion={
                  msg.role === 'assistant' && idx > 0 && messages[idx - 1]?.role === 'user'
                    ? messages[idx - 1].content
                    : undefined
                }
                onRefresh={
                  msg.role === 'assistant' && msg.cache_hit && idx > 0 && messages[idx - 1]?.role === 'user'
                    ? () => { void handleSend(messages[idx - 1].content, false, true); }
                    : undefined
                }
              />
            ))}

          {/* ── Streaming in-progress preview ── */}
          {streamingState.isStreaming && (
            <div className="flex items-start gap-3">
              <div className="h-7 w-7 shrink-0 overflow-hidden rounded-lg border border-border">
                <img src={logoImg} alt="" className="h-full w-full object-contain" />
              </div>
              <div className="min-w-0 flex-1 space-y-3">
                {/* Pipeline progress bar — hidden once an error arrives */}
                {!streamingState.error && (
                  <StreamingProgress
                    statusText={streamingState.statusText}
                    hasSql={!!streamingState.sql}
                    rowCount={streamingState.rowCount}
                    className="max-w-sm"
                  />
                )}

                {/* Inline error — shown immediately when the error event arrives,
                    before the done event clears isStreaming */}
                {streamingState.error && (
                  <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
                    <p className="text-sm text-destructive">{streamingState.error}</p>
                  </div>
                )}

                {/* SQL block — appears as soon as the sql event arrives */}
                {streamingState.sql && (
                  <div className="overflow-hidden rounded-xl border border-border bg-card">
                    <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                      <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-muted-foreground">
                        {streamingState.dialect ?? 'SQL'}
                      </span>
                      {streamingState.rowCount > 0 && (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {streamingState.rowCount} row{streamingState.rowCount !== 1 ? 's' : ''} so far…
                        </span>
                      )}
                    </div>
                    <QueryHighlight
                      query={streamingState.sql}
                      dialect={streamingState.dialect}
                      showHeader={false}
                    />
                    {streamingResults && (
                      <ResultsView
                        results={streamingResults}
                        messageId="streaming"
                        isStreaming={true}
                      />
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      )}
      </div>

      {/* No-provider banner */}
      {!selectedProvider && (
        <p className="px-4 pb-1 text-center text-xs text-muted-foreground">
          No LLM provider configured.{' '}
          <button
            onClick={() => navigate('/settings')}
            className="underline text-primary hover:opacity-80"
          >
            Go to Settings
          </button>{' '}
          to add one.
        </p>
      )}

      {/* Input */}
      <ChatInput
        value={inputText}
        onChange={setInputText}
        onSend={handleSend}
        onStop={handleStop}
        disabled={streamingState.isStreaming || !selectedProvider}
        placeholder={
          !selectedProvider
            ? 'Configure an LLM provider in Settings first…'
            : 'Ask about your data...'
        }
      />
      </div>{/* end chat column */}
      <WorkspacePanel open={workspaceOpen} onClose={() => setWorkspaceOpen(false)} />
      </div>{/* end content row */}
    </div>
  );
}
