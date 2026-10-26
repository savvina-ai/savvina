// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// Kept out of DriftSummaryPanel.tsx: a module that exports anything besides components
// loses React fast refresh (react-refresh/only-export-components).

import type { DriftApplySummary } from '@/types';

type ColumnKey = 'columns_added' | 'columns_removed';
type ListKey = Exclude<
  keyof DriftApplySummary,
  'changed' | 'dry_run' | ColumnKey | 'tables_not_annotated' | 'column_tables_failed' | 'warnings'
>;

// Removals first: they are what the user cannot get back without a regenerate.
const ROWS: { key: ListKey | ColumnKey; added: boolean; noun: string }[] = [
  { key: 'tables_removed', added: false, noun: 'table' },
  { key: 'columns_removed', added: false, noun: 'column' },
  { key: 'metrics_removed', added: false, noun: 'metric' },
  { key: 'joins_removed', added: false, noun: 'join' },
  { key: 'derived_columns_removed', added: false, noun: 'derived column' },
  { key: 'segments_removed', added: false, noun: 'segment' },
  { key: 'relationships_removed', added: false, noun: 'relationship' },
  { key: 'tables_added', added: true, noun: 'table' },
  { key: 'columns_added', added: true, noun: 'column' },
  { key: 'metrics_added', added: true, noun: 'metric' },
  { key: 'joins_added', added: true, noun: 'join' },
  { key: 'derived_columns_added', added: true, noun: 'derived column' },
  { key: 'segments_added', added: true, noun: 'segment' },
  { key: 'relationships_added', added: true, noun: 'relationship' },
];

export const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`;

export interface SummaryRow {
  key: string;
  label: string;
  names: string[];
}

/** The non-empty categories of `summary`, removals first, labelled for display. */
export function summaryRows(summary: DriftApplySummary): SummaryRow[] {
  const [addVerb, removeVerb] = summary.dry_run
    ? ['Will add', 'Will remove']
    : ['Added', 'Removed'];
  return ROWS.map(({ key, added, noun }) => {
    const names =
      key === 'columns_added' || key === 'columns_removed'
        ? Object.entries(summary[key]).flatMap(([table, cols]) =>
            cols.map((c) => `${table}.${c}`),
          )
        : summary[key];
    return { key, label: `${added ? addVerb : removeVerb} ${plural(names.length, noun)}`, names };
  }).filter((r) => r.names.length > 0);
}

/** The panel's heading. A success only when the model changed. */
export function summaryTitle(summary: DriftApplySummary): string {
  if (summary.dry_run) return 'Apply Drift will:';
  return summary.changed ? 'Schema drift applied' : 'Nothing was applied';
}

/**
 * One line for assistive tech: the row labels; otherwise the new tables not annotated;
 * failing that, the tables whose new columns were not annotated (a failed batch, no
 * answer, or an earlier apply's mark); otherwise why there is nothing.
 */
export function summaryDescription(summary: DriftApplySummary): string {
  const rows = summaryRows(summary);
  if (rows.length > 0) return rows.map((r) => r.label).join(' · ');
  const skipped = summary.tables_not_annotated.length;
  if (skipped > 0) return `${plural(skipped, 'new table')} not annotated`;
  const failed = summary.column_tables_failed.length;
  if (failed > 0) return `New columns on ${plural(failed, 'table')} not annotated`;
  return 'Nothing to apply — the model already matches the live schema.';
}

/** Title and description as one sentence, for a status line that speaks for the panel. */
export function summaryAnnouncement(summary: DriftApplySummary): string {
  const title = summaryTitle(summary);
  const description = summaryDescription(summary);
  if (!description) return title;
  return `${title}${title.endsWith(':') ? '' : ':'} ${description}`;
}
