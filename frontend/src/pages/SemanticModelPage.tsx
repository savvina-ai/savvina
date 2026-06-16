// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * SemanticModelPage v2 — enhanced with v2 semantic layer features:
 * drift warnings, relationship graph, derived columns, time expressions,
 * semantic type badges, and the pending suggestion feedback loop.
 */

import { useState, useEffect, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Check, ChevronRight, Pencil, Plus, X } from 'lucide-react';

import { semanticApi } from '../api/semantic';
import apiClient from '../api/client';
import SemanticModelEditor from '../components/SemanticModelEditor';
import BusinessMetricEditor from '../components/BusinessMetricEditor';
import { useProviders } from '../hooks/useProviders';
import { useConnections } from '../hooks/useConnections';
import { useAuthStore } from '../store/authStore';
import { cn } from '@/lib/utils';
import type { SemanticModel, RelationshipEdge, DerivedColumn } from '../types';

interface DriftReport {
  connection_id: string;
  warnings: string[];
  warning_count: number;
  checked_at: string;
}


// ── Semantic type badge ────────────────────────────────────────────────────

const SEMANTIC_TYPE_STYLES: Record<string, string> = {
  MONETARY: 'bg-primary/15 text-primary',
  PERCENTAGE: 'bg-primary/15 text-primary',
  COUNT: 'bg-accent/50 text-accent-foreground',
  MEASUREMENT: 'bg-accent/50 text-accent-foreground',
  TIMESTAMP: 'bg-secondary/60 text-secondary-foreground',
  DATE: 'bg-secondary/60 text-secondary-foreground',
  STATUS_FLAG: 'bg-destructive/15 text-destructive',
  BOOLEAN_FLAG: 'bg-destructive/15 text-destructive',
  CATEGORICAL: 'bg-muted text-foreground',
  IDENTIFIER: 'bg-muted text-muted-foreground',
  FOREIGN_KEY: 'bg-primary/10 text-primary',
  FREE_TEXT: 'bg-muted text-muted-foreground',
  URL: 'bg-muted text-muted-foreground',
  EMAIL: 'bg-muted text-muted-foreground',
  PHONE: 'bg-muted text-muted-foreground',
  UNKNOWN: 'bg-muted text-muted-foreground',
};

function SemanticTypeBadge({ type }: { type: string }) {
  if (!type || type === 'unknown' || type === 'UNKNOWN') return null;
  const style =
    SEMANTIC_TYPE_STYLES[type.toUpperCase()] ?? 'bg-muted text-muted-foreground';
  return (
    <span
      className={cn(
        'rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide',
        style,
      )}
    >
      {type.toLowerCase().replace(/_/g, ' ')}
    </span>
  );
}

// ── Column intelligence panel (read-only summary per table) ───────────────

