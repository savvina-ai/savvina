// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useId, useState, type ReactNode, type Ref } from 'react';
import { AlertTriangle, Check, Eye, Info, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { DriftApplySummary } from '@/types';
import { plural, summaryDescription, summaryRows, summaryTitle } from './driftSummaryRows';

/** Names shown per category before "+N more". */
const SHOWN = 8;

// Capped, but never hidden: the banner promises the preview shows what an apply removes.
function Names({ names }: { names: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const more = names.length - SHOWN;
  return (
    <>
      <span className="font-mono">
        {(expanded ? names : names.slice(0, SHOWN)).join(', ')}
      </span>
      {more > 0 && (
        <button
          onClick={() => setExpanded((e) => !e)}
          aria-expanded={expanded}
          className="ml-1 text-muted-foreground underline-offset-2 hover:underline"
        >
          {expanded ? 'Show fewer' : `+${more} more`}
        </button>
      )}
    </>
  );
}

interface Props {
  summary: DriftApplySummary;
  /** Defaults to "Apply Drift will:" for a preview and "Schema drift applied" otherwise. */
  title?: string;
  /** Renders a Dismiss button when given. */
  onDismiss?: () => void;
  /** Actions under the lists — the preview's Apply / Cancel. */
  children?: ReactNode;
  /**
   * A focusable region named by its title and described by its counts, for the page to
   * move focus to when the panel appears (the preview and the result): focus is what
   * reliably gets it read. A live region would not be — it mounts already filled, and the
   * focus move after it cancels queued speech. A past apply in Drift history is neither.
   */
  region?: boolean;
  /** The region, for a page that moves focus to it. */
  panelRef?: Ref<HTMLDivElement>;
}

/**
 * One Apply Drift, by name: the preview of what it will do (`summary.dry_run`), the
 * result of what it did, or a logged past apply (Drift history). The summary is the
 * route's before/after diff of the model, so cascades — a metric over a removed table,
 * a relationship to it — are listed as what they are.
 */
export default function DriftSummaryPanel({
  summary,
  title,
  onDismiss,
  children,
  region = false,
  panelRef,
}: Props) {
  const id = useId();
  const rows = summaryRows(summary);
  const preview = summary.dry_run;
  const applied = !preview && rows.length > 0;
  const description = summaryDescription(summary);
  // Unchanged and nothing skipped: the model already matches the live schema.
  const nothingToApply =
    !summary.changed &&
    summary.tables_not_annotated.length === 0 &&
    summary.column_tables_failed.length === 0;
  return (
    <div
      ref={panelRef}
      {...(region && {
        role: 'region',
        tabIndex: -1,
        'aria-labelledby': `${id}-title`,
        'aria-describedby': description ? `${id}-desc` : undefined,
      })}
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        region && 'outline-none focus-visible:ring-2 focus-visible:ring-ring',
        applied ? 'border-success/30 bg-success/10' : 'border-border bg-muted/40',
      )}
    >
      {/* Nothing to apply describes itself in its visible line, which takes the id. */}
      {region && description && !nothingToApply && (
        <span id={`${id}-desc`} className="sr-only">
          {description}
        </span>
      )}
      <div className="flex items-start justify-between gap-3">
        {/* Emerald-700 in light mode, like the amber below: `text-success` on
            `bg-success/10` is about 2.5:1. */}
        <div
          className={cn(
            'flex items-center gap-2 font-medium',
            applied ? 'text-emerald-700 dark:text-success' : 'text-foreground',
          )}
        >
          {preview ? (
            <Eye className="h-4 w-4" />
          ) : applied ? (
            <Check className="h-4 w-4" />
          ) : (
            <Info className="h-4 w-4" />
          )}
          <span id={`${id}-title`}>{title ?? summaryTitle(summary)}</span>
        </div>
        {onDismiss && (
          <button
            onClick={onDismiss}
            aria-label="Dismiss"
            className="rounded p-0.5 text-muted-foreground hover:bg-muted"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      {nothingToApply ? (
        <p id={region ? `${id}-desc` : undefined} className="mt-2 text-xs text-muted-foreground">
          Nothing to apply — the model already matches the live schema.
        </p>
      ) : rows.length === 0 ? null : (
        <ul className="mt-2 space-y-1 text-xs text-foreground">
          {rows.map((r) => (
            <li key={r.key}>
              <span className="font-medium">{r.label}</span>
              {': '}
              <Names names={r.names} />
            </li>
          ))}
        </ul>
      )}

      {/* The preview makes no LLM call, so nothing built over a new table is in it — not
          even a catalog relationship to it, which the apply adds alongside the LLM's. */}
      {preview && summary.tables_added.length > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          New tables are annotated by the LLM when you apply. Relationships to them, and the
          metrics, joins and other items the LLM writes over them, appear only then and are
          not listed here.
        </p>
      )}

      {/* A new column's annotation is the LLM's too. The catalog edges on a new column (a
          renamed one is not new) are listed, but only between tables the model already
          holds: an edge to a new table appears only on apply. */}
      {preview && Object.keys(summary.columns_added).length > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          New columns are annotated by the LLM when you apply. The preview lists the
          relationships the database catalog adds on them between tables the model already
          holds.
        </p>
      )}

      {/* Amber-700 in light mode: `text-warning` on a tinted background is about 2:1. */}
      {summary.tables_not_annotated.length > 0 && (
        <p className="mt-2 text-xs text-amber-700 dark:text-warning">
          {plural(summary.tables_not_annotated.length, 'new table')}{' '}
          {summary.tables_not_annotated.length === 1 ? 'was' : 'were'} not annotated — the LLM
          returned nothing. Run Check Drift again and apply to retry:{' '}
          <Names names={summary.tables_not_annotated} />
        </p>
      )}

      {summary.warnings.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-amber-700 dark:text-warning">
          {summary.warnings.map((w, i) => (
            <li key={i} className="flex gap-1.5">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}

      {children}
    </div>
  );
}
