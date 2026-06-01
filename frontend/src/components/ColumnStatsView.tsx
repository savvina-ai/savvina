// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Data profile grid — one row per column showing type, null %, range, and top value. */

import { useMemo } from 'react';
import type { QueryResults } from '../types';
import { computeColumnStats } from '../lib/columnStats';

interface Props {
  results: QueryResults;
}

function fmt(v: number | string | null, decimals = 2): string {
  if (v === null) return '—';
  if (typeof v === 'number') {
    const abs = Math.abs(v);
    if (abs >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(decimals)}B`;
    if (abs >= 1_000_000) return `${(v / 1_000_000).toFixed(decimals)}M`;
    if (abs >= 1_000) return `${(v / 1_000).toFixed(decimals)}K`;
    return v % 1 === 0 ? String(v) : v.toFixed(decimals);
  }
  // Truncate long strings
  return v.length > 24 ? `${v.slice(0, 22)}…` : v;
}

export default function ColumnStatsView({ results }: Props) {
  const stats = useMemo(() => computeColumnStats(results), [results]);

  if (stats.length === 0) {
    return (
      <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
        No columns to profile.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left text-muted-foreground">
            <th className="px-3 py-2 font-medium">Column</th>
            <th className="px-3 py-2 font-medium">Type</th>
            <th className="px-3 py-2 font-medium text-right">Non-null</th>
            <th className="px-3 py-2 font-medium text-right">Unique</th>
            <th className="px-3 py-2 font-medium">Min</th>
            <th className="px-3 py-2 font-medium">Max</th>
            <th className="px-3 py-2 font-medium">Mean / Top value</th>
          </tr>
        </thead>
        <tbody>
          {stats.map((s) => (
            <tr key={s.name} className="border-b border-border/50 hover:bg-muted/20">
              <td className="px-3 py-1.5 font-mono font-medium text-foreground">{s.name}</td>
              <td className="px-3 py-1.5 text-muted-foreground">{s.type.toLowerCase()}</td>
              <td className="px-3 py-1.5 text-right">
                <span
                  className={
                    s.nonNullPct === 100
                      ? 'text-foreground'
                      : s.nonNullPct >= 90
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-destructive'
                  }
                >
                  {s.nonNullPct.toFixed(1)}%
                </span>
              </td>
              <td className="px-3 py-1.5 text-right text-muted-foreground">
                {s.uniqueCount.toLocaleString()}
              </td>
              <td className="px-3 py-1.5 font-mono text-muted-foreground">{fmt(s.min)}</td>
              <td className="px-3 py-1.5 font-mono text-muted-foreground">{fmt(s.max)}</td>
              <td className="px-3 py-1.5 text-muted-foreground">
                {s.mean !== null ? fmt(s.mean) : (s.topValue ?? '—')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {results.truncated && (
        <p className="px-3 py-1.5 text-[11px] text-muted-foreground">
          Stats computed from visible rows only — results are truncated.
        </p>
      )}
    </div>
  );
}