function ColumnIntelligencePanel({ model }: { model: SemanticModel }) {
  const [openTable, setOpenTable] = useState<string | null>(null);

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        Auto-inferred semantic types from catalog statistics. Edit in the Tables tab.
      </p>
      {Object.entries(model.tables).map(([tableKey, table]) => {
        const cols = table.columns as Record<
          string,
          { display_name: string; semantic_type?: string; cardinality?: string }
        >;
        const typedCount = Object.values(cols).filter(
          (c) => c.semantic_type && c.semantic_type !== 'unknown',
        ).length;
        return (
          <div key={tableKey} className="rounded-lg border border-border overflow-hidden">
            <button
              onClick={() => setOpenTable((t) => (t === tableKey ? null : tableKey))}
              className="flex w-full items-center gap-3 bg-muted px-4 py-2.5 text-left"
            >
              <span className="text-xs text-muted-foreground">
                {openTable === tableKey ? '▼' : '▶'}
              </span>
              <span className="font-mono text-sm text-foreground">{tableKey}</span>
              <span className="text-sm text-foreground">{table.display_name}</span>
              <span className="ml-auto text-xs text-muted-foreground">
                {typedCount}/{Object.keys(cols).length} typed
              </span>
            </button>
            {openTable === tableKey && (
              <div className="divide-y divide-border border-t border-border">
                {Object.entries(cols).map(([colName, col]) => (
                  <div
                    key={colName}
                    className="flex items-center gap-3 px-4 py-2"
                  >
                    <span className="w-40 shrink-0 font-mono text-xs text-foreground">
                      {colName}
                    </span>
                    <span className="flex-1 text-xs text-muted-foreground">
                      {col.display_name}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {col.semantic_type && (
                        <SemanticTypeBadge type={col.semantic_type} />
                      )}
                      {col.cardinality && (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {col.cardinality}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Generation confirm dialog ─────────────────────────────────────────────

interface ConfirmDialogProps {
  onConfirm: () => void;
  onCancel: () => void;
}

function GenerateConfirmDialog({ onConfirm, onCancel }: ConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
      <div className="mx-4 max-w-md rounded-xl border border-border bg-card p-6 shadow-lg">
        <h3 className="mb-2 text-base font-semibold text-foreground">
          Regenerate Semantic Model?
        </h3>
        <p className="mb-3 text-sm text-muted-foreground">
          This will call the LLM to regenerate the full semantic model from your current
          schema. Any manual edits will be overwritten.
        </p>
        <ul className="mb-4 space-y-1 text-xs text-muted-foreground">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            Column fingerprinting reads catalog statistics from your database (no user
            data rows).
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            LLM is invoked once to generate display names, descriptions, and derived
            columns.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            Relationship edges are built from FK catalog only — no extra queries.
          </li>
        </ul>
        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white shadow-gradient-btn hover:opacity-90"
          >
            Regenerate
          </button>
        </div>
      </div>
    </div>
  );
}


// ── API error helper ──────────────────────────────────────────────────────

function apiErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const axErr = err as { response?: { data?: { detail?: string } } };
    return axErr.response?.data?.detail ?? String(err);
  }
  return String(err);
}

// ── Derived column inline form ────────────────────────────────────────────

interface DerivedFormState {
  name: string;
  sql_expression: string;
  description: string;
  format_hint: string;
  base_tables_str: string;
}

interface DerivedColumnFormProps {
  form: DerivedFormState;
  onChange: (f: DerivedFormState) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

export function DerivedColumnForm({ form, onChange, onConfirm, onCancel }: DerivedColumnFormProps) {
  const inputClass =
    'w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring';
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => onChange({ ...form, name: e.target.value })}
            placeholder="revenue_ytd"
            className={inputClass}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">Format Hint</label>
          <select
            value={form.format_hint}
            onChange={(e) => onChange({ ...form, format_hint: e.target.value })}
            className={inputClass}
          >
            <option value="">None</option>
            <option value="percentage">percentage</option>
            <option value="currency_usd">currency_usd</option>
            <option value="currency_eur">currency_eur</option>
            <option value="integer">integer</option>
          </select>
        </div>
      </div>
      <div>
        <label className="mb-1 block text-xs text-muted-foreground">SQL Expression</label>
        <input
          type="text"
          value={form.sql_expression}
          onChange={(e) => onChange({ ...form, sql_expression: e.target.value })}
          placeholder="SUM(amount) FILTER (WHERE ...)"
          className={inputClass}
        />
      </div>
      <div>
        <label className="mb-1 block text-xs text-muted-foreground">Description</label>
        <input
          type="text"
          value={form.description}
          onChange={(e) => onChange({ ...form, description: e.target.value })}
          placeholder="What this column calculates"
          className={inputClass}
        />
      </div>
      <div>
        <label className="mb-1 block text-xs text-muted-foreground">
          Base Tables (comma-separated)
        </label>
        <input
          type="text"
          value={form.base_tables_str}
          onChange={(e) => onChange({ ...form, base_tables_str: e.target.value })}
          placeholder="schema.table1, schema.table2"
          className={inputClass}
        />
      </div>
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          disabled={!form.name || !form.sql_expression}
          className="rounded-md bg-brand-gradient px-3 py-1.5 text-sm font-medium text-white shadow-gradient-btn hover:opacity-90 disabled:opacity-50"
        >
          Confirm
        </button>
      </div>
    </div>
  );
}

// ── Tab type ──────────────────────────────────────────────────────────────

type Tab = 'tables' | 'intelligence' | 'metrics' | 'joins' | 'relationships' | 'derived' | 'time';

// ── Main component ────────────────────────────────────────────────────────

export default function SemanticModelPageV2() {
  const { connectionId } = useParams<{ connectionId: string }>();
  const queryClient = useQueryClient();
  const { data: connections } = useConnections();
  const { data: providers } = useProviders();
  const currentUser = useAuthStore((s) => s.user);
  const canGenerate = !!currentUser;
  const [activeTab, setActiveTab] = useState<Tab>('tables');
  const [selectedProvider, setSelectedProvider] = useState('');
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [draft, setDraft] = useState<SemanticModel | null>(null);
  const [showConfirm, setShowConfirm] = useState(false);
  const [genProgress, setGenProgress] = useState<{
    tables_done: number;
    tables_total: number;
    batch_size: number;
  } | null>(null);

  // Relationship add form
  const [showAddRel, setShowAddRel] = useState(false);
  const [newRel, setNewRel] = useState({
    from_table: '',
    from_column: '',
    to_table: '',
    to_column: '',
    description: '',
  });

  // Derived column edit form
  const emptyDerived: DerivedFormState = {
    name: '',
    sql_expression: '',
    description: '',
    format_hint: '',
    base_tables_str: '',
  };
  const [editingDerivedIdx, setEditingDerivedIdx] = useState<number | 'new' | null>(null);
  const [derivedForm, setDerivedForm] = useState<DerivedFormState>(emptyDerived);

  // Primary model query
  const {
    data: model,
    isLoading,
    error,
  } = useQuery<SemanticModel>({
    queryKey: ['semantic', connectionId],
    queryFn: () => semanticApi.get(connectionId!) as Promise<SemanticModel>,
    enabled: !!connectionId,
  });

  // Drift report — manual trigger only
  const {
    data: driftReport,
    refetch: checkDrift,
    isFetching: driftChecking,
  } = useQuery<DriftReport>({
    queryKey: ['semantic-drift', connectionId],
    queryFn: () =>
      apiClient
        .get<DriftReport>(`/api/v1/connections/${connectionId}/semantic/drift`)
        .then((r) => r.data),
    enabled: false,
  });


  // Populate draft on first load
  useEffect(() => {
    if (model && !draft) setDraft(model);
  }, [model]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-select first configured provider when none is selected
  const configuredProviders = useMemo(
    () => providers?.filter((p) => p.is_configured) ?? [],
    [providers],
  );
  useEffect(() => {
    if (configuredProviders.length > 0 && !selectedProvider) {
      setSelectedProvider(configuredProviders[0].id ?? configuredProviders[0].provider_type);
    }
  }, [configuredProviders, selectedProvider]);

  const generate = useMutation({
    mutationFn: async (_args: { resumeFromBatch?: number } = {}) => {
      const providerParam = selectedProvider || undefined;

      // Always call init — it sets generation_status=TABLES_PARTIAL in the DB,
      // which the batch endpoint requires. Skipping it on resume causes 400s
      // because the DB state may have drifted from the React Query cache.
      const init = await semanticApi.generateInit(connectionId!, providerParam);
      const { batch_count: batchCount, tables_total: tablesTotal, batch_size: batchSize } = init;

      setGenProgress({ tables_done: 0, tables_total: tablesTotal, batch_size: batchSize });

      const CONCURRENCY = 2;
      const MAX_RETRIES = 4;
      const queue = Array.from({ length: batchCount }, (_, i) => i);
      let cancelled = false;

      const runBatchWithRetry = async (i: number) => {
        for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
          if (cancelled) throw new Error('cancelled');
          try {
            return await semanticApi.generateBatch(connectionId!, i, providerParam);
          } catch (err: unknown) {
            const status = axios.isAxiosError(err) ? err.response?.status : undefined;
            const isNetworkError = axios.isAxiosError(err) && !err.response;
            const isRetryable = status === 429 || isNetworkError;
            if (!isRetryable || attempt === MAX_RETRIES - 1) {
              cancelled = true;
              throw err;
            }
            await new Promise((r) => setTimeout(r, 2000 * 2 ** attempt));
          }
        }
        return undefined;
      };

      const runWorker = async () => {
        while (queue.length > 0 && !cancelled) {
          const i = queue.shift()!;
          const updated = await runBatchWithRetry(i);
          const done = updated?.generation_progress?.tables_done;
          if (done !== undefined) {
            setGenProgress((p) => (p ? { ...p, tables_done: Math.max(p.tables_done, done) } : null));
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(CONCURRENCY, batchCount) }, runWorker));

      return semanticApi.generateGlobals(connectionId!, providerParam);
    },
    onSuccess: (m) => {
      queryClient.setQueryData(['semantic', connectionId], m);
      setDraft(m);
      setShowConfirm(false);
      setGenProgress(null);
    },
    onError: () => {
      setGenProgress(null);
      queryClient.invalidateQueries({ queryKey: ['semantic', connectionId] });
    },
  });

  const save = useMutation({
    mutationFn: () => semanticApi.update(connectionId!, draft! as SemanticModel),
    onSuccess: (m) => {
      queryClient.setQueryData(['semantic', connectionId], m);
      setSaveError(null);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    },
    onError: () => setSaveError('Failed to save — please try again'),
  });


  const connMatch = connections?.find((c) => c.id === connectionId);
  const connName = connMatch?.name ?? (connections === undefined ? 'Loading…' : connectionId);
  const displayModel = draft ?? model;
  const hasModel = !!displayModel;


  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: 'tables', label: 'Tables', count: Object.keys(displayModel?.tables ?? {}).length },
    { id: 'intelligence', label: 'Column Intelligence' },
    { id: 'metrics', label: 'Business Metrics', count: displayModel?.business_metrics?.length },
    { id: 'joins', label: 'Common Joins' },
    {
      id: 'relationships',
      label: 'Relationships',
      count: displayModel?.relationships?.length ?? 0,
    },
    {
      id: 'derived',
      label: 'Derived',
      count: displayModel?.derived_columns?.length ?? 0,
    },
    { id: 'time', label: 'Time Expressions' },
  ];

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center bg-background">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <>
      {showConfirm && (
        <GenerateConfirmDialog
          onConfirm={() => {
            setShowConfirm(false);
            generate.mutate({});
          }}
          onCancel={() => setShowConfirm(false)}
        />
      )}

      <div className="flex-1 overflow-auto bg-background">
        <div className="mx-auto max-w-4xl space-y-6 px-4 py-8">
          {/* Header */}
          <div className="space-y-2">
            <h1 className="font-display text-2xl font-bold text-foreground">
              Semantic Model: <span className="text-primary">{connName}</span>
            </h1>
            {displayModel?.generated_at && (
              <p className="text-sm text-accent-foreground">
                ⚡ Auto-generated on{' '}
                {new Date(displayModel.generated_at).toLocaleDateString('en-US', {
                  year: 'numeric',
                  month: 'long',
                  day: 'numeric',
                })}
                {displayModel.generation_model && ` using ${displayModel.generation_model}`}
              </p>
            )}
            {displayModel?.source_dialect && displayModel.source_dialect !== 'sql' && (
              <p className="text-xs text-muted-foreground">
                Dialect:{' '}
                <span className="font-mono">{displayModel.source_dialect}</span>
              </p>
            )}
            {displayModel && (
              <label className="flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
                <input
                  type="checkbox"
                  checked={displayModel.is_user_reviewed}
                  onChange={(e) =>
                    setDraft((d) => (d ? { ...d, is_user_reviewed: e.target.checked } : d))
                  }
                  className="rounded accent-primary"
                />
                Reviewed by user
              </label>
            )}
          </div>

          {/* Info banner */}
          {displayModel && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-800/50 dark:bg-amber-950/30">
              <div className="flex items-start gap-3">
                <svg className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
                <div className="flex-1 space-y-2">
                  <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
                    This semantic model was auto-generated by AI. Always review and validate all values before use in production queries.
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    {/* Generation status */}
                    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                      displayModel.generation_status === 'complete'
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                        : displayModel.generation_status === 'tables_partial'
                        ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                        : 'bg-muted text-muted-foreground'
                    }`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${
                        displayModel.generation_status === 'complete'
                          ? 'bg-green-500'
                          : displayModel.generation_status === 'tables_partial'
                          ? 'bg-blue-500'
                          : 'bg-muted-foreground'
                      }`} />
                      {displayModel.generation_status === 'complete'
                        ? 'Generation complete'
                        : displayModel.generation_status === 'tables_partial'
                        ? 'Partial generation'
                        : 'Not generated'}
                    </span>

                    {/* Review status */}
                    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                      displayModel.is_user_reviewed
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                        : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                    }`}>
                      {displayModel.is_user_reviewed ? '✓ Reviewed' : '⚠ Needs review'}
                    </span>

                    {/* Warnings */}
                    {displayModel.generation_warnings?.length > 0 && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-900/40 dark:text-red-300">
                        {displayModel.generation_warnings.length} warning{displayModel.generation_warnings.length !== 1 ? 's' : ''}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Action bar */}
          <div className="flex flex-wrap items-center gap-3">
            {canGenerate && (
              <select
                aria-label="LLM provider"
                value={selectedProvider}
                onChange={(e) => setSelectedProvider(e.target.value)}
                className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              >
                {configuredProviders.map((p) => (
                  <option key={p.id ?? p.provider_type} value={p.id ?? p.provider_type}>
                    {p.display_name}{p.current_model ? ` · ${p.current_model}` : ''}
                  </option>
                ))}
                {configuredProviders.length === 0 && (
                  <option value="" disabled>No providers configured</option>
                )}
              </select>
            )}

            <button
              onClick={() => hasModel ? setShowConfirm(true) : generate.mutate({})}
              disabled={!canGenerate || generate.isPending || !selectedProvider}
              title={
                !canGenerate
                  ? 'Only admins can generate the semantic model'
                  : !selectedProvider
                  ? 'Configure an LLM provider in Settings first'
                  : undefined
              }
              className="rounded-md border border-primary px-4 py-1.5 text-sm text-primary transition-colors hover:bg-primary/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {generate.isPending ? 'Generating…' : hasModel ? 'Regenerate' : 'Generate'}
            </button>

            <button
              onClick={() => void checkDrift()}
              disabled={driftChecking || !displayModel}
              className="rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
            >
              {driftChecking ? 'Checking…' : 'Check Drift'}
            </button>

            {displayModel && (
              <button
                onClick={() => save.mutate()}
                disabled={save.isPending || !draft}
                className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white shadow-gradient-btn transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                {save.isPending ? 'Saving…' : 'Save Changes'}
              </button>
            )}

            {saveSuccess && (
              <span className="flex items-center gap-1 text-xs text-success">
                <Check className="h-3 w-3" /> Saved
              </span>
            )}
            {saveError && (
              <span className="text-xs text-destructive">{saveError}</span>
            )}
          </div>

          {/* Phased generation progress bar */}
          {genProgress && (
            <div className="space-y-1.5">
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>Analysing tables…</span>
                <span>{genProgress.tables_done} / {genProgress.tables_total}</span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-primary transition-all duration-300"
                  style={{
                    width: `${genProgress.tables_total > 0 ? (genProgress.tables_done / genProgress.tables_total) * 100 : 0}%`,
                  }}
                />
              </div>
            </div>
          )}

          {/* Resume banner — interrupted generation */}
          {model?.generation_status === 'tables_partial' && !generate.isPending && (
            <div className="flex items-center gap-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span>
                Generation was interrupted at table{' '}
                {model.generation_progress?.tables_done ?? 0} of{' '}
                {model.generation_progress?.tables_total ?? '?'}.
              </span>
              <button
                onClick={() =>
                  generate.mutate({
                    resumeFromBatch: model.generation_progress?.tables_done ?? 0,
                  })
                }
                className="ml-auto shrink-0 rounded bg-amber-500/20 px-2.5 py-1 text-xs font-medium hover:bg-amber-500/30"
              >
                Resume
              </button>
            </div>
          )}

          {/* Generation error */}
          {generate.error && (
            <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              Generation failed: {apiErrorMessage(generate.error)}
            </div>
          )}

          {/* Generation warnings — e.g. truncated output */}
          {(displayModel?.generation_warnings ?? []).length > 0 && (
            <div className="rounded-xl border border-warning/30 bg-warning/10 px-4 py-3">
              <div className="mb-1 flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-warning" />
                <span className="text-sm font-medium text-warning">
                  Incomplete generation
                </span>
              </div>
              <ul className="space-y-1 pl-6 text-xs text-warning/80">
                {(displayModel!.generation_warnings).map((w, i) => (
                  <li key={i} className="list-disc">{w}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Drift warning banner — from fresh drift check */}
          {(driftReport?.warnings?.length ?? 0) > 0 && (
            <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
              <div className="mb-2 flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-destructive" />
                <span className="text-sm font-medium text-destructive">
                  Schema drift detected ({driftReport!.warning_count} warning
                  {driftReport!.warning_count !== 1 ? 's' : ''})
                </span>
              </div>
              <ul className="space-y-1 pl-6 text-xs text-destructive/80">
                {driftReport!.warnings.map((w, i) => (
                  <li key={i} className="list-disc">
                    {w}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-destructive/70">
                Checked at {new Date(driftReport!.checked_at).toLocaleTimeString()} —
                Regenerate to sync.
              </p>
            </div>
          )}


          {/* Empty state */}
          {(!!error || (!displayModel && !generate.isPending)) && (
            <div className="rounded-xl border border-dashed border-border p-8 text-center">
              <p className="mb-3 text-sm text-muted-foreground">No semantic model yet.</p>
              {canGenerate ? (
                <button
                  onClick={() => generate.mutate({})}
                  disabled={generate.isPending || !selectedProvider}
                  className="rounded-md bg-brand-gradient px-6 py-2 text-sm font-medium text-white shadow-gradient-btn transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  {generate.isPending ? 'Generating…' : 'Generate Semantic Model'}
                </button>
              ) : (
                <p className="text-xs text-muted-foreground">Contact an admin to generate the semantic model.</p>
              )}
            </div>
          )}

          {/* Model content */}
          {displayModel && (
            <>
              {/* Tab bar */}
              <div role="tablist" aria-label="Semantic model sections" className="flex flex-wrap gap-1 border-b border-border">
                {tabs.map((tab) => (
                  <button
                    key={tab.id}
                    role="tab"
                    aria-selected={activeTab === tab.id}
                    aria-controls={`sem-tabpanel-${tab.id}`}
                    id={`sem-tab-${tab.id}`}
                    onClick={() => setActiveTab(tab.id)}
                    className={cn(
                      '-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors',
                      activeTab === tab.id
                        ? 'border-primary text-primary'
                        : 'border-transparent text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {tab.label}
                    {tab.count !== undefined && (
                      <span className="ml-1.5 text-xs text-muted-foreground">
                        ({tab.count})
                      </span>
                    )}
                  </button>
                ))}
              </div>

              <div
                role="tabpanel"
                id={`sem-tabpanel-${activeTab}`}
                aria-labelledby={`sem-tab-${activeTab}`}
              >

              {/* Tables — existing editor (v2 fields are preserved via spread) */}
              {activeTab === 'tables' && (
                <SemanticModelEditor
                  model={displayModel as SemanticModel}
                  onChange={(m) => setDraft((d) => (d ? { ...d, ...m } : (m as SemanticModel)))}
                />
              )}

              {/* Column intelligence — semantic types + cardinality per column */}
              {activeTab === 'intelligence' && (
                <ColumnIntelligencePanel model={displayModel} />
              )}

              {/* Business metrics */}
              {activeTab === 'metrics' && (
                <BusinessMetricEditor
                  metrics={displayModel.business_metrics ?? []}
                  onChange={(metrics) =>
                    setDraft((d) => (d ? { ...d, business_metrics: metrics } : d))
                  }
                />
              )}

              {/* Common joins */}
              {activeTab === 'joins' && (
                <div className="space-y-3">
                  {displayModel.common_joins.map((join, i) => (
                    <div
                      key={i}
                      className="space-y-2 rounded-xl border border-border bg-card p-4"
                    >
                      <p className="text-sm font-medium text-foreground">{join.description}</p>
                      <p className="text-xs text-muted-foreground">
                        Tables: {join.tables.join(', ')}
                      </p>
                      <pre className="overflow-x-auto rounded bg-muted px-3 py-2 font-mono text-xs text-muted-foreground">
                        {join.join_pattern}
                      </pre>
                    </div>
                  ))}
                  {displayModel.common_joins.length === 0 && (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No common joins defined.
                    </p>
                  )}
                </div>
              )}

              {/* FK Relationships */}
              {activeTab === 'relationships' && (
                <div className="space-y-3">
                  {(displayModel.relationships ?? []).map((rel, i) => (
                    <div
                      key={i}
                      className="space-y-2 rounded-xl border border-border bg-card p-4"
                    >
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-mono text-foreground">
                          {rel.from_table}.{rel.from_column}
                        </span>
                        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                        <span className="font-mono text-foreground">
                          {rel.to_table}.{rel.to_column}
                        </span>
                        <span className="ml-auto text-xs text-muted-foreground">
                          {rel.relationship_type}
                        </span>
                        {rel.is_required ? (
                          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                            REQUIRED
                          </span>
                        ) : (
                          <button
                            onClick={() =>
                              setDraft((d) =>
                                d
                                  ? {
                                      ...d,
                                      relationships: d.relationships.filter(
                                        (_, idx) => idx !== i,
                                      ),
                                    }
                                  : d,
                              )
                            }
                            className="rounded p-0.5 text-muted-foreground hover:text-destructive"
                            aria-label="Delete relationship"
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                      {rel.description && (
                        <p className="text-xs text-muted-foreground">{rel.description}</p>
                      )}
                      <pre className="overflow-x-auto rounded bg-muted px-3 py-2 font-mono text-xs text-muted-foreground">
                        {rel.join_sql}
                      </pre>
                    </div>
                  ))}

                  {(displayModel.relationships ?? []).length === 0 && !showAddRel && (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No formal FK relationships detected. Make sure your schema exposes
                      foreign key constraints, or add a manual relationship below.
                    </p>
                  )}

                  {/* Add manual relationship */}
                  {showAddRel ? (
                    <div className="space-y-3 rounded-xl border border-border bg-card p-4">
                      <p className="text-sm font-medium text-foreground">
                        Add Manual Relationship
                      </p>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="mb-1 block text-xs text-muted-foreground">
                            From Table
                          </label>
                          <select
                            value={newRel.from_table}
                            onChange={(e) =>
                              setNewRel((r) => ({ ...r, from_table: e.target.value }))
                            }
                            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          >
                            <option value="">Select…</option>
                            {Object.keys(displayModel.tables).map((t) => (
                              <option key={t} value={t}>
                                {t}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label className="mb-1 block text-xs text-muted-foreground">
                            From Column
                          </label>
                          <input
                            type="text"
                            value={newRel.from_column}
                            onChange={(e) =>
                              setNewRel((r) => ({ ...r, from_column: e.target.value }))
                            }
                            placeholder="column_name"
                            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-xs text-muted-foreground">
                            To Table
                          </label>
                          <select
                            value={newRel.to_table}
                            onChange={(e) =>
                              setNewRel((r) => ({ ...r, to_table: e.target.value }))
                            }
                            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          >
                            <option value="">Select…</option>
                            {Object.keys(displayModel.tables).map((t) => (
                              <option key={t} value={t}>
                                {t}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label className="mb-1 block text-xs text-muted-foreground">
                            To Column
                          </label>
                          <input
                            type="text"
                            value={newRel.to_column}
                            onChange={(e) =>
                              setNewRel((r) => ({ ...r, to_column: e.target.value }))
                            }
                            placeholder="column_name"
                            className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </div>
                      </div>
                      <div>
                        <label className="mb-1 block text-xs text-muted-foreground">
                          Description (optional)
                        </label>
                        <input
                          type="text"
                          value={newRel.description}
                          onChange={(e) =>
                            setNewRel((r) => ({ ...r, description: e.target.value }))
                          }
                          placeholder="Describes the join relationship"
                          className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                        />
                      </div>
                      <div className="flex justify-end gap-2">
                        <button
                          onClick={() => {
                            setShowAddRel(false);
                            setNewRel({
                              from_table: '',
                              from_column: '',
                              to_table: '',
                              to_column: '',
                              description: '',
                            });
                          }}
                          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => {
                            if (
                              !newRel.from_table ||
                              !newRel.from_column ||
                              !newRel.to_table ||
                              !newRel.to_column
                            )
                              return;
                            const edge: RelationshipEdge = {
                              from_table: newRel.from_table,
                              from_column: newRel.from_column,
                              to_table: newRel.to_table,
                              to_column: newRel.to_column,
                              description: newRel.description || null,
                              is_required: false,
                              relationship_type: 'many_to_one',
                              join_sql: `${newRel.from_table}.${newRel.from_column} = ${newRel.to_table}.${newRel.to_column}`,
                            };
                            setDraft((d) =>
                              d
                                ? { ...d, relationships: [...(d.relationships ?? []), edge] }
                                : d,
                            );
                            setShowAddRel(false);
                            setNewRel({
                              from_table: '',
                              from_column: '',
                              to_table: '',
                              to_column: '',
                              description: '',
                            });
                          }}
                          disabled={
                            !newRel.from_table ||
                            !newRel.from_column ||
                            !newRel.to_table ||
                            !newRel.to_column
                          }
                          className="rounded-md bg-brand-gradient px-3 py-1.5 text-sm font-medium text-white shadow-gradient-btn hover:opacity-90 disabled:opacity-50"
                        >
                          Add
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      onClick={() => setShowAddRel(true)}
                      className="flex items-center gap-1.5 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
                    >
                      <Plus className="h-4 w-4" />
                      Add Manual Relationship
                    </button>
                  )}
                </div>
              )}

              {/* Derived columns */}
              {activeTab === 'derived' && (
                <div className="space-y-3">
                  <div className="flex justify-end">
                    <button
                      onClick={() => {
                        setEditingDerivedIdx('new');
                        setDerivedForm(emptyDerived);
                      }}
                      className="flex items-center gap-1.5 rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
                    >
                      <Plus className="h-4 w-4" />
                      Add Derived Column
                    </button>
                  </div>

                  {editingDerivedIdx === 'new' && (
                    <div className="rounded-xl border border-border bg-card p-4">
                      <p className="mb-3 text-sm font-medium text-foreground">
                        New Derived Column
                      </p>
                      <DerivedColumnForm
                        form={derivedForm}
                        onChange={setDerivedForm}
                        onConfirm={() => {
                          const col: DerivedColumn = {
                            name: derivedForm.name,
                            sql_expression: derivedForm.sql_expression,
                            description: derivedForm.description,
                            format_hint: derivedForm.format_hint || null,
                            base_tables: derivedForm.base_tables_str
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean),
                            available_on: [],
                          };
                          setDraft((d) =>
                            d
                              ? { ...d, derived_columns: [...(d.derived_columns ?? []), col] }
                              : d,
                          );
                          setEditingDerivedIdx(null);
                          setDerivedForm(emptyDerived);
                        }}
                        onCancel={() => {
                          setEditingDerivedIdx(null);
                          setDerivedForm(emptyDerived);
                        }}
                      />
                    </div>
                  )}

                  {(displayModel.derived_columns ?? []).length === 0 &&
                    editingDerivedIdx !== 'new' && (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        No derived columns defined. Regenerate with a capable LLM to
                        auto-generate common calculations, or add one manually above.
                      </p>
                    )}

                  {(displayModel.derived_columns ?? []).map((col, i) => (
                    <div
                      key={i}
                      className="space-y-2 rounded-xl border border-border bg-card p-4"
                    >
                      {editingDerivedIdx === i ? (
                        <DerivedColumnForm
                          form={derivedForm}
                          onChange={setDerivedForm}
                          onConfirm={() => {
                            const updated: DerivedColumn = {
                              name: derivedForm.name,
                              sql_expression: derivedForm.sql_expression,
                              description: derivedForm.description,
                              format_hint: derivedForm.format_hint || null,
                              base_tables: derivedForm.base_tables_str
                                .split(',')
                                .map((s) => s.trim())
                                .filter(Boolean),
                              available_on: col.available_on ?? [],
                            };
                            setDraft((d) =>
                              d
                                ? {
                                    ...d,
                                    derived_columns: d.derived_columns.map((c, idx) =>
                                      idx === i ? updated : c,
                                    ),
                                  }
                                : d,
                            );
                            setEditingDerivedIdx(null);
                            setDerivedForm(emptyDerived);
                          }}
                          onCancel={() => {
                            setEditingDerivedIdx(null);
                            setDerivedForm(emptyDerived);
                          }}
                        />
                      ) : (
                        <>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-sm font-medium text-foreground">
                              {col.name}
                            </span>
                            {col.format_hint && (
                              <span className="text-xs text-muted-foreground">
                                ({col.format_hint})
                              </span>
                            )}
                            <div className="ml-auto flex items-center gap-1">
                              <button
                                onClick={() => {
                                  setEditingDerivedIdx(i);
                                  setDerivedForm({
                                    name: col.name,
                                    sql_expression: col.sql_expression,
                                    description: col.description,
                                    format_hint: col.format_hint ?? '',
                                    base_tables_str: col.base_tables.join(', '),
                                  });
                                }}
                                className="rounded p-1 text-muted-foreground hover:text-foreground"
                                aria-label="Edit derived column"
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </button>
                              <button
                                onClick={() =>
                                  setDraft((d) =>
                                    d
                                      ? {
                                          ...d,
                                          derived_columns: d.derived_columns.filter(
                                            (_, idx) => idx !== i,
                                          ),
                                        }
                                      : d,
                                  )
                                }
                                className="rounded p-1 text-muted-foreground hover:text-destructive"
                                aria-label="Delete derived column"
                              >
                                <X className="h-3.5 w-3.5" />
                              </button>
                            </div>
                          </div>
                          <p className="text-xs text-muted-foreground">{col.description}</p>
                          <pre className="overflow-x-auto rounded bg-muted px-3 py-2 font-mono text-xs text-muted-foreground">
                            {col.sql_expression}
                          </pre>
                          {col.base_tables.length > 0 && (
                            <p className="text-xs text-muted-foreground">
                              Tables: {col.base_tables.join(', ')}
                            </p>
                          )}
                          {(col.available_on ?? []).length > 0 && (
                            <p className="text-xs text-muted-foreground">
                              Available on: {(col.available_on ?? []).join(', ')}
                            </p>
                          )}
                        </>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {/* Time expressions */}
              {activeTab === 'time' && (
                <div className="space-y-3">
                  {displayModel.db_timezone && (
                    <div className="rounded-xl border border-border bg-card px-4 py-3">
                      <span className="text-xs text-muted-foreground">Database timezone: </span>
                      <span className="font-mono text-sm text-foreground">
                        {displayModel.db_timezone}
                      </span>
                    </div>
                  )}
                  {Object.keys(displayModel.time_expressions ?? {}).length === 0 ? (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No time expressions generated. Regenerate to populate dialect-specific
                      date helpers.
                    </p>
                  ) : (
                    Object.entries(displayModel.time_expressions ?? {}).map(
                      ([name, expr]) => (
                        <div
                          key={name}
                          className="flex flex-col gap-1 rounded-xl border border-border bg-card p-4 sm:flex-row sm:items-center sm:gap-4"
                        >
                          <span className="w-44 shrink-0 font-mono text-xs font-medium text-foreground">
                            {name}
                          </span>
                          <pre className="flex-1 overflow-x-auto font-mono text-xs text-muted-foreground">
                            {expr}
                          </pre>
                        </div>
                      ),
                    )
                  )}
                </div>
              )}

              </div>{/* end tabpanel */}
            </>
          )}
        </div>
      </div>
    </>
  );
}
