// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { memo, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ElementType } from 'react';
import { ThumbsUp, ThumbsDown, Zap, Copy, Check, RotateCcw, Pencil, Info, RefreshCw } from 'lucide-react';
import logoImg from '@/assets/logo.png';
import QueryHighlight from './QueryHighlight';
import QueryReviewPanel from './QueryReviewPanel';
import ResultsView from './ResultsView';
import TokenUsageBadge from './TokenUsageBadge';
import { chatApi } from '../api/chat';
import { cn } from '@/lib/utils';
import type { ChatMessage as ChatMessageType } from '../types';

interface Props {
  message: ChatMessageType;
  onRetry?: () => void;
  onEdit?: (text: string) => void;
  /** When true the message is part of an in-progress SSE stream (unused here, forwarded to ResultsView). */
  isStreaming?: boolean;
  /** The user's original NL question that prompted this assistant response. */
  userQuestion?: string;
  /** Called when the user wants to re-run bypassing cache (only provided for cache-hit messages). */
  onRefresh?: () => void;
}

function ActionButton({
  onClick,
  icon: Icon,
  label,
  title,
  active,
}: {
  onClick: () => void;
  icon: ElementType;
  label: string;
  title?: string;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title ?? label}
      className={cn(
        'rounded-md p-1.5 transition-colors',
        active
          ? 'text-foreground'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
}

async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
  } else {
    // Fallback for non-secure contexts (HTTP non-localhost)
    const el = document.createElement('textarea');
    el.value = text;
    el.style.position = 'fixed';
    el.style.opacity = '0';
    document.body.appendChild(el);
    el.focus();
    el.select();
    document.execCommand('copy'); // legacy fallback — clipboard API unavailable in HTTP contexts
    document.body.removeChild(el);
  }
}

