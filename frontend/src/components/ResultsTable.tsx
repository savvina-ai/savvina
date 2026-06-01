// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import type { QueryResults } from '../types';

interface Props {
  results: QueryResults;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function Cell({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="italic text-muted-foreground/60">NULL</span>;
  }
  const str = String(value);
  if (str.length > 80) {
    return (
      <span title={str} className="cursor-help">
        {str.slice(0, 80)}…
      </span>
    );
  }
  return <>{str}</>;
}

export default function ResultsTable({ results }: Props) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-surface-elevated">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border bg-table-header">
              {results.columns.map((col) => (
                <th
                  key={col}
                  className="whitespace-nowrap px-3 py-2 text-left font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {results.rows.map((row, i) => (
              <tr
                key={i}
                className="border-t border-border transition-colors hover:bg-table-row-hover"
              >
                {row.map((cell, j) => (
                  <td
                    key={j}
                    className="max-w-xs truncate whitespace-nowrap px-3 py-2 font-mono text-xs text-foreground"
                  >
                    <Cell value={cell} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t border-border bg-muted/50 px-3 py-1.5">
        <span className="font-mono text-[10px] text-muted-foreground">
          {results.row_count} row{results.row_count !== 1 ? 's' : ''}
          {results.truncated && ' · truncated'}
        </span>
        {results.bytes_scanned != null && (
          <span className="font-mono text-[10px] text-muted-foreground">
            {formatBytes(results.bytes_scanned)} scanned
          </span>
        )}
      </div>
    </div>
  );
}
