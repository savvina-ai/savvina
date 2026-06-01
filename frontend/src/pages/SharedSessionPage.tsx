// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Public shared session page — renders a full conversation thread without authentication. */

import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Zap } from 'lucide-react';

import type { PublicMessageSummary, PublicSessionResult } from '../api/share';
import { shareApi } from '../api/share';
import { downloadCsv } from '../lib/exportUtils';
import QueryHighlight from '../components/QueryHighlight';
import ResultsTable from '../components/ResultsTable';

function SharedMessage({ msg, idx }: { msg: PublicMessageSummary; idx: number }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[70%] rounded-2xl rounded-br-md bg-chat-user px-4 py-2.5 text-sm text-foreground">
          {msg.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary">
        <span className="font-display text-[11px] font-bold text-primary-foreground">S</span>
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        {msg.status === 'cached' && (
          <span className="inline-flex items-center gap-1 rounded-full border border-badge-text/20 bg-badge-bg px-2 py-0.5 text-xs text-badge-text">
            <Zap className="h-3 w-3" />
            Cached
          </span>
        )}

        {msg.query_generated && (
          <div className="overflow-hidden rounded-xl border border-border bg-surface-elevated">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="rounded bg-badge-bg px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase text-badge-text">
                  {msg.query_dialect ?? 'SQL'}
                </span>
                {msg.results_json && (
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {msg.results_json.row_count} row{msg.results_json.row_count !== 1 ? 's' : ''}
                  </span>
                )}
              </div>
              {msg.execution_time_ms != null && (
                <span className="font-mono text-[10px] text-muted-foreground">
                  {msg.execution_time_ms}ms
                </span>
              )}
            </div>

            <QueryHighlight query={msg.query_generated} dialect={null} showHeader={false} />

            {msg.results_json && (
              <div>
                <div className="border-t border-border">
                  <ResultsTable results={msg.results_json} />
                </div>
                <div className="flex items-center gap-1 border-t border-border px-3 py-1.5">
                  <button
                    onClick={() => downloadCsv(msg.results_json!, `result-${idx}.csv`)}
                    className="rounded px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  >
                    Download CSV
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {msg.content && (
          <div className="max-w-2xl rounded-xl border border-border bg-chat-ai px-4 py-3">
            <p className="text-sm text-muted-foreground">{msg.content}</p>
          </div>
        )}
      </div>
    </div>
  );
}

export default function SharedSessionPage() {
  const { token } = useParams<{ token: string }>();
  const [session, setSession] = useState<PublicSessionResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    shareApi
      .getSharedSession(token)
      .then((res) => setSession(res.data))
      .catch(() => setError('This shared session is unavailable or has expired.'))
      .finally(() => setLoading(false));
  }, [token]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border px-6 py-3 flex items-center justify-between">
        <span className="savvina-grad-text font-display text-base font-semibold">savvina ai</span>
        <span className="eyebrow">Shared session</span>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-8">
        {loading && (
          <div className="flex items-center justify-center py-20">
            <span className="text-sm text-muted-foreground">Loading…</span>
          </div>
        )}

        {error && (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-6 text-center">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        {session && (
          <>
            <h1 className="mb-6 text-lg font-semibold text-foreground">{session.title}</h1>
            <div className="space-y-4">
              {session.messages.map((msg, idx) => (
                <SharedMessage key={idx} msg={msg} idx={idx} />
              ))}
            </div>
          </>
        )}
      </main>

      <footer className="border-t border-border px-6 py-3 text-center">
        <span className="text-xs text-muted-foreground">
          Powered by <span className="savvina-grad-text font-semibold">savvina ai</span>
        </span>
      </footer>
    </div>
  );
}
