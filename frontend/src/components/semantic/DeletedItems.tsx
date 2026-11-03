// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { semanticApi } from '../../api/semantic';
import { apiErrorMessage } from '@/lib/apiError';
import { tombstonesKey } from './tombstonesKey';
import type { TombstoneEntry, TombstoneKind } from '@/types';

const KIND_LABELS: Record<TombstoneKind, string> = {
  metric: 'Metric',
  segment: 'Segment',
  derived_column: 'Derived column',
  common_join: 'Join',
  relationship: 'Relationship',
};

/**
 * Items deleted from the model (`GET .../semantic/tombstones`), newest first, collapsed
 * under one heading like the drift history. Regenerate and Apply Drift leave them out;
 * **Restore** lifts one, so the next Regenerate (or Apply Drift, for a relationship) may
 * bring the generator's version back. Renders nothing while the list is empty, except the
 * note confirming the last restore.
 */
export default function DeletedItems({
  connectionId,
  enabled,
  disabled = false,
}: {
  connectionId: string;
  /** Fetch only once a model exists (the route is 404 without one). */
  enabled: boolean;
  /** Restore is unavailable while a run or an apply is in flight. */
  disabled?: boolean;
}) {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  // A note fades after a while, like the page's "Saved"; the next restore replaces it.
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(timer);
  }, [notice]);
  const { data: entries } = useQuery<TombstoneEntry[]>({
    queryKey: tombstonesKey(connectionId),
    queryFn: () => semanticApi.getTombstones(connectionId),
    enabled,
  });
  const restore = useMutation({
    mutationFn: (entry: TombstoneEntry) =>
      semanticApi.restoreTombstone(connectionId, entry.kind, entry.key),
    onSuccess: (_void, entry) => {
      setNotice(
        entry.kind === 'common_join'
          ? `Restored join ${entry.key}: joins are not generated, so add it again by hand if you need it.`
          : `Restored ${KIND_LABELS[entry.kind].toLowerCase()} ${entry.key}: a later Regenerate or Apply Drift may bring it back.`,
      );
      void queryClient.invalidateQueries({ queryKey: tombstonesKey(connectionId) });
    },
    onError: (err) => setNotice(apiErrorMessage(err, 'Could not restore the item')),
  });

  const list = entries ?? [];
  if (list.length === 0 && !notice) return null;
  return (
    <details className="rounded-xl border border-border px-4 py-3 text-sm">
      <summary className="cursor-pointer text-muted-foreground">
        Deleted items ({list.length})
      </summary>
      <p className="mt-2 text-xs text-muted-foreground">
        Regenerate and Apply Drift leave these out. Restore one to let the generator bring it
        back.
      </p>
      {notice && (
        <p role="status" className="mt-2 text-xs text-muted-foreground">
          {notice}
        </p>
      )}
      <ul className="mt-2 space-y-1.5">
        {list.map((entry) => (
          <li key={`${entry.kind}:${entry.key}`} className="flex items-center gap-2 text-xs">
            <span className="font-medium">{KIND_LABELS[entry.kind]}</span>
            <span className="min-w-0 flex-1 truncate font-mono" title={entry.key}>
              {entry.key}
            </span>
            <span className="text-muted-foreground">
              {new Date(entry.created_at).toLocaleDateString()}
            </span>
            <button
              type="button"
              onClick={() => restore.mutate(entry)}
              disabled={disabled || restore.isPending}
              aria-label={`Restore ${KIND_LABELS[entry.kind].toLowerCase()} ${entry.key}`}
              className="rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
            >
              Restore
            </button>
          </li>
        ))}
      </ul>
    </details>
  );
}
