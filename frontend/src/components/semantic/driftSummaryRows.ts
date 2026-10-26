// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// Kept out of DriftSummaryPanel.tsx: a module that exports anything besides components
// loses React fast refresh (react-refresh/only-export-components).

import type { DriftApplySummary } from '@/types';

type ColumnKey = 'columns_added' | 'columns_removed';
type RenameKey = 'columns_renamed';
type ListKey = Exclude<
  keyof DriftApplySummary,
  | 'changed'
  | 'dry_run'
  | ColumnKey
  | RenameKey
  | 'tables_not_annotated'
  | 'column_tables_failed'
  | 'warnings'
>;
type Change = 'remove' | 'rename' | 'add';

// Removals first: they are what the user cannot get back without a regenerate. Renames
// next: they lose nothing, but change the names the model uses. Additions last.
const ROWS: { key: ListKey | ColumnKey | RenameKey; change: Change; noun: string }[] = [
  { key: 'tables_removed', change: 'remove', noun: 'table' },
  { key: 'columns_removed', change: 'remove', noun: 'column' },
  { key: 'metrics_removed', change: 'remove', noun: 'metric' },
  { key: 'joins_removed', change: 'remove', noun: 'join' },
  { key: 'derived_columns_removed', change: 'remove', noun: 'derived column' },
  { key: 'segments_removed', change: 'remove', noun: 'segment' },
  { key: 'relationships_removed', change: 'remove', noun: 'relationship' },
  { key: 'columns_renamed', change: 'rename', noun: 'column' },
  { key: 'tables_added', change: 'add', noun: 'table' },
  { key: 'columns_added', change: 'add', noun: 'column' },
  { key: 'metrics_added', change: 'add', noun: 'metric' },
  { key: 'joins_added', change: 'add', noun: 'join' },
  { key: 'derived_columns_added', change: 'add', noun: 'derived column' },
  { key: 'segments_added', change: 'add', noun: 'segment' },
  { key: 'relationships_added', change: 'add', noun: 'relationship' },
];

const VERBS: Record<'preview' | 'applied', Record<Change, string>> = {
  preview: { remove: 'Will remove', rename: 'Will rename', add: 'Will add' },
  applied: { remove: 'Removed', rename: 'Renamed', add: 'Added' },
};

export const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`;

export interface SummaryRow {
  key: string;
  label: string;
  names: string[];
}

/**
 * The non-empty categories of `summary`, removals first, labelled for display. A renamed
 * column is named `table.old → new`.
 */
export function summaryRows(summary: DriftApplySummary): SummaryRow[] {
  const verbs = VERBS[summary.dry_run ? 'preview' : 'applied'];
  return ROWS.map(({ key, change, noun }) => {
    let names: string[];
    if (key === 'columns_renamed') {
      names = Object.entries(summary[key]).flatMap(([table, renames]) =>
        Object.entries(renames).map(([old, live]) => `${table}.${old} → ${live}`),
      );
    } else if (key === 'columns_added' || key === 'columns_removed') {
      names = Object.entries(summary[key]).flatMap(([table, cols]) =>
        cols.map((c) => `${table}.${c}`),
      );
    } else {
      names = summary[key];
    }
    return { key, label: `${verbs[change]} ${plural(names.length, noun)}`, names };
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
