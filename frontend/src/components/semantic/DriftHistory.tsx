// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import DriftSummaryPanel from './DriftSummaryPanel';
import { summaryRows } from './driftSummaryRows';
import type { DriftApplyHistoryEntry } from '@/types';

/**
 * Logged Apply Drifts, newest first, from `GET .../semantic/drift/history`. Collapsed to
 * one line each (date and counts); an entry expands to its full summary. Renders nothing
 * until there is an entry.
 */
export default function DriftHistory({
  entries,
  limit,
}: {
  entries: DriftApplyHistoryEntry[];
  /** The page size asked for: a full page may not be every apply there is. */
  limit: number;
}) {
  // Keyed on `applied_at`, not the index: an apply prepends an entry when the list reloads,
  // and an index would then open a different one.
  const [open, setOpen] = useState<string | null>(null);
  if (entries.length === 0) return null;
  return (
    <details className="rounded-xl border border-border px-4 py-3 text-sm">
      <summary className="cursor-pointer text-muted-foreground">
        Drift history ({entries.length >= limit ? `last ${entries.length}` : entries.length})
      </summary>
      <ul className="mt-2 space-y-2">
        {entries.map((entry, i) => {
          const when = new Date(entry.applied_at).toLocaleString();
          const rows = summaryRows(entry.summary);
          const expanded = open === entry.applied_at;
          return (
            <li key={`${entry.applied_at}-${i}`}>
              <button
                onClick={() => setOpen(expanded ? null : entry.applied_at)}
                aria-expanded={expanded}
                className="text-left text-xs hover:underline"
              >
                <span className="font-medium">{when}</span>
                {' — '}
                {rows.length > 0 ? rows.map((r) => r.label).join(' · ') : 'No changes'}
              </button>
              {expanded && (
                <div className="mt-2">
                  <DriftSummaryPanel summary={entry.summary} title={`Applied ${when}`} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
