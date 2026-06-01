// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/appStore';
import { useSessions, useDeleteSession } from '../hooks/useChat';
import { Button } from '../components/ui/button';
import { useConnections } from '../hooks/useConnections';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog';

export default function HistoryPage() {
  const navigate = useNavigate();
  const { activeConnectionId, setActiveSession, setActiveConnection } = useAppStore();
  const { data: connections } = useConnections();
  const [viewConnectionId, setViewConnectionId] = useState<string | null>(activeConnectionId);

  useEffect(() => {
    setViewConnectionId(activeConnectionId);
  }, [activeConnectionId]);
  const { data: sessions, isLoading } = useSessions(viewConnectionId);
  const deleteSession = useDeleteSession();
  const [search, setSearch] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const filtered = sessions?.filter((s) =>
    s.title.toLowerCase().includes(search.toLowerCase()),
  ) ?? [];

  const handleResume = (sessionId: string, connectionId: string) => {
    setActiveConnection(connectionId);
    setActiveSession(sessionId);
    navigate('/chat');
  };

  const connName = (id: string) =>
    connections?.find((c) => c.id === id)?.name ?? id;

  return (
    <div className="flex-1 overflow-auto">
      <div className="max-w-3xl mx-auto px-4 py-8">
        <h1 className="text-2xl font-bold text-foreground mb-6">Chat History</h1>

        {connections && connections.length > 0 && (
          <select
            aria-label="Filter by connection"
            value={viewConnectionId ?? ''}
            onChange={(e) => setViewConnectionId(e.target.value || null)}
            className="w-full mb-4 px-3 py-2 border border-input rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-ring bg-background"
          >
            <option value="">Select a connection…</option>
            {connections.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        )}

        <input
          type="text"
          aria-label="Search sessions"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search sessions…"
          className="w-full mb-4 px-4 py-2 border border-input rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />

        {!viewConnectionId && (
          <p className="text-sm text-muted-foreground">Select a connection to view its history.</p>
        )}

        {isLoading && (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-16 bg-muted rounded-lg animate-pulse" />
            ))}
          </div>
        )}

        {!viewConnectionId && !connections?.length && (
          <p className="text-sm text-muted-foreground text-center py-8">
            No connections yet.{' '}
            <a href="/connect" className="text-primary underline hover:opacity-80">
              Add a connection
            </a>{' '}
            to start chatting.
          </p>
        )}

        {!isLoading && filtered.length === 0 && viewConnectionId && (
          <p className="text-sm text-muted-foreground text-center py-8">No sessions found.</p>
        )}

        <div className="space-y-2">
          {filtered.map((session) => {
            const cacheHits = session.cache_hit_count;
            return (
              <div
                key={session.id}
                className="flex items-center justify-between px-4 py-3 border border-border rounded-lg bg-card hover:bg-muted/50"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-foreground truncate">{session.title}</p>
                    {cacheHits > 0 && (
                      <span className="text-xs text-amber-600 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded-full flex-shrink-0">
                        ⚡ {cacheHits} cached
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {connName(session.connection_id)} · {session.provider} ·{' '}
                    {new Date(session.updated_at).toLocaleDateString()}
                  </p>
                </div>
                <div className="flex gap-2 flex-shrink-0 ml-3">
                  <Button
                    size="sm"
                    onClick={() => handleResume(session.id, session.connection_id)}
                  >
                    Resume
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => setDeleteConfirmId(session.id)}
                  >
                    Delete
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <Dialog open={deleteConfirmId !== null} onOpenChange={(open) => { if (!open) setDeleteConfirmId(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete session?</DialogTitle>
            <DialogDescription>
              This will permanently delete this chat session and all its messages. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <button
              onClick={() => setDeleteConfirmId(null)}
              className="rounded-md border border-border px-4 py-2 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                if (deleteConfirmId) {
                  deleteSession.mutate(deleteConfirmId);
                  setDeleteConfirmId(null);
                }
              }}
              className="rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
