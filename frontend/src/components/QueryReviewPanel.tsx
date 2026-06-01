// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import { Play, Pencil, Copy, X } from 'lucide-react';
import QueryHighlight from './QueryHighlight';
import { useExecutePending, useEditAndExecute } from '../hooks/useChat';
import type { ChatMessage } from '../types';

interface Props {
  message: ChatMessage;
  onCancel?: () => void;
}

export default function QueryReviewPanel({ message, onCancel }: Props) {
  const [editing, setEditing] = useState(false);
  const [editedQuery, setEditedQuery] = useState(message.query_generated ?? '');

  const executePending = useExecutePending();
  const editAndExecute = useEditAndExecute();

  const handleRun = () => {
    executePending.mutate(message.id);
  };

  const handleRunEdited = () => {
    editAndExecute.mutate({ messageId: message.id, query: editedQuery });
    setEditing(false);
  };

  const copyQuery = async () => {
    if (message.query_generated) {
      await navigator.clipboard.writeText(message.query_generated);
    }
  };

  const busy = executePending.isPending || editAndExecute.isPending;

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-surface-elevated">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border bg-muted/50 px-3 py-2">
        <span className="text-sm font-medium text-foreground">Generated Query</span>
        <div className="flex items-center gap-2">
          {!editing && (
            <button
              onClick={() => setEditing(true)}
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <Pencil className="h-3 w-3" />
              Edit
            </button>
          )}
          {onCancel && (
            <button
              onClick={onCancel}
              className="rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* SQL block or edit textarea */}
      {editing ? (
        <textarea
          value={editedQuery}
          onChange={(e) => setEditedQuery(e.target.value)}
          rows={6}
          className="w-full bg-sql-bg px-3 py-2 font-mono text-sm text-sql-text focus:outline-none"
        />
      ) : (
        message.query_generated && (
          <QueryHighlight
            query={message.query_generated}
            dialect={message.query_dialect}
            showHeader={false}
          />
        )
      )}

      {/* Explanation */}
      {message.content && (
        <div className="border-t border-border px-3 py-2">
          <p className="text-sm text-muted-foreground">{message.content}</p>
        </div>
      )}

      {/* Action buttons */}
      <div className="flex items-center gap-2 border-t border-border px-3 py-2">
        {editing ? (
          <>
            <button
              onClick={handleRunEdited}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-md bg-brand-gradient px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              {busy ? 'Running…' : 'Run Edited Query'}
            </button>
            <button
              onClick={() => setEditing(false)}
              className="text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              Cancel edit
            </button>
          </>
        ) : (
          <>
            <button
              onClick={handleRun}
              disabled={busy}
              className="flex items-center gap-1.5 rounded-md bg-brand-gradient px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              {busy ? 'Running…' : 'Run Query'}
            </button>
            <button
              onClick={() => setEditing(true)}
              className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-foreground transition-colors hover:bg-muted"
            >
              <Pencil className="h-3.5 w-3.5" />
              Edit Query
            </button>
            <button
              onClick={copyQuery}
              className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-foreground transition-colors hover:bg-muted"
            >
              <Copy className="h-3.5 w-3.5" />
              Copy
            </button>
          </>
        )}
      </div>
    </div>
  );
}
