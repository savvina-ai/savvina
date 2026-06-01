// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Report builder — select query results from sessions and generate a PDF report. */

import { Component, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { FileText, ChevronDown, ChevronRight, ChevronUp, Check, X, BarChart2 } from 'lucide-react';
import { toPng } from 'html-to-image';

import { useAppStore } from '../store/appStore';
import { useSessions } from '../hooks/useChat';
import { useConnections } from '../hooks/useConnections';
import { chatApi } from '../api/chat';
import { exportApi } from '../api/export';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import ChartView from '@/components/ChartView';
import type { ChatMessage, QueryResults } from '../types';
import type { UserChart } from '../types/charts';
import { CHART_TYPES, CHART_TYPE_ICONS, CHART_TYPE_LABELS } from '../types/charts';

// ── Error boundary so a broken chart doesn't kill the card ────────────────────
class ChartErrorBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state = { error: null };
  static getDerivedStateFromError(e: Error) {
    return { error: e.message };
  }
  componentDidCatch(_e: Error, _info: ErrorInfo) {}
  render() {
    if (this.state.error) {
      return (
        <p className="px-3 py-4 text-xs text-destructive">
          Chart preview failed: {this.state.error}
        </p>
      );
    }
    return this.props.children;
  }
}

// ── Types ─────────────────────────────────────────────────────────────────────
interface ReportSection {
  messageId: string;
  heading: string;
  preview: string;
  results: QueryResults | null;
  chart: UserChart | null;
}


function randomId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function makeDefaultChart(columns: string[]): UserChart {
  return {
    id: randomId(),
    title: '',
    type: 'bar',
    xKey: columns[0] ?? '',
    yKeys: columns.length > 1 ? [columns[1]] : [],
    seriesGroup: null,
    aggregation: 'none',
    stacked: false,
    showLegend: true,
    legendPosition: 'bottom',
    showDataLabels: false,
    yAxisScale: 'linear',
    yAxisMin: null,
    yAxisMax: null,
    xAxisAngle: 0,
    filterNullX: false,
    connectNulls: false,
    comboSeriesTypes: {},
    filters: [],
    showTrendLine: false,
    trendLineType: 'linear',
    movingAvgWindow: 3,
    numberFormat: 'decimal',
    currencySymbol: '$',
    comparisonValue: null,
    comparisonLabel: '',
    gaugeMin: 0,
    gaugeMax: 100,
    gaugeThresholds: [],
  };
}

// ── Shared select style ───────────────────────────────────────────────────────
const sel =
  'w-full rounded border border-border bg-background px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring';

// ── Toggle pill button ────────────────────────────────────────────────────────
function Toggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded px-2 py-1 text-xs font-medium transition-colors',
        active
          ? 'bg-brand-gradient text-white shadow-gradient-btn'
          : 'border border-border text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      {children}
    </button>
  );
}

