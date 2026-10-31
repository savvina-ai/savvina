// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';

interface Props {
  tablesDone: number;
  tablesTotal: number | null;
  /** A provider is selected and the user may generate. */
  canGenerate: boolean;
  /** Why every action is disabled, when it is; shown as each button's title. */
  disabledReason: string | null;
  /** At least one table row exists — Finish has something to finalise. */
  hasTables: boolean;
  onResume: () => void;
  onFinish: () => void;
  onRestart: () => void;
}

const button =
  'rounded px-2.5 py-1 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50';

/**
 * The interrupted-generation banner. Three actions, in order of how much they keep:
 * Resume generates only the tables the interrupted run had not reached and then the
 * globals phase; Finish runs the globals phase over what exists; Restart deletes the
 * model first and is the only destructive path — `/generate/init` itself blanks
 * nothing any more. The page puts Restart behind a confirm dialog.
 *
 * Not rendered while a run is in flight: `SemanticModelPage` unmounts it under
 * `anyGenerationPending`, so there is no "busy" state to handle here.
 */
export default function GenerationBanner({
  tablesDone,
  tablesTotal,
  canGenerate,
  disabledReason,
  hasTables,
  onResume,
  onFinish,
  onRestart,
}: Props) {
  const blocked = !canGenerate;
  const finishReason = !hasTables ? 'No tables were generated — resume or restart instead' : null;

  return (
    // Amber-700 in light mode: `text-warning` on `bg-warning/10` is about 2:1.
    <div className="flex flex-wrap items-center gap-3 rounded-md border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-amber-700 dark:text-warning">
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <div className="space-y-0.5">
        <p>Generation was interrupted at table {tablesDone} of {tablesTotal ?? '?'}.</p>
        {/* Visible as well as on each button's title: a disabled button's tooltip is
            neither reliably shown nor announced. */}
        {disabledReason && <p className="text-xs">{disabledReason}</p>}
        {finishReason && <p className="text-xs">{finishReason}</p>}
      </div>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <button
          onClick={onResume}
          disabled={blocked}
          title={disabledReason ?? 'Generate only the tables that are still missing, then finish'}
          className={cn(button, 'bg-warning/30 hover:bg-warning/40')}
        >
          Resume
        </button>
        <button
          onClick={onFinish}
          disabled={blocked || !hasTables}
          title={
            disabledReason ??
            finishReason ??
            'Generate metrics, segments and derived columns from the tables already generated'
          }
          className={cn(button, 'bg-warning/20 hover:bg-warning/30')}
        >
          Finish with what&apos;s generated
        </button>
        <button
          onClick={onRestart}
          disabled={blocked}
          title={disabledReason ?? 'Delete the partial model and generate again from scratch'}
          className={cn(button, 'bg-warning/20 hover:bg-warning/30')}
        >
          Restart from scratch
        </button>
      </div>
    </div>
  );
}
