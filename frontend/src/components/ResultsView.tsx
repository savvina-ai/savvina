// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * Results display — Fabric-style tabbed interface.
 * Default view is the data table. Users add charts on demand with "+ New Chart".
 * Each chart tab has a config panel (ChartEditor) shown below the chart.
 * The table/chart can be expanded to a full-screen overlay via the Maximize button.
 */

import { useState, useRef, useCallback, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { toPng } from 'html-to-image';
import {
  Plus,
  Download,
  Share2,
  Check,
  Settings2,
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
  Trash2,
  Maximize2,
  Minimize2,
  FileSpreadsheet,
  FileDown,
  RefreshCw,
} from 'lucide-react';

import type { QueryResults } from '../types';
import type { UserChart } from '../types/charts';
import { chatApi } from '@/api/chat';
import { suggestChart } from '../lib/chartDetection';
import { downloadCsv, triggerUrlDownload } from '../lib/exportUtils';
import { exportApi } from '../api/export';
import ChartView from './ChartView';
import ChartEditor from './ChartEditor';
import ColumnStatsView from './ColumnStatsView';
import { shareApi } from '../api/share';
import { cn } from '@/lib/utils';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

interface Props {
  results: QueryResults;
  messageId: string;
  /** When true, + New Chart and Share are disabled (SSE streaming in progress). */
  isStreaming?: boolean;
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
  if (str.length > CELL_TRUNCATE_AT) {
    return (
      <span title={str} className="cursor-help">
        {str.slice(0, CELL_TRUNCATE_AT)}…
      </span>
    );
  }
  return <>{str}</>;
}

const CELL_TRUNCATE_AT = 80;

export default function ResultsView({ results, messageId, isStreaming = false }: Props) {
  const chartCounterRef = useRef(0);

  const [activeTab, setActiveTab] = useState<'table' | string>('table');
  const [charts, setCharts] = useState<UserChart[]>([]);
  const [editorOpen, setEditorOpen] = useState(false);
  const [shareState, setShareState] = useState<'idle' | 'loading' | 'copied'>('idle');
  const [isExpanded, setIsExpanded] = useState(false);

  // Sort state
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<'ASC' | 'DESC'>('ASC');
  const [sortedResults, setSortedResults] = useState<QueryResults | null>(null);
  const [isSorting, setIsSorting] = useState(false);
  const [smartSortActive, setSmartSortActive] = useState(false);
  const [sortError, setSortError] = useState<string | null>(null);

  const displayedResults = sortedResults ?? results;

  // Two refs: one for the inline chart, one for the expanded-overlay chart.
  // Export functions pick the active one based on isExpanded.
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const expandedChartContainerRef = useRef<HTMLDivElement>(null);

  const activeChart = charts.find((c) => c.id === activeTab);

  // Close expanded view on Escape
  useEffect(() => {
    if (!isExpanded) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsExpanded(false);
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [isExpanded]);

  // ── Tab management ──────────────────────────────────────────────────────────

  const addChart = useCallback(() => {
    if (isStreaming) return;
    const suggestion = suggestChart(results);
    const num = charts.length + 1;
    const newChart: UserChart = {
      ...suggestion,
      id: `chart-${++chartCounterRef.current}-${Date.now()}`,
      title: num === 1 ? suggestion.title : `Chart ${num}`,
    };
    setCharts((prev) => [...prev, newChart]);
    setActiveTab(newChart.id);
    setEditorOpen(true);
  }, [isStreaming, results, charts.length]);

  const deleteChart = useCallback(
    (id: string) => {
      setCharts((prev) => prev.filter((c) => c.id !== id));
      if (activeTab === id) setActiveTab('table');
    },
    [activeTab],
  );

  const updateChart = useCallback((updated: UserChart) => {
    setCharts((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
  }, []);

  // ── Sort ────────────────────────────────────────────────────────────────────

  const handleSort = useCallback(
    async (col: string) => {
      if (isStreaming) return;
      const newDir: 'ASC' | 'DESC' =
        col === sortCol ? (sortDir === 'ASC' ? 'DESC' : 'ASC') : 'ASC';
      setSortCol(col);
      setSortDir(newDir);

      if (!results.truncated) {
        // Client-side sort — no API call
        const base = sortedResults ?? results;
        const colIdx = base.columns.indexOf(col);
        const sorted = [...base.rows].sort((a, b) => {
          const av = a[colIdx];
          const bv = b[colIdx];
          if (av === null || av === undefined) return 1;
          if (bv === null || bv === undefined) return -1;
          const an = Number(av);
          const bn = Number(bv);
          const cmp =
            !isNaN(an) && !isNaN(bn)
              ? an - bn
              : String(av).localeCompare(String(bv));
          return newDir === 'ASC' ? cmp : -cmp;
        });
        setSortedResults({ ...base, rows: sorted });
        setSmartSortActive(false);
      } else {
        // Smart sort — re-execute with ORDER BY via backend
        setIsSorting(true);
        setSortError(null);
        try {
          const data = await chatApi.sortResults(messageId, col, newDir);
          setSortedResults(data);
          setSmartSortActive(true);
        } catch {
          setSortError('Sort failed — results may be unordered');
        } finally {
          setIsSorting(false);
        }
      }
    },
    [isStreaming, sortCol, sortDir, results, sortedResults, messageId],
  );

  // ── Export ──────────────────────────────────────────────────────────────────

  const exportPng = useCallback(async () => {
    const ref = isExpanded ? expandedChartContainerRef : chartContainerRef;
    if (!ref.current) return;
    try {
      const el = ref.current;
      const wasDark = document.documentElement.classList.contains('dark');
      if (wasDark) document.documentElement.classList.replace('dark', 'light');
      try {
        const url = await toPng(el, { cacheBust: true, backgroundColor: '#ffffff' });
        triggerUrlDownload(url, `chart-${messageId}.png`);
      } finally {
        if (wasDark) document.documentElement.classList.replace('light', 'dark');
      }
    } catch { /* browser sandbox may block */ }
  }, [messageId, isExpanded]);

  const exportSvg = useCallback(() => {
    const ref = isExpanded ? expandedChartContainerRef : chartContainerRef;
    const svg = ref.current?.querySelector('svg');
    if (!svg) return;
    const blob = new Blob([new XMLSerializer().serializeToString(svg)], {
      type: 'image/svg+xml',
    });
    triggerUrlDownload(URL.createObjectURL(blob), `chart-${messageId}.svg`);
  }, [messageId, isExpanded]);

  // ── Share ───────────────────────────────────────────────────────────────────

  const handleShare = useCallback(async () => {
    if (shareState !== 'idle' || isStreaming) return;
    setShareState('loading');
    try {
      const res = await shareApi.shareMessage(messageId);
      const url = `${window.location.origin}/share/${res.data.share_token}`;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        const el = document.createElement('textarea');
        el.value = url;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      }
      setShareState('copied');
      setTimeout(() => setShareState('idle'), 2500);
    } catch {
      setShareState('idle');
    }
  }, [messageId, shareState, isStreaming]);

  // ── Shared sub-renders ──────────────────────────────────────────────────────

  /** Tab strip — shared between inline and expanded views. */
  const renderTabs = () => (
    <div className="flex items-center gap-0.5 overflow-x-auto">
      <button
        onClick={() => setActiveTab('table')}
        className={cn(
          'whitespace-nowrap rounded px-2.5 py-1 text-xs font-medium transition-colors',
          activeTab === 'table'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:bg-background/60 hover:text-foreground',
        )}
      >
        Table
      </button>

      <button
        onClick={() => setActiveTab('stats')}
        className={cn(
          'whitespace-nowrap rounded px-2.5 py-1 text-xs font-medium transition-colors',
          activeTab === 'stats'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:bg-background/60 hover:text-foreground',
        )}
      >
        Stats
      </button>

      {charts.map((chart) => (
        <div
          key={chart.id}
          className={cn(
            'group flex items-center gap-0.5 rounded transition-colors',
            activeTab === chart.id
              ? 'bg-background shadow-sm'
              : 'hover:bg-background/60',
          )}
        >
          <button
            onClick={() => setActiveTab(chart.id)}
            className={cn(
              'whitespace-nowrap px-2 py-1 text-xs font-medium transition-colors',
              activeTab === chart.id ? 'text-foreground' : 'text-muted-foreground',
            )}
          >
            {chart.title || 'Untitled chart'}
          </button>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className={cn(
                  'rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground',
                  'opacity-0 group-hover:opacity-100',
                  activeTab === chart.id && 'opacity-100',
                )}
              >
                <ChevronDown className="h-3 w-3" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="text-xs">
              <DropdownMenuItem
                onClick={() => deleteChart(chart.id)}
                className="text-xs text-destructive focus:text-destructive"
              >
                <Trash2 className="mr-2 h-3.5 w-3.5" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ))}

      {charts.length < 5 && (
        <button
          onClick={addChart}
          disabled={isStreaming}
          className={cn(
            'flex items-center gap-1 whitespace-nowrap rounded px-2 py-1 text-xs text-muted-foreground transition-colors',
            isStreaming
              ? 'cursor-not-allowed opacity-40'
              : 'hover:bg-background/60 hover:text-foreground',
          )}
        >
          <Plus className="h-3 w-3" />
          New Chart
        </button>
      )}
    </div>
  );

  /** Data + chart export + share buttons — shared between inline and expanded views. */
  const renderActions = (expanded: boolean) => (
    <div className="flex shrink-0 items-center gap-0.5 pl-2">
      <button
        onClick={() => downloadCsv(displayedResults, `query-${messageId}.csv`)}
        title="Download as CSV"
        className="flex items-center gap-1 rounded px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide text-primary transition-colors hover:bg-accent hover:text-accent-foreground"
      >
        <FileDown className="h-3 w-3" />
        CSV
      </button>
      <button
        onClick={() => exportApi.downloadXlsx(messageId)}
        disabled={isStreaming}
        title="Download as Excel"
        className={cn(
          'flex items-center gap-1 rounded px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide text-primary transition-colors hover:bg-accent hover:text-accent-foreground',
          isStreaming && 'cursor-not-allowed opacity-40',
        )}
      >
        <FileSpreadsheet className="h-3 w-3" />
        Excel
      </button>
      {activeChart && (
        <>
          <button
            onClick={exportPng}
            title="Export as PNG"
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <Download className="h-3 w-3" />
            PNG
          </button>
          <button
            onClick={exportSvg}
            title="Export as SVG"
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <Download className="h-3 w-3" />
            SVG
          </button>
          <button
            onClick={() => setEditorOpen((o) => !o)}
            title={editorOpen ? 'Hide settings' : 'Show settings'}
            className={cn(
              'flex items-center gap-1 rounded px-2 py-1 text-[11px] transition-colors',
              editorOpen
                ? 'bg-muted text-foreground'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )}
          >
            <Settings2 className="h-3 w-3" />
            Settings
          </button>
        </>
      )}
      <button
        onClick={handleShare}
        disabled={isStreaming || shareState === 'loading'}
        title={shareState === 'copied' ? 'Link copied!' : 'Copy shareable link'}
        className={cn(
          'flex items-center gap-1 rounded px-2 py-1 text-[11px] transition-colors',
          shareState === 'copied'
            ? 'text-success'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground',
          (isStreaming || shareState === 'loading') && 'cursor-not-allowed opacity-40',
        )}
      >
        {shareState === 'copied' ? (
          <Check className="h-3 w-3" />
        ) : (
          <Share2 className="h-3 w-3" />
        )}
        {shareState === 'copied' ? 'Copied' : 'Share'}
      </button>

      {/* Expand / minimize */}
      <button
        onClick={() => setIsExpanded((e) => !e)}
        title={expanded ? 'Minimize (Esc)' : 'Expand to full screen'}
        className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        {expanded ? (
          <Minimize2 className="h-3 w-3" />
        ) : (
          <Maximize2 className="h-3 w-3" />
        )}
      </button>
    </div>
  );

  /** Table content — rendered in both inline and expanded contexts. */
  const renderTable = () => (
    <>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border bg-table-header">
              {displayedResults.columns.map((col) => (
                <th
                  key={col}
                  onClick={() => handleSort(col)}
                  className={cn(
                    'group whitespace-nowrap px-3 py-2 text-left font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground',
                    'cursor-pointer select-none hover:bg-table-header/80',
                  )}
                >
                  <span className="flex items-center gap-1">
                    {col}
                    {isSorting && sortCol === col ? (
                      <RefreshCw className="h-3 w-3 animate-spin text-primary" />
                    ) : sortCol === col ? (
                      sortDir === 'ASC' ? (
                        <ChevronUp className="h-3 w-3 text-foreground" />
                      ) : (
                        <ChevronDown className="h-3 w-3 text-foreground" />
                      )
                    ) : (
                      <ChevronsUpDown className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-50" />
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayedResults.rows.map((row, i) => (
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
      <div className="flex items-center justify-between border-t border-border bg-muted/50 px-3 py-1.5">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          {displayedResults.row_count} {displayedResults.row_count !== 1 ? 'rows' : 'row'}
          {results.truncated && ' · truncated'}
          {smartSortActive && ' · sorted by db'}
        </span>
        {results.bytes_scanned != null && (
          <span className="font-mono text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {formatBytes(results.bytes_scanned)} scanned
          </span>
        )}
      </div>
      {sortError && (
        <p className="border-t border-border bg-destructive/10 px-3 py-1.5 text-[11px] text-destructive">
          {sortError}
        </p>
      )}
    </>
  );

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <>
      {/* ── Inline view ──────────────────────────────────────────────────────── */}

      {/* Tab bar — always visible inline */}
      <div className="flex items-center justify-between border-t border-border bg-muted/40 px-2 py-1">
        {renderTabs()}
        {renderActions(false)}
      </div>

      {/* Inline content — hidden when expanded to full screen */}
      {!isExpanded && (
        activeTab === 'table' ? (
          renderTable()
        ) : activeTab === 'stats' ? (
          <ColumnStatsView results={displayedResults} />
        ) : activeChart ? (
          <>
            {displayedResults.truncated && activeChart.aggregation !== 'none' && (
              <div className="border-b border-border bg-amber-50/60 px-4 py-1.5 text-xs text-amber-700 dark:bg-amber-950/30 dark:text-amber-400">
                Results are truncated — aggregation applies to displayed rows only.
              </div>
            )}
            <ChartView
              results={displayedResults}
              chart={activeChart}
              containerRef={chartContainerRef}
            />
            {editorOpen && (
              <ChartEditor
                chart={activeChart}
                columns={results.columns}
                onChange={updateChart}
              />
            )}
          </>
        ) : null
      )}

      {/* Collapsed placeholder shown beneath the tab bar when expanded */}
      {isExpanded && (
        <div className="flex items-center justify-center border-t border-border bg-muted/20 py-3">
          <span className="text-[11px] text-muted-foreground">
            Results open in full screen — press <kbd className="rounded border border-border bg-muted px-1 py-0.5 font-mono text-[10px]">Esc</kbd> to close
          </span>
        </div>
      )}

      {/* ── Expanded full-screen overlay (portal) ────────────────────────────── */}
      {isExpanded &&
        createPortal(
          <div className="fixed inset-0 z-50 flex flex-col bg-background">
            {/* Overlay tab bar */}
            <div className="flex shrink-0 items-center justify-between border-b border-border bg-muted/40 px-2 py-1">
              {renderTabs()}
              {renderActions(true)}
            </div>

            {/* Scrollable content */}
            <div className="flex-1 overflow-auto">
              {activeTab === 'table' ? (
                renderTable()
              ) : activeTab === 'stats' ? (
                <ColumnStatsView results={results} />
              ) : activeChart ? (
                <>
                  {results.truncated && activeChart.aggregation !== 'none' && (
                    <div className="border-b border-border bg-amber-50/60 px-4 py-1.5 text-xs text-amber-700 dark:bg-amber-950/30 dark:text-amber-400">
                      Results are truncated — aggregation applies to displayed rows only.
                    </div>
                  )}
                  <ChartView
                    results={results}
                    chart={activeChart}
                    containerRef={expandedChartContainerRef}
                  />
                  {editorOpen && (
                    <ChartEditor
                      chart={activeChart}
                      columns={results.columns}
                      onChange={updateChart}
                    />
                  )}
                </>
              ) : null}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