// ── Chart configurator ────────────────────────────────────────────────────────
function ChartConfig({
  chart,
  columns,
  results,
  onChange,
}: {
  chart: UserChart;
  columns: string[];
  results: QueryResults | null;
  onChange: (c: UserChart) => void;
}) {
  const set = <K extends keyof UserChart>(key: K, val: UserChart[K]) =>
    onChange({ ...chart, [key]: val });

  const isPolar = chart.type === 'pie';
  const isScalar = chart.type === 'number' || chart.type === 'gauge';
  const canStack = chart.type === 'bar' || chart.type === 'area' || chart.type === 'combo';
  const canConnectNulls = chart.type === 'line' || chart.type === 'area' || chart.type === 'combo';
  const showDataLabelsOpt = !isScalar && chart.type !== 'pie' && chart.type !== 'scatter';

  // Detect numeric columns by sampling up to 5 non-null values per column
  const numericCols = new Set<string>(
    columns.filter((col) => {
      if (!results || results.rows.length === 0) return true; // can't tell, show all
      const colIdx = results.columns.indexOf(col);
      const samples = results.rows
        .slice(0, 20)
        .map((r) => r[colIdx])
        .filter((v) => v !== null && v !== undefined && v !== '');
      if (samples.length === 0) return false;
      return samples.some((v) => !isNaN(parseFloat(String(v))));
    }),
  );

  // multi-Y: toggle a column in/out of yKeys
  const toggleY = (col: string) => {
    const next = chart.yKeys.includes(col)
      ? chart.yKeys.filter((k) => k !== col)
      : [...chart.yKeys, col];
    set('yKeys', next);
  };

  return (
    <div className="space-y-3 px-3 pt-3 pb-2">

      {/* ── Chart type ── */}
      <div className="flex flex-wrap gap-1">
        {CHART_TYPES.map((type) => {
          const Icon = CHART_TYPE_ICONS[type];
          return (
            <Toggle key={type} active={chart.type === type} onClick={() => set('type', type)}>
              <span className="flex items-center gap-1">
                <Icon className="h-3 w-3" />
                {CHART_TYPE_LABELS[type]}
              </span>
            </Toggle>
          );
        })}
      </div>

      {/* ── Chart title ── */}
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">Chart title (optional)</Label>
        <input
          value={chart.title}
          onChange={(e) => set('title', e.target.value)}
          placeholder="e.g. Salary by Employee"
          className="w-full rounded border border-border bg-background px-2 py-1 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </div>

      {/* ── Axes ── */}
      {isScalar ? (
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">Value column</Label>
          <select
            value={chart.yKeys[0] ?? ''}
            onChange={(e) => set('yKeys', e.target.value ? [e.target.value] : [])}
            className={sel}
          >
            <option value="">— select —</option>
            {columns.filter((c) => numericCols.has(c)).map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground">
              {isPolar ? 'Category (slice label)' : 'X axis (category)'}
            </Label>
            <select value={chart.xKey} onChange={(e) => set('xKey', e.target.value)} className={sel}>
              {columns.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground">
              {isPolar ? 'Value (slice size)' : 'Y axis — primary'}
            </Label>
            <select
              value={chart.yKeys[0] ?? ''}
              onChange={(e) => {
                const rest = chart.yKeys.slice(1);
                set('yKeys', e.target.value ? [e.target.value, ...rest] : rest);
              }}
              className={sel}
            >
              <option value="">— select —</option>
              {columns.filter((c) => numericCols.has(c)).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
        </div>
      )}

      {/* ── Additional Y series (non-pie, non-scalar only) ── */}
      {!isPolar && !isScalar && columns.length > 2 && (
        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">Additional Y series</Label>
          <div className="flex flex-wrap gap-1">
            {columns
              .filter((c) => numericCols.has(c) && c !== chart.xKey && c !== chart.yKeys[0])
              .map((c) => (
                <Toggle key={c} active={chart.yKeys.includes(c)} onClick={() => toggleY(c)}>
                  {c}
                </Toggle>
              ))}
          </div>
        </div>
      )}

      {/* ── Options row ── */}
      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-2">
        {/* Legend */}
        <div className="flex items-center gap-1.5">
          <Toggle active={chart.showLegend} onClick={() => set('showLegend', !chart.showLegend)}>
            Legend
          </Toggle>
          {chart.showLegend && (
            <select
              value={chart.legendPosition}
              onChange={(e) => set('legendPosition', e.target.value as UserChart['legendPosition'])}
              className="rounded border border-border bg-background px-1.5 py-1 text-xs text-foreground focus:outline-none"
            >
              {(['top', 'bottom', 'left', 'right'] as const).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          )}
        </div>

        {/* Stack (bar / area / combo only) */}
        {canStack && (
          <Toggle active={chart.stacked} onClick={() => set('stacked', !chart.stacked)}>
            Stacked
          </Toggle>
        )}

        {/* Data labels */}
        {showDataLabelsOpt && (
          <Toggle active={chart.showDataLabels} onClick={() => set('showDataLabels', !chart.showDataLabels)}>
            Data labels
          </Toggle>
        )}

        {/* Connect nulls (line / area / combo) */}
        {canConnectNulls && (
          <Toggle active={chart.connectNulls} onClick={() => set('connectNulls', !chart.connectNulls)}>
            Connect nulls
          </Toggle>
        )}
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function ReportBuilderPage() {
  const activeConnectionId = useAppStore((s) => s.activeConnectionId);
  const { data: connections } = useConnections();
  const [connectionId, setConnectionId] = useState(activeConnectionId);
  const { data: sessions = [], isLoading: sessionsLoading } = useSessions(connectionId);

  const [expandedSession, setExpandedSession] = useState<string | null>(null);
  const [sessionMessages, setSessionMessages] = useState<Record<string, ChatMessage[]>>({});
  const [loadingSessions, setLoadingSessions] = useState<Set<string>>(new Set());

  const [title, setTitle] = useState('Data Report');
  const [sections, setSections] = useState<ReportSection[]>([]);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);

  const chartCaptureRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const captureRef = (id: string) => (el: HTMLDivElement | null) => {
    chartCaptureRefs.current[id] = el;
  };

  const toggleSession = async (sessionId: string) => {
    if (expandedSession === sessionId) {
      setExpandedSession(null);
      return;
    }
    setExpandedSession(sessionId);
    if (!sessionMessages[sessionId]) {
      setLoadingSessions((s) => new Set(s).add(sessionId));
      try {
        const msgs = await chatApi.getHistory(sessionId);
        setSessionMessages((prev) => ({ ...prev, [sessionId]: msgs }));
      } finally {
        setLoadingSessions((s) => {
          const next = new Set(s);
          next.delete(sessionId);
          return next;
        });
      }
    }
  };

  const addSection = (msg: ChatMessage) => {
    if (sections.some((s) => s.messageId === msg.id)) return;
    setSections((prev) => [
      ...prev,
      {
        messageId: msg.id,
        heading: msg.content,
        preview: msg.query_generated?.slice(0, 60) ?? '',
        results: msg.results_json,
        chart: null,
      },
    ]);
  };

  const removeSection = (messageId: string) =>
    setSections((prev) => prev.filter((s) => s.messageId !== messageId));

  const updateSection = (messageId: string, patch: Partial<ReportSection>) =>
    setSections((prev) =>
      prev.map((s) => (s.messageId === messageId ? { ...s, ...patch } : s)),
    );

  const moveSection = (idx: number, dir: -1 | 1) => {
    const target = idx + dir;
    if (target < 0 || target >= sections.length) return;
    setSections((prev) => {
      const next = [...prev];
      [next[idx], next[target]] = [next[target], next[idx]];
      return next;
    });
  };

  const handleGenerate = async () => {
    if (sections.length === 0 || !title.trim()) return;
    setGenerating(true);
    setGenerateError(null);
    try {
      const chartImages: Record<string, string> = {};
      const wasDark = document.documentElement.classList.contains('dark');
      if (wasDark) document.documentElement.classList.replace('dark', 'light');
      try {
        for (const sec of sections) {
          if (sec.chart?.xKey && sec.chart.yKeys.length > 0) {
            const el = chartCaptureRefs.current[sec.messageId];
            if (el) {
              try {
                const externalLinks = Array.from(
                  document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]'),
                ).filter((l) => l.href && !l.href.startsWith(window.location.origin));
                externalLinks.forEach((l) => { l.disabled = true; });
                try {
                  chartImages[sec.messageId] = await toPng(el, {
                    backgroundColor: '#ffffff',
                    skipFonts: true,
                  });
                } finally {
                  externalLinks.forEach((l) => { l.disabled = false; });
                }
              } catch {
                // non-fatal — PDF section will just not have a chart image
              }
            }
          }
        }
      } finally {
        if (wasDark) document.documentElement.classList.replace('light', 'dark');
      }
      await exportApi.downloadReport(
        title.trim(),
        sections.map((s) => ({
          message_id: s.messageId,
          heading: s.heading,
          chart_image: chartImages[s.messageId],
          chart_title: s.chart?.title || undefined,
        })),
      );
    } catch {
      setGenerateError('PDF generation failed — please try again');
    } finally {
      setGenerating(false);
    }
  };

  const assistantMsgs = (sessionId: string) =>
    (sessionMessages[sessionId] ?? []).filter(
      (m) => m.role === 'assistant' && m.results_json && m.status !== 'error',
    );

  // Null ref object used where ChartView requires containerRef but we don't need it
  const nullRef = { current: null } as React.RefObject<HTMLDivElement>;

  return (
    <div className="flex-1 overflow-auto">
      {/* Off-screen chart capture at fixed width for consistent PDF output */}
      <div style={{ position: 'fixed', left: -9999, top: 0, pointerEvents: 'none' }} aria-hidden>
        {sections.map((sec) =>
          sec.chart && sec.results ? (
            <div
              key={sec.messageId}
              ref={captureRef(sec.messageId)}
              style={{ width: 760, height: 360, background: '#ffffff' }}
            >
              {/* Strip title from PNG — backend renders it as proper PDF text.
                 Disable animation so labels are present immediately when toPng captures. */}
              <ChartView results={sec.results} chart={{ ...sec.chart, title: '' }} containerRef={nullRef} animate={false} />
            </div>
          ) : null,
        )}
      </div>

      <div className="mx-auto max-w-5xl px-4 py-8 pb-24">
        <div className="mb-6 flex items-center gap-3">
          <FileText className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-xl font-semibold text-foreground">Report Builder</h1>
        </div>

        {/* Connection + title */}
        <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label>Connection</Label>
            {connections && connections.length > 0 && (
              <select
                value={connectionId ?? ''}
                onChange={(e) => setConnectionId(e.target.value || null)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="">Select a connection…</option>
                {connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="report-title">Report Title</Label>
            <Input
              id="report-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Q1 Revenue Report"
            />
          </div>
        </div>

        {/* Two-column layout */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]">
          {/* Left: session browser */}
          <div className="space-y-3">
            <Label>Sessions</Label>
            {!connectionId && (
              <p className="py-4 text-center text-sm text-muted-foreground">
                Select a connection above to browse sessions.
              </p>
            )}
            {sessionsLoading && (
              <p className="py-4 text-center text-sm text-muted-foreground">Loading…</p>
            )}
            {!sessionsLoading && sessions.length === 0 && connectionId && (
              <p className="py-4 text-center text-sm text-muted-foreground">No sessions found.</p>
            )}
            {sessions.map((session) => {
              const isExpanded = expandedSession === session.id;
              const msgs = assistantMsgs(session.id);
              const isLoadingMsgs = loadingSessions.has(session.id);
              return (
                <div key={session.id} className="rounded-lg border border-border">
                  <button
                    onClick={() => toggleSession(session.id)}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-foreground hover:bg-muted/50"
                  >
                    {isExpanded ? (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    )}
                    <span className="flex-1 truncate font-medium">{session.title}</span>
                    <span className="text-xs text-muted-foreground">
                      {new Date(session.updated_at).toLocaleDateString()}
                    </span>
                  </button>
                  {isExpanded && (
                    <div className="border-t border-border px-3 py-2 space-y-0.5">
                      {isLoadingMsgs && (
                        <p className="text-xs text-muted-foreground">Loading messages…</p>
                      )}
                      {!isLoadingMsgs && msgs.length === 0 && (
                        <p className="text-xs text-muted-foreground">
                          No results in this session.
                        </p>
                      )}
                      {msgs.map((msg) => {
                        const selected = sections.some((s) => s.messageId === msg.id);
                        return (
                          <button
                            key={msg.id}
                            onClick={() => (selected ? removeSection(msg.id) : addSection(msg))}
                            className={cn(
                              'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors',
                              selected
                                ? 'bg-primary/10 text-primary'
                                : 'text-foreground hover:bg-muted/50',
                            )}
                          >
                            {selected && <Check className="h-3 w-3 shrink-0" />}
                            <span className="flex-1 truncate">
                              {msg.content.slice(0, 80) || 'Query result'}
                            </span>
                            {msg.results_json && (
                              <span className="shrink-0 font-mono text-muted-foreground">
                                {msg.results_json.row_count} rows
                              </span>
                            )}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Right: sections */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <Label>Sections ({sections.length})</Label>
              {sections.length > 0 && (
                <div className="flex flex-col items-end gap-1">
                  {generateError && (
                    <p className="text-xs text-destructive">{generateError}</p>
                  )}
                  <Button
                    variant="gradient"
                    size="sm"
                    disabled={!title.trim() || generating}
                    onClick={handleGenerate}
                  >
                    {generating ? 'Generating…' : `Generate PDF (${sections.length})`}
                  </Button>
                </div>
              )}
            </div>

            {sections.length === 0 && (
              <p className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
                Select query results from sessions on the left to add them as report sections.
              </p>
            )}

            {sections.map((sec, idx) => {
              const columns = sec.results?.columns ?? [];
              const hasChart = sec.chart !== null;
              const chartReady =
                hasChart && !!sec.chart?.xKey && (sec.chart?.yKeys.length ?? 0) > 0;

              return (
                <div key={sec.messageId} className="rounded-lg border border-border bg-card">
                  {/* ── Header row ── */}
                  <div className="flex items-stretch">
                    {/* Order badge + move buttons */}
                    <div className="flex w-10 shrink-0 flex-col items-center justify-between border-r border-border bg-muted/30 py-1 rounded-l-lg">
                      <button
                        onClick={() => moveSection(idx, -1)}
                        disabled={idx === 0}
                        title="Move up"
                        className="flex h-6 w-full items-center justify-center text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-20 disabled:hover:bg-transparent"
                      >
                        <ChevronUp className="h-3.5 w-3.5" />
                      </button>
                      <span className="text-xs font-bold tabular-nums text-muted-foreground">
                        {idx + 1}
                      </span>
                      <button
                        onClick={() => moveSection(idx, 1)}
                        disabled={idx === sections.length - 1}
                        title="Move down"
                        className="flex h-6 w-full items-center justify-center text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-20 disabled:hover:bg-transparent"
                      >
                        <ChevronDown className="h-3.5 w-3.5" />
                      </button>
                    </div>

                    {/* Heading + preview + actions */}
                    <div className="flex min-w-0 flex-1 items-start gap-2 p-3">
                      <div className="min-w-0 flex-1">
                        <input
                          value={sec.heading}
                          onChange={(e) => updateSection(sec.messageId, { heading: e.target.value })}
                          className="w-full bg-transparent text-sm font-medium text-foreground focus:outline-none"
                          placeholder="Section heading"
                        />
                        {sec.preview && (
                          <p className="truncate font-mono text-[10px] text-muted-foreground">
                            {sec.preview}
                          </p>
                        )}
                      </div>

                      {/* Actions */}
                      <div className="flex shrink-0 items-center gap-1.5">
                        {columns.length > 0 && (
                          <button
                            type="button"
                            onClick={() =>
                              updateSection(sec.messageId, {
                                chart: hasChart ? null : makeDefaultChart(columns),
                              })
                            }
                            className={cn(
                              'flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors',
                              hasChart
                                ? 'border-primary bg-primary/10 text-primary hover:bg-primary/20'
                                : 'border-border text-muted-foreground hover:border-primary/50 hover:bg-muted hover:text-foreground',
                            )}
                          >
                            <BarChart2 className="h-3.5 w-3.5" />
                            {hasChart ? 'Chart on' : 'Add chart'}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => removeSection(sec.messageId)}
                          className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>

                  {/* ── Chart panel (only when chart is enabled) ── */}
                  {hasChart && sec.chart && (
                    <div className="border-t border-border bg-muted/20">
                      <ChartConfig
                        chart={sec.chart}
                        columns={columns}
                        results={sec.results}
                        onChange={(c) => updateSection(sec.messageId, { chart: c })}
                      />

                      {/* Live preview inside a fixed-height box so ResponsiveContainer can measure */}
                      <div className="mx-3 mb-3 overflow-hidden rounded border border-border bg-background">
                        {chartReady && sec.results ? (
                          <ChartErrorBoundary key={`${sec.messageId}-${sec.chart.type}-${sec.chart.xKey}`}>
                            <ChartView
                              results={sec.results}
                              chart={sec.chart}
                              containerRef={nullRef}
                            />
                          </ChartErrorBoundary>
                        ) : (
                          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                            Select X and Y axis columns above to preview the chart.
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

          </div>
        </div>
      </div>
    </div>
  );
}
