// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Public share page — renders a shared result (table + chart) without authentication. */

import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { toPng } from 'html-to-image';
import { Download, FileDown, FileSpreadsheet } from 'lucide-react';

import type { QueryResults } from '../types';
import { downloadCsv } from '../lib/exportUtils';
import type { UserChart } from '../types/charts';
import { CHART_TYPE_LABELS } from '../types/charts';
import { suggestChart } from '../lib/chartDetection';
import ChartView from '../components/ChartView';
import ChartEditor from '../components/ChartEditor';
import { shareApi } from '../api/share';
import { cn } from '@/lib/utils';

function triggerDownload(url: string, filename: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
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

let _id = 0;

export default function SharePage() {
  const { token } = useParams<{ token: string }>();
  const [results, setResults] = useState<QueryResults | null>(null);
  const [sql, setSql] = useState<string | null>(null);
  const [chart, setChart] = useState<UserChart | null>(null);
  const [activeView, setActiveView] = useState<'table' | 'chart'>('table');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!token) return;
    shareApi
      .getSharedResult(token)
      .then((res) => {
        const r = res.data.results;
        setResults(r);
        setSql(res.data.query_generated);
        const suggestion = suggestChart(r);
        setChart({ ...suggestion, id: `share-${++_id}` });
      })
      .catch(() => setError('This shared result is unavailable or has expired.'))
      .finally(() => setLoading(false));
  }, [token]);

  const exportPng = async () => {
    if (!chartRef.current) return;
    try {
      const wasDark = document.documentElement.classList.contains('dark');
      if (wasDark) document.documentElement.classList.replace('dark', 'light');
      try {
        triggerDownload(
          await toPng(chartRef.current, { cacheBust: true, backgroundColor: '#ffffff' }),
          'savvina-chart.png',
        );
      } finally {
        if (wasDark) document.documentElement.classList.replace('light', 'dark');
      }
    } catch { /* ignore */ }
  };

  const exportSvg = () => {
    const svg = chartRef.current?.querySelector('svg');
    if (!svg) return;
    const blob = new Blob([new XMLSerializer().serializeToString(svg)], {
      type: 'image/svg+xml',
    });
    triggerDownload(URL.createObjectURL(blob), 'savvina-chart.svg');
  };

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border px-6 py-3 flex items-center justify-between">
        <span className="savvina-grad-text font-display text-base font-semibold">savvina ai</span>
        <span className="eyebrow">Shared result</span>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
        {loading && (
          <div className="flex items-center justify-center py-20">
            <span className="text-sm text-muted-foreground">Loading…</span>
          </div>
        )}

        {error && (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-6 text-center">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        {results && chart && (
          <>
            {/* View toggle + export actions */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setActiveView('table')}
                  className={cn(
                    'rounded px-3 py-1.5 text-xs font-medium transition-colors',
                    activeView === 'table'
                      ? 'bg-brand-gradient text-white shadow-gradient-btn'
                      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                  )}
                >
                  Table
                </button>
                {(Object.keys(CHART_TYPE_LABELS) as UserChart['type'][]).map((type) => (
                  <button
                    key={type}
                    onClick={() => {
                      setChart((c) => c ? { ...c, type } : c);
                      setActiveView('chart');
                    }}
                    className={cn(
                      'rounded px-3 py-1.5 text-xs font-medium transition-colors',
                      activeView === 'chart' && chart.type === type
                        ? 'bg-brand-gradient text-white shadow-gradient-btn'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                    )}
                  >
                    {CHART_TYPE_LABELS[type]}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => results && downloadCsv(results, 'shared-results.csv')}
                  className="flex items-center gap-1 rounded px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <FileDown className="h-3 w-3" />
                  CSV
                </button>
                <button
                  onClick={() => {
                    if (!token) return;
                    const base = window.location.origin;
                    const a = document.createElement('a');
                    a.href = `${base}/api/v1/public/share/${token}/xlsx`;
                    a.download = 'shared-results.xlsx';
                    a.click();
                  }}
                  className="flex items-center gap-1 rounded px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <FileSpreadsheet className="h-3 w-3" />
                  Excel
                </button>
                {activeView === 'chart' && (
                  <>
                    <button
                      onClick={exportPng}
                      className="flex items-center gap-1 rounded px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                    >
                      <Download className="h-3 w-3" />
                      PNG
                    </button>
                    <button
                      onClick={exportSvg}
                      className="flex items-center gap-1 rounded px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                    >
                      <Download className="h-3 w-3" />
                      SVG
                    </button>
                    <button
                      onClick={() => setEditorOpen((o) => !o)}
                      className={cn(
                        'rounded px-2 py-1.5 text-xs transition-colors',
                        editorOpen
                          ? 'bg-muted text-foreground'
                          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                      )}
                    >
                      Settings
                    </button>
                  </>
                )}
              </div>
            </div>

            {/* Content area */}
            <div className="overflow-hidden rounded-xl border border-border bg-surface-elevated">
              {activeView === 'table' ? (
                <>
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
                  <div className="flex items-center border-t border-border bg-muted/50 px-3 py-1.5">
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {results.row_count} row{results.row_count !== 1 ? 's' : ''}
                      {results.truncated && ' · truncated'}
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <ChartView results={results} chart={chart} containerRef={chartRef} />
                  {editorOpen && (
                    <ChartEditor
                      chart={chart}
                      columns={results.columns}
                      onChange={setChart}
                    />
                  )}
                </>
              )}
            </div>

            {sql && (
              <details className="rounded-xl border border-border">
                <summary className="cursor-pointer select-none px-4 py-2 text-xs font-mono text-muted-foreground hover:text-foreground">
                  View SQL
                </summary>
                <pre className="overflow-x-auto bg-sql-bg px-4 py-3 text-xs text-sql-text">{sql}</pre>
              </details>
            )}
          </>
        )}
      </main>

      <footer className="border-t border-border px-6 py-3 text-center">
        <span className="text-xs text-muted-foreground">
          Powered by <span className="savvina-grad-text font-semibold">savvina ai</span>
        </span>
      </footer>
    </div>
  );
}