function ChatMessageBubble({ message, onRetry, onEdit, userQuestion: _userQuestion, onRefresh }: Props) {
  const queryClient = useQueryClient();
  const [feedback, setFeedback] = useState<'positive' | 'negative' | null>(
    message.feedback === 'thumbs_up' ? 'positive'
    : message.feedback === 'thumbs_down' ? 'negative'
    : null,
  );
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
  const [sqlCopied, setSqlCopied] = useState(false);
  const [contentCopied, setContentCopied] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState(message.content);
  const sqlTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const contentTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (sqlTimerRef.current) clearTimeout(sqlTimerRef.current);
      if (contentTimerRef.current) clearTimeout(contentTimerRef.current);
    };
  }, []);

  const sendFeedback = async (f: 'positive' | 'negative') => {
    if (feedbackSubmitting || feedback) return;
    setFeedbackSubmitting(true);
    setFeedback(f);
    try {
      await chatApi.submitFeedback(message.id, f);
      if (f === 'positive') {
        queryClient.invalidateQueries({ queryKey: ['examples'] });
      }
    } catch {
      setFeedback(null);
    } finally {
      setFeedbackSubmitting(false);
    }
  };

  const copyQuery = async () => {
    if (!message.query_generated) return;
    try {
      await copyToClipboard(message.query_generated);
      setSqlCopied(true);
      if (sqlTimerRef.current) clearTimeout(sqlTimerRef.current);
      sqlTimerRef.current = setTimeout(() => setSqlCopied(false), 2000);
    } catch { /* ignore */ }
  };

  const copyContent = async () => {
    if (!message.content) return;
    try {
      await copyToClipboard(message.content);
      setContentCopied(true);
      if (contentTimerRef.current) clearTimeout(contentTimerRef.current);
      contentTimerRef.current = setTimeout(() => setContentCopied(false), 2000);
    } catch { /* ignore */ }
  };

  const handleEditSubmit = () => {
    const trimmed = editText.trim();
    if (!trimmed || !onEdit) return;
    onEdit(trimmed);
    setIsEditing(false);
  };

  const handleEditCancel = () => {
    setIsEditing(false);
    setEditText(message.content);
  };

  // ── User bubble ──────────────────────────────────────────────────────────
  if (message.role === 'user') {
    // Inline edit mode
    if (isEditing) {
      return (
        <div className="flex justify-end">
          <div className="flex w-full max-w-[70%] flex-col items-end gap-2">
            <textarea
              className="w-full rounded-2xl rounded-br-md bg-chat-user px-4 py-2.5 text-sm text-foreground resize-none focus:outline-none focus:ring-1 focus:ring-ring"
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleEditSubmit();
                }
                if (e.key === 'Escape') {
                  handleEditCancel();
                }
              }}
              rows={3}
              autoFocus
            />
            <div className="flex items-center gap-1.5">
              <button
                onClick={handleEditCancel}
                className="rounded-md px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={handleEditSubmit}
                disabled={!editText.trim()}
                className="rounded-md bg-brand-gradient px-2.5 py-1 text-xs text-white shadow-gradient-btn transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                Send
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="group flex justify-end">
        <div className="flex flex-col items-end gap-1">
          <div className="max-w-[70%] rounded-2xl rounded-br-md bg-brand-gradient px-4 py-2.5 text-sm text-white shadow-gradient-btn">
            {message.content}
          </div>
          {/* Hover-reveal action bar */}
          <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
            <ActionButton
              onClick={copyContent}
              icon={contentCopied ? Check : Copy}
              label={contentCopied ? 'Copied' : 'Copy'}
              active={contentCopied}
            />
            {onEdit && (
              <ActionButton
                onClick={() => {
                  setIsEditing(true);
                  setEditText(message.content);
                }}
                icon={Pencil}
                label="Edit"
              />
            )}
            {onRetry && (
              <ActionButton
                onClick={onRetry}
                icon={RotateCcw}
                label="Retry"
              />
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── Assistant response ───────────────────────────────────────────────────
  const successStatuses = ['executed', 'cached', 'query_only'];
  const isSuccess = successStatuses.includes(message.status);
  const showFeedback = isSuccess && message.query_generated;

  const NO_DATA_PREFIX = 'NO_DATA: ';
  const isNoData = message.error?.startsWith(NO_DATA_PREFIX) ?? false;
  const noDataMessage = isNoData ? (message.error?.slice(NO_DATA_PREFIX.length) ?? '') : null;

  return (
    <div className="flex items-start gap-3">
      {/* Assistant avatar */}
      <div className="h-5 w-5 shrink-0 overflow-hidden rounded-md border border-border">
        <img src={logoImg} alt="" className="h-full w-full object-contain" />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        {/* Identity row */}
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            savvina ai
            {message.execution_time_ms != null && (
              <> · {message.execution_time_ms}ms</>
            )}
          </span>
        </div>
        {/* Cache hit badge */}
        {message.cache_hit && (
          <div className="flex items-center gap-1.5">
            <span className="inline-flex items-center gap-1 rounded border border-badge-text/20 bg-badge-bg px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-badge-text">
              <Zap className="h-2.5 w-2.5" />
              ✓ Cached
            </span>
            {onRefresh && (
              <button
                onClick={onRefresh}
                title="Re-run bypassing cache"
                className="inline-flex items-center gap-1 rounded border border-badge-text/20 bg-badge-bg px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide text-badge-text transition-opacity hover:opacity-70"
              >
                <RefreshCw className="h-2.5 w-2.5" />
                Refresh
              </button>
            )}
          </div>
        )}

        {/* Review panel (pending_approval status) */}
        {message.status === 'pending_approval' && (
          <QueryReviewPanel message={message} />
        )}

        {/* SQL + results card (executed / cached / query_only) */}
        {message.status !== 'pending_approval' && message.query_generated && (
          <div className="overflow-hidden rounded-xl border border-border bg-surface-elevated">
            {/* Card header */}
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="rounded bg-badge-bg px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-badge-text">
                  {message.query_dialect ?? 'SQL'}
                </span>
                {message.results_json && (
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {message.results_json.row_count} row
                    {message.results_json.row_count !== 1 ? 's' : ''}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <TokenUsageBadge
                  compact
                  total={message.token_count}
                  input={message.input_tokens}
                  output={message.output_tokens}
                />
                <button
                  onClick={copyQuery}
                  className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  {sqlCopied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                  {sqlCopied ? 'Copied' : 'Copy'}
                </button>
              </div>
            </div>

            {/* SQL */}
            <QueryHighlight query={message.query_generated} dialect={null} showHeader={false} />

            {/* Results view (table/chart toggle) */}
            {message.results_json && (
              <ResultsView results={message.results_json} messageId={message.id} />
            )}
          </div>
        )}

        {/* Explanation + action buttons */}
        {message.content && message.status !== 'pending_approval' && (
          <div className="flex max-w-2xl items-start justify-between rounded-xl border border-border bg-chat-ai px-4 py-3">
            <p className="flex-1 text-sm text-muted-foreground">{message.content}</p>
            <div className="ml-3 flex shrink-0 items-center gap-1">
              <ActionButton
                onClick={copyContent}
                icon={contentCopied ? Check : Copy}
                label={contentCopied ? 'Copied' : 'Copy'}
                title="Copy response"
                active={contentCopied}
              />
              {showFeedback && (
                <>
                  <button
                    onClick={() => sendFeedback('positive')}
                    disabled={feedbackSubmitting || !!feedback}
                    title="Good answer"
                    className={cn(
                      'rounded-md p-1.5 transition-colors',
                      feedback === 'positive'
                        ? 'text-success'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40',
                    )}
                  >
                    <ThumbsUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    onClick={() => sendFeedback('negative')}
                    disabled={feedbackSubmitting || !!feedback}
                    title="Bad answer"
                    className={cn(
                      'rounded-md p-1.5 transition-colors',
                      feedback === 'negative'
                        ? 'text-destructive'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40',
                    )}
                  >
                    <ThumbsDown className="h-3.5 w-3.5" />
                  </button>
                </>
              )}
            </div>
          </div>
        )}

        {/* Info banner — schema data unavailable */}
        {isNoData && (
          <div className="flex items-start gap-3 rounded-xl border border-warning/50 bg-warning/15 px-4 py-3">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <p className="text-sm text-warning-foreground">{noDataMessage}</p>
          </div>
        )}

        {/* TPM warning — prompt was compressed due to token-per-minute limit; result may be degraded */}
        {message.warning && (
          <div className="flex items-start gap-3 rounded-xl border border-warning/50 bg-warning/15 px-4 py-3">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <p className="text-sm text-warning-foreground">{message.warning}</p>
          </div>
        )}

        {/* Error box — execution errors, connection failures, pipeline errors */}
        {!isNoData && (message.error || message.status === 'error') && (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
            <p className="text-sm text-destructive">
              {message.error ?? 'Query execution failed — please try again.'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(ChatMessageBubble);
