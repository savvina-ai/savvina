// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { AlertTriangle, X } from 'lucide-react';
import type { DriftReport } from '@/types';

/**
 * What the background drift monitor found (`GET .../semantic/drift/last`), shown until the
 * user runs Check Drift or dismisses it. The page renders it only for a report newer than
 * its own check and with at least one warning. Check Drift runs the live check the preview
 * and Apply Drift need.
 */
export default function BackgroundDriftNotice({
  report,
  onCheck,
  checkDisabled,
  onDismiss,
}: {
  report: DriftReport;
  onCheck: () => void;
  checkDisabled: boolean;
  onDismiss: () => void;
}) {
  const when = new Date(report.checked_at).toLocaleString();
  const count = report.warning_count;
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-medium text-warning">
          Background check at {when}: {count} schema {count === 1 ? 'change' : 'changes'}
        </p>
        <p className="text-xs text-muted-foreground">
          {report.has_actionable_changes
            ? 'The database changed since the model was built. Check Drift to review it and apply it.'
            : 'Check Drift to see the details.'}
          {report.schema_source === 'cache' ? ' The source was unreachable, so the cached schema was compared.' : ''}
        </p>
        <button
          type="button"
          onClick={onCheck}
          disabled={checkDisabled}
          className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
        >
          Check Drift
        </button>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss the background drift notice"
        className="text-muted-foreground hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
