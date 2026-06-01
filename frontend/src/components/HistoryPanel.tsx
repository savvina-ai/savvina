// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { MessageSquare, Search, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAppStore } from '../store/appStore';
import { useSessions, useDeleteSession } from '../hooks/useChat';
import { ConfirmDeleteDialog } from './ui/confirm-delete-dialog';

interface Props {
  embedded?: boolean;
}

function relativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString();
}

export default function HistoryPanel({ embedded = false }: Props) {
  const [width, setWidth] = useState(256);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  useEffect(() => {
    if (embedded) return;
    const onMouseMove = (e: globalThis.MouseEvent) => {
      if (!isResizing.current) return;
      const delta = e.clientX - startX.current;
      setWidth(Math.min(500, Math.max(180, startWidth.current + delta)));
    };
    const onMouseUp = () => {
      isResizing.current = false;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, [embedded]);

  const activeConnectionId = useAppStore((s) => s.activeConnectionId);
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const clearMessages = useAppStore((s) => s.clearMessages);
  const setActiveSession = useAppStore((s) => s.setActiveSession);

  const [search, setSearch] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const { data: sessions = [] } = useSessions(activeConnectionId);
  const deleteSession = useDeleteSession();
  const queryClient = useQueryClient();

  const filtered = search
    ? sessions.filter((s) =>
        (s.title || 'Untitled chat').toLowerCase().includes(search.toLowerCase()),
      )
    : sessions;

  const handleSelect = (id: string) => {
    queryClient.removeQueries({ queryKey: ['history', id] });
    clearMessages();
    setActiveSession(id);
  };

  return (
    <div
      className={cn(
        'relative flex flex-col animate-fade-in',
        embedded ? 'flex-1 min-h-0' : 'h-full shrink-0 border-r border-border bg-background',
      )}
      style={embedded ? undefined : { width }}
    >
      {/* Search header */}
      <div className="border-b border-border px-3 py-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search chats…"
            className="h-8 w-full rounded-md border border-border bg-secondary pl-8 pr-2 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto p-2">
        {sessions.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">No sessions yet</p>
        ) : filtered.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">No results</p>
        ) : (
          <div className="space-y-0.5">
            {filtered.map((session) => {
              const isActive = session.id === activeSessionId;
              return (
                <div
                  key={session.id}
                  className={cn(
                    'group flex w-full items-start gap-2 rounded-lg px-3 py-2.5 text-left transition-colors',
                    isActive
                      ? 'border-l-2 border-primary bg-primary/15 text-foreground'
                      : 'border-l-2 border-transparent text-foreground hover:bg-muted',
                  )}
                >
                  <button
                    onClick={() => handleSelect(session.id)}
                    className="flex min-w-0 flex-1 flex-col gap-0.5"
                  >
                    <div className="flex items-center gap-2">
                      <MessageSquare className="h-3 w-3 shrink-0 text-muted-foreground" />
                      <span className="flex-1 truncate text-xs font-medium">
                        {session.title || 'Untitled chat'}
                      </span>
                    </div>
                    <span className="pl-5 text-[10px] text-muted-foreground/70">
                      {relativeTime(session.updated_at)}
                    </span>
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteConfirmId(session.id);
                    }}
                    title="Delete session"
                    aria-label="Delete session"
                    className="mt-1 shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-colors hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Resize handle — standalone mode only */}
      {!embedded && (
        <div
          onMouseDown={(e) => {
            isResizing.current = true;
            startX.current = e.clientX;
            startWidth.current = width;
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'col-resize';
            e.preventDefault();
          }}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50"
        />
      )}

      <ConfirmDeleteDialog
        open={deleteConfirmId !== null}
        onOpenChange={(open) => { if (!open) setDeleteConfirmId(null); }}
        title="Delete session?"
        description="This will permanently delete this chat session and all its messages. This cannot be undone."
        onConfirm={() => {
          if (deleteConfirmId) {
            deleteSession.mutate(deleteConfirmId);
            setDeleteConfirmId(null);
          }
        }}
        isPending={deleteSession.isPending}
      />
    </div>
  );
}
