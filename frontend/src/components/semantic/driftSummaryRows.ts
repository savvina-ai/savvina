// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// Kept out of DriftSummaryPanel.tsx: a module that exports anything besides components
// loses React fast refresh (react-refresh/only-export-components).

import type { DriftApplySummary } from '@/types';

/** The lists that are not changes to the model: flags and the apply's own notes. */
type NoteKey = 'changed' | 'dry_run' | 'tables_not_annotated' | 'column_tables_failed' | 'warnings';
/** Every change list of the summary: each one must have a row below. */
type RowKey = Exclude<keyof DriftApplySummary, NoteKey>;
type Change = 'remove' | 'clear' | 'rename' | 'add';

// One entry per change list, typed exhaustively: a list added to the summary type without
// a row here is a compile error, so it cannot silently vanish from the panel. The `order`
// sorts the rows: removals first (what the user cannot get back without a regenerate),
// then cleared references, then renames (they lose nothing, but change the names the model
// uses), additions last.
const ROW_SPECS: Record<RowKey, { order: number; change: Change; noun: string }> = {
  tables_removed: { order: 0, change: 'remove', noun: 'table' },
  columns_removed: { order: 1, change: 'remove', noun: 'column' },
  metrics_removed: { order: 2, change: 'remove', noun: 'metric' },
  joins_removed: { order: 3, change: 'remove', noun: 'join' },
  derived_columns_removed: { order: 4, change: 'remove', noun: 'derived column' },
  segments_removed: { order: 5, change: 'remove', noun: 'segment' },
  relationships_removed: { order: 6, change: 'remove', noun: 'relationship' },
  references_cleared: { order: 7, change: 'clear', noun: 'table reference' },
  tables_renamed: { order: 8, change: 'rename', noun: 'table' },
  columns_renamed: { order: 9, change: 'rename', noun: 'column' },
  tables_added: { order: 10, change: 'add', noun: 'table' },
  columns_added: { order: 11, change: 'add', noun: 'column' },
  metrics_added: { order: 12, change: 'add', noun: 'metric' },
  joins_added: { order: 13, change: 'add', noun: 'join' },
  derived_columns_added: { order: 14, change: 'add', noun: 'derived column' },
  segments_added: { order: 15, change: 'add', noun: 'segment' },
  relationships_added: { order: 16, change: 'add', noun: 'relationship' },
};

const ROWS = (Object.entries(ROW_SPECS) as [RowKey, (typeof ROW_SPECS)[RowKey]][])
  .sort(([, a], [, b]) => a.order - b.order)
  .map(([key, spec]) => ({ key, ...spec }));

/** The change lists that get a row, in display order. */
export const SUMMARY_ROW_KEYS: readonly RowKey[] = ROWS.map((r) => r.key);

const VERBS: Record<'preview' | 'applied', Record<Change, string>> = {
  preview: { remove: 'Will remove', clear: 'Will clear', rename: 'Will rename', add: 'Will add' },
  applied: { remove: 'Removed', clear: 'Cleared', rename: 'Renamed', add: 'Added' },
};

export const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? '' : 's'}`;

export interface SummaryRow {
  key: string;
  label: string;
  names: string[];
}

/**
 * The non-empty categories of `summary`, removals first, labelled for display. A renamed
 * column is named `table.old → new`, a renamed table `old → new`, and a cleared reference
 * `table.field`.
 */
export function summaryRows(summary: DriftApplySummary): SummaryRow[] {
  const verbs = VERBS[summary.dry_run ? 'preview' : 'applied'];
  return ROWS.map(({ key, change, noun }) => {
    let names: string[];
    if (key === 'columns_renamed') {
      names = Object.entries(summary[key]).flatMap(([table, renames]) =>
        Object.entries(renames).map(([old, live]) => `${table}.${old} → ${live}`),
      );
    } else if (key === 'tables_renamed') {
      names = Object.entries(summary[key]).map(([old, live]) => `${old} → ${live}`);
    } else if (key === 'columns_added' || key === 'columns_removed' || key === 'references_cleared') {
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
