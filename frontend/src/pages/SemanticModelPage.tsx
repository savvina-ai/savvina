// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * SemanticModelPage v2 — enhanced with v2 semantic layer features:
 * drift warnings, relationship graph, derived columns, time expressions,
 * semantic type badges, and the pending suggestion feedback loop.
 */

import { useState, useEffect, useMemo, useRef } from 'react';
import { useParams } from 'react-router';
import {
  replaceEqualDeep,
  useIsMutating,
  useMutationState,
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Check, ChevronRight, Pencil, Plus, X } from 'lucide-react';

import { semanticApi } from '../api/semantic';
import { connectionsApi } from '../api/connections';
import { metricValidationError } from '../lib/semanticUtils';
import SemanticModelEditor from '../components/SemanticModelEditor';
import BusinessMetricEditor from '../components/BusinessMetricEditor';
import GenerationBanner from '../components/semantic/GenerationBanner';
import DriftSummaryPanel from '../components/semantic/DriftSummaryPanel';
import DriftHistory from '../components/semantic/DriftHistory';
import { summaryAnnouncement } from '../components/semantic/driftSummaryRows';
import { useProviders } from '../hooks/useProviders';
import { useConnections } from '../hooks/useConnections';
import { useAuthStore } from '../store/authStore';
import { cn } from '@/lib/utils';
import { apiErrorMessage } from '@/lib/apiError';
import type {
  ApplyDriftResponse,
  DerivedColumn,
  DriftApplyHistoryEntry,
  DriftApplySummary,
  DriftReport,
  RelationshipEdge,
  SchemaTable,
  Segment,
  SemanticModel,
  SemanticSuggestionResponse,
} from '../types';

const SUGGESTION_PENDING_REASON = 'Wait for the suggestion to finish applying';
const RUN_PENDING_REASON = 'Wait for the current run to finish';
const DRIFT_APPLY_PENDING_REASON = 'Wait for the drift apply to finish';

/** Logged Apply Drifts the page lists, newest first. */
const DRIFT_HISTORY_LIMIT = 10;

/**
 * One sentence for the status line after a successful Check Drift. `heldBecause` is why
 * Preview & apply is disabled now that the check has settled, if it is: the sentence
 * offers the button only when it can be used.
 */
function driftCheckAnnouncement(report: DriftReport, heldBecause: string | null): string {
  const cachedAt = report.schema_cached_at
    ? ` at ${new Date(report.schema_cached_at).toLocaleString()}`
    : '';
  const cached =
    report.schema_source === 'cache'
      ? ` the schema cached${cachedAt}; the database could not be reached.`
      : null;
  if (report.warning_count > 0) {
    const n = report.warning_count;
    const found = `Schema drift detected: ${n} warning${n === 1 ? '' : 's'}`;
    // Preview & apply is held on a cached report: both need the live database.
    if (cached) return `${found} against${cached}`;
    if (!report.has_actionable_changes) return `${found}.`;
    return heldBecause
      ? `${found}. Preview & apply is not available yet: ${heldBecause}.`
      : `${found}. Preview & apply is available.`;
  }
  if (!cached) return 'No drift: the model matches the live schema.';
  return `No drift against${cached}`;
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

type ConfirmAction = 'regenerate' | 'restart';

interface ConfirmDialogProps {
  /**
   * `regenerate` reconciles onto the existing model and keeps saved hand edits (the page
   * will not start it over unsaved ones); `restart`
   * deletes the model first, edits included, so it is worded and styled as destructive.
   */
  action: ConfirmAction;
  onConfirm: () => void;
  onCancel: () => void;
}

function GenerateConfirmDialog({ action, onConfirm, onCancel }: ConfirmDialogProps) {
  const restart = action === 'restart';
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="generate-confirm-title"
        className="mx-4 max-w-md rounded-xl border border-border bg-card p-6 shadow-lg"
      >
        <h3 id="generate-confirm-title" className="mb-2 text-base font-semibold text-foreground">
          {restart ? 'Restart from scratch?' : 'Regenerate Semantic Model?'}
        </h3>
        {restart ? (
          <p className="mb-3 text-sm text-muted-foreground">
            This deletes the semantic model, including your edits, and generates it again
            from your current schema. This cannot be undone.
          </p>
        ) : (
          <p className="mb-3 text-sm text-muted-foreground">
            This will call the LLM to re-annotate the model from your current schema. Saved
            edits you made by hand are kept; tables and columns no longer in the database are
            removed.
          </p>
        )}
        {/* Mirrors the phased backend: GENERATION_BATCH_SIZE tables per /generate/batch
            call, one /generate/globals call, edges from `_build_relationship_graph`. */}
        <ul className="mb-4 space-y-1 text-xs text-muted-foreground">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            Tables are annotated in batches of up to 4, one LLM call per batch.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            One final LLM call builds business metrics, common joins, derived columns and
            segments from the annotated tables.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            Relationship edges are built from your database&apos;s foreign keys only — no
            extra queries.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 shrink-0 text-primary">•</span>
            The LLM sees only the schema — table and column names, types, keys and
            comments — never row data.
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
            className={
              restart
                ? 'rounded-md bg-destructive px-4 py-1.5 text-sm font-medium text-destructive-foreground hover:opacity-90'
                : 'rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white shadow-gradient-btn hover:opacity-90'
            }
          >
            {restart ? 'Delete and regenerate' : 'Regenerate'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Suggestions ───────────────────────────────────────────────────────────

function correctionLabel(s: SemanticSuggestionResponse): string {
  switch (s.correction_type) {
    case 'add_value_mapping':
      return `Add value mapping to ${s.table_key}.${s.field}`;
    case 'update_filter':
      return `Add default filter to ${s.table_key}`;
    case 'update_description':
      return `Update description on ${s.table_key}${s.field ? `.${s.field}` : ''}`;
    default:
      return `${s.correction_type} on ${s.table_key}`;
  }
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

type Tab =
  | 'tables'
  | 'intelligence'
  | 'metrics'
  | 'joins'
  | 'relationships'
  | 'derived'
  | 'segments'
  | 'notes'
  | 'time'
  | 'suggestions';

const MAX_GENERATION_RETRIES = 4;

/** Cancellation flag shared by every call in one generation run. */
type RetryToken = { cancelled: boolean };

/**
 * Retry a generation call on a throttle, a transient outage or a dropped connection.
 *
 * 429 covers both our own per-IP limiter and an upstream provider throttle; 503 covers
 * a provider 5xx or dropped connection and a busy connection row lock. The backend maps
 * these to 429/503 rather than 422 precisely so this retry can see them (the SDKs'
 * own silent retries are off for generation). Any other status is permanent for this
 * run, and continuing would burn the remaining batches against a provider that is going
 * to refuse them too — so the first permanent failure cancels the shared token.
 *
 * Module-level rather than a closure inside the full-run mutation, because the
 * globals-only "Finish with what's generated" path must share this exact policy rather
 * than carry a second copy of it.
 */
async function withRetry<T>(call: () => Promise<T>, token: RetryToken): Promise<T | undefined> {
  for (let attempt = 0; attempt < MAX_GENERATION_RETRIES; attempt++) {
    if (token.cancelled) return undefined;
    try {
      return await call();
    } catch (err: unknown) {
      const status = axios.isAxiosError(err) ? err.response?.status : undefined;
      const isNetworkError = axios.isAxiosError(err) && !err.response;
      const isRetryable = status === 429 || status === 503 || isNetworkError;
      if (!isRetryable || attempt === MAX_GENERATION_RETRIES - 1) {
        token.cancelled = true;
        throw err;
      }
      await new Promise((r) => setTimeout(r, 2000 * 2 ** attempt));
    }
  }
  return undefined;
}

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
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  // Dismissals are intentionally session-local: the backend has no "rejected" state
  // for a suggestion, only is_applied.
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
  const [genProgress, setGenProgress] = useState<{
    tables_done: number;
    tables_total: number;
    batch_size: number;
    // Which phase the label describes. Tables is the batch loop; globals is the single
    // trailing call that builds metrics and joins over whatever tables exist.
    phase?: 'tables' | 'globals';
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

  // Primary model query. `null` data means "known to have no model": Restart writes it
  // after its DELETE, see the `generation` mutation's restart path.
  const fetchModel = () => semanticApi.get(connectionId!) as Promise<SemanticModel>;
  const {
    data: model,
    dataUpdatedAt,
    isLoading,
    error,
  } = useQuery<SemanticModel | null>({
    queryKey: ['semantic', connectionId],
    queryFn: () => fetchModel(),
    enabled: !!connectionId,
  });

  // Drift report — manual trigger only. The server introspects the live database on
  // each check (and refreshes the schema cache), so a failure is not retried.
  const {
    data: driftReport,
    error: driftError,
    refetch: refetchDrift,
    isFetching: driftChecking,
  } = useQuery<DriftReport>({
    queryKey: ['semantic-drift', connectionId],
    queryFn: () => semanticApi.getDrift(connectionId!),
    enabled: false,
    retry: false,
  });

  // What the last Apply Drift changed. The route returns it (and logs it, see Drift
  // history); it stays on screen until dismissed, the next preview or apply, or the next
  // run.
  const [driftSummary, setDriftSummary] = useState<DriftApplySummary | null>(null);
  // The preview of an Apply Drift (the same route with dry_run: nothing written, no LLM
  // call). Kept until applied, cancelled, or the next Check Drift; anything else that
  // writes the model drops it, since the apply would no longer do what it lists.
  const [driftPreview, setDriftPreview] = useState<DriftApplySummary | null>(null);
  // Why an apply started by an earlier mount of this page failed. Its own error renders in
  // the preview panel, which this mount never had. Cleared by the next check, preview or run.
  const [elsewhereApplyError, setElsewhereApplyError] = useState<string | null>(null);
  // Bumped on every drop, so a preview still in flight when its predecessor's state was
  // dropped is ignored when it lands (see previewDrift). The reset does the same for its
  // failure: this observer stops tracking that mutation, so no "Preview failed" from a
  // preview the page has moved past.
  const previewSeq = useRef(0);
  const dropPreview = () => {
    previewSeq.current += 1;
    setDriftPreview(null);
    previewDrift.reset();
    setDriftStatus('');
  };
  // Where focus goes once the control that had it unmounts: a closed panel, a preview
  // that replaced Preview & apply, an apply that took its preview with it. Set only by
  // those transitions, so no other state change moves focus (see the effect below).
  // 'panel' and 'result' focus the preview and result panels themselves: focus is what
  // gets them read, and a region takes no Space or Enter.
  const focusAfterClose = useRef<'preview' | 'panel' | 'result' | 'check' | null>(null);
  const previewButtonRef = useRef<HTMLButtonElement>(null);
  const checkDriftButtonRef = useRef<HTMLButtonElement>(null);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const resultPanelRef = useRef<HTMLDivElement>(null);
  // Speaks for a preview or result that arrived without focus moving to it: one from an
  // apply an earlier mount started, or one that landed after the user moved on. Never set
  // alongside a focus move, which reads the panel already. Cleared by the next action.
  const [driftStatus, setDriftStatus] = useState('');
  // Why Preview & apply would be held once a check settles; set each render (see below).
  const previewReasonOnceCheckedRef = useRef<string | null>(null);

  const checkDrift = async () => {
    // A fresh report starts clean: no preview of the last one, and (dropPreview) none
    // still in flight and no "Preview failed" under it. A failed apply's error goes with
    // the preview it renders in; the next preview's start resets it.
    dropPreview();
    setElsewhereApplyError(null);
    const started = Date.now();
    const result = await refetchDrift();
    // Only this check's own report. A failed check keeps the last report's data, and one
    // whose query was removed mid-flight (an apply or a run landing first) can resolve as
    // a success carrying the previous report; neither refreshed anything. TanStack stamps
    // `dataUpdatedAt` only when a fetch (or a cache write) lands, so a stale one is older.
    const fresh = result.status === 'success' && result.dataUpdatedAt >= started;
    if (!fresh) return;
    // A live check refreshed the schema cache; the column dropdowns read it.
    if (result.data.schema_source === 'live') {
      void queryClient.invalidateQueries({ queryKey: ['schema', connectionId] });
    }
    // Focus stays on Check Drift, so its outcome is spoken from the status line (cleared
    // by dropPreview above, so a repeat of the same sentence is still a change). A failure
    // needs none: its alert speaks for itself.
    setDriftStatus(driftCheckAnnouncement(result.data, previewReasonOnceCheckedRef.current));
  };

  // Schema — the authoritative column list behind the table editor's column dropdowns.
  // `table.columns` only holds columns the generator annotated, a subset on a partial model.
  const { data: schemaData } = useQuery({
    queryKey: ['schema', connectionId],
    queryFn: () => connectionsApi.getSchema(connectionId!),
    enabled: !!connectionId,
    retry: false,
  });
  const schemaTables = (schemaData as { tables?: SchemaTable[] } | undefined)?.tables;

  // Suggestions — corrections captured from thumbs-down feedback in chat.
  const { data: suggestions } = useQuery<SemanticSuggestionResponse[]>({
    queryKey: ['semantic-suggestions', connectionId],
    queryFn: () => semanticApi.getSuggestions(connectionId!),
    enabled: !!connectionId && !!model,
  });

  // Logged Apply Drifts (semantic_model_history), newest first — what survives a reload.
  const { data: driftHistory } = useQuery<DriftApplyHistoryEntry[]>({
    queryKey: ['semantic-drift-history', connectionId],
    queryFn: () => semanticApi.getDriftHistory(connectionId!, DRIFT_HISTORY_LIMIT),
    enabled: !!connectionId && !!model,
  });


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

  // A fresh read of the model. `fetchQuery` alone joins a fetch already in flight for the
  // key — say a focus refetch that started before a run's batches landed — and would hand
  // back that older snapshot, so cancel it first.
  const fetchFreshModel = async () => {
    await queryClient.cancelQueries({ queryKey: ['semantic', connectionId], exact: true });
    return queryClient.fetchQuery<SemanticModel | null>({
      queryKey: ['semantic', connectionId],
      queryFn: fetchModel,
      staleTime: 0,
    });
  };

  // Bumped as every run starts. Nothing blocks a new run while a reseed is in flight (an
  // empty draft is never dirty), and a reseed landing mid-run would re-arm the editor.
  const runSeq = useRef(0);
  // Fill an empty draft from a fresh fetch once a run has ended without handing this page
  // a result. Only ever fills a null draft, so it is idempotent with every other seed (the
  // `dataUpdatedAt` effect, a success handler). A failed fetch leaves the draft empty; the
  // effect then seeds it from whichever later fetch succeeds.
  const reseedDraft = () => {
    const run = runSeq.current;
    fetchFreshModel()
      .then((m) => {
        if (m && runSeq.current === run) setDraft((d) => d ?? m);
      })
      .catch(() => {});
  };

  // Every generation path — a full run, Resume, Restart and "Finish with what's
  // generated" — ends the same way, so they share one pair of handlers rather than
  // drifting apart.
  const handleGenerationSuccess = (m: SemanticModel) => {
    // The cache write is what retires the partial banner: it reads `model` (server
    // truth), not `draft`, and the response carries generation_status: 'complete'.
    queryClient.setQueryData(['semantic', connectionId], m);
    setDraft(m);
    setConfirmAction(null);
    setGenProgress(null);
  };
  // A failed run has still reconciled whatever batches landed, so the pre-run draft is
  // stale: saving it would revert those values AND mark them user-edited, and neither
  // Resume nor a regenerate would touch them again. Drop it — Save stays disabled and
  // every editor ignores input while `draft` is null — then re-seed it here from a fresh
  // fetch (reseedDraft), rather than leaving it to the `dataUpdatedAt` effect, which only
  // seeds once the run is no longer pending. A failed fetch leaves the draft empty rather
  // than resurrecting the pre-run cache.
  const handleGenerationError = () => {
    setGenProgress(null);
    setDraft(null);
    reseedDraft();
  };

  // One run: init (fresh or resume) followed by the pending keys sliced into batches and
  // driven by two workers, then globals. Nothing here is positional any more — the server
  // hands back the exact work list in `pending_table_keys`, keyed by table.
  //
  // The bar is driven client-side. Regeneration reconciles onto existing rows, so the
  // server's `tables_done` counts this run's stamped rows; the client knows exactly how
  // many keys it dispatched per batch, so it starts from `tables_total - pending.length`
  // and adds the dispatched count as each batch resolves.
  const runGeneration = async (resume: boolean) => {
    const providerParam = selectedProvider || undefined;
    const token: RetryToken = { cancelled: false };
    // Init is rate limited too, and the likeliest moment to hit that is a Resume straight
    // after a throttled run — so it shares the batches' retry policy.
    const init = await withRetry(
      () => semanticApi.generateInit(connectionId!, providerParam, resume),
      token,
    );
    if (!init) throw new Error('Generation was cancelled');
    const { tables_total: tablesTotal, batch_size: batchSize, pending_table_keys: pending } = init;
    const alreadyDone = tablesTotal - pending.length;

    setGenProgress({
      tables_done: alreadyDone,
      tables_total: tablesTotal,
      batch_size: batchSize,
      phase: 'tables',
    });

    const CONCURRENCY = 2;
    // Guard the slice step: a zero batch size would never advance.
    const step = Math.max(1, batchSize);
    const queue: string[][] = [];
    for (let i = 0; i < pending.length; i += step) {
      queue.push(pending.slice(i, i + step));
    }
    // A table the LLM returns nothing for stays pending server-side. It gets exactly
    // one more attempt, as its own batch; a second omission counts as done for the
    // bar, and /generate/globals names every table still without an annotation.
    const retried = new Set<string>();
    const runWorker = async () => {
      try {
        while (queue.length > 0 && !token.cancelled) {
          const keys = queue.shift()!;
          const result = await withRetry(
            () => semanticApi.generateBatch(connectionId!, keys, providerParam),
            token,
          );
          // A batch that lands after the other worker failed must not move the bar of a
          // run that has already been abandoned.
          if (token.cancelled || !result) return;
          // Only keys this batch actually carried: anything else would skew the bar.
          const requeue = (result.omitted_table_keys ?? []).filter(
            (k) => keys.includes(k) && !retried.has(k),
          );
          requeue.forEach((k) => retried.add(k));
          if (requeue.length > 0) queue.push(requeue);
          const landed = keys.length - requeue.length;
          setGenProgress((p) => (p ? { ...p, tables_done: p.tables_done + landed } : null));
        }
      } catch (err) {
        // withRetry cancels on its own failures; anything else thrown here must stop the
        // sibling worker too, or it keeps dispatching batches for an abandoned run.
        token.cancelled = true;
        throw err;
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, runWorker));

    setGenProgress((p) => (p ? { ...p, phase: 'globals' } : null));
    // Globals runs once every batch has already been paid for, so a throttle here is the
    // most expensive point in the run to give up at.
    const globals = await withRetry(
      () => semanticApi.generateGlobals(connectionId!, providerParam),
      token,
    );
    // withRetry yields undefined only when another worker cancelled the run — impossible
    // for this single trailing call, which throws on its own failure. Narrowing regardless.
    if (!globals) throw new Error('Generation was cancelled');
    return globals;
  };

  type GenerationKind = 'full' | 'resume' | 'restart' | 'finish';

  // One mutation for every way a run can start, so there is one isPending, one error
  // (TanStack clears it when the next run starts) and no cross-resets. Restart is the
  // one destructive path: the model is deleted first, then a fresh (non-resumed) run —
  // /generate/init on its own no longer discards anything. Finish runs /generate/globals
  // alone: it accepts a tables_partial model and finalises over whatever is there.
  const generation = useMutation<SemanticModel, Error, GenerationKind>({
    // Keyed so a page mounted later can see a run this one started (see runningElsewhere).
    mutationKey: ['semantic-generation', connectionId],
    mutationFn: async (kind) => {
      switch (kind) {
        case 'full':
          return runGeneration(false);
        case 'resume':
          return runGeneration(true);
        case 'restart':
          await semanticApi.delete(connectionId!);
          // The model is gone server-side; keep no cached copy, or a failed init leaves
          // the old editor and a stale interrupted banner on screen. Data is set to
          // `null` rather than removed: removing an observed query makes it refetch at
          // once, and the page's loading spinner would replace the progress bar mid-run.
          queryClient.setQueryData<SemanticModel | null>(['semantic', connectionId], null);
          return runGeneration(false);
        case 'finish': {
          const done = model?.generation_progress?.tables_done ?? 0;
          setGenProgress({
            tables_done: done,
            tables_total: model?.generation_progress?.tables_total ?? done,
            batch_size: 0,
            // Skips straight to the globals label; there is no batch queue on this path.
            phase: 'globals',
          });
          const globals = await withRetry(
            () => semanticApi.generateGlobals(connectionId!, selectedProvider || undefined),
            { cancelled: false },
          );
          if (!globals) throw new Error('Generation was cancelled');
          return globals;
        }
      }
    },
    // Every editor ignores input while `draft` is null (see handleGenerationError), so
    // nothing typed mid-run can be replaced by the result without a trace. The result
    // re-seeds the draft on success; a failed run refetches and re-seeds from that.
    // The drift report goes at the start, not on success: it described the model the run
    // is replacing, and a failed run has still reconciled the batches that landed, so it
    // is no truer of the model either outcome leaves. The next Check Drift reads that.
    onMutate: () => {
      runSeq.current += 1;
      setDraft(null);
      setDriftSummary(null);
      dropPreview();
      setElsewhereApplyError(null);
      queryClient.removeQueries({ queryKey: ['semantic-drift', connectionId] });
    },
    onSuccess: handleGenerationSuccess,
    onError: handleGenerationError,
  });

  // Apply Drift writes the model like a run does, so the editor is paused the same way
  // (`anyGenerationPending`) and the result re-seeds the draft. It can take as long as a
  // small run — one LLM call per GENERATION_BATCH_SIZE new tables, then a globals pass,
  // and new-column batches that share the backend's cap of 3 annotation batches with the
  // new tables — so it is keyed like `generation` and a page mounted mid-apply stays inert too.
  const applyDrift = useMutation({
    mutationKey: ['semantic-apply-drift', connectionId],
    mutationFn: () => semanticApi.applyDrift(connectionId!, selectedProvider || undefined),
    // The preview stays: a failed apply shows its error beside it, to retry or cancel.
    onMutate: () => {
      runSeq.current += 1;
      setDraft(null);
      setDriftSummary(null);
    },
    onSuccess: ({ semantic_model: m, summary }) => {
      queryClient.setQueryData(['semantic', connectionId], m);
      setDraft(m);
      // The report is stale now; the next Check Drift reads the applied model.
      queryClient.removeQueries({ queryKey: ['semantic-drift', connectionId] });
      // The apply introspected live and the purge keeps that schema cached; the column
      // dropdowns read it.
      void queryClient.invalidateQueries({ queryKey: ['schema', connectionId] });
      focusAfterClose.current = 'result';
      dropPreview();
      setDriftSummary(summary);
      void queryClient.invalidateQueries({ queryKey: ['semantic-drift-history', connectionId] });
    },
    onError: () => reseedDraft(),
  });

  const previewDrift = useMutation({
    mutationFn: () => semanticApi.applyDrift(connectionId!, selectedProvider || undefined, true),
    onMutate: () => {
      setDriftPreview(null);
      setDriftSummary(null);
      // The last apply's error belongs to the preview it failed under, which every path
      // to a new preview (Cancel, Check Drift, a save, a run) has dropped.
      applyDrift.reset();
      setElsewhereApplyError(null);
      setDriftStatus('');
      previewSeq.current += 1;
      return previewSeq.current;
    },
    // Not if the preview was dropped meanwhile (a Check Drift, a run, a save): it would
    // describe a report or a model that is no longer on screen.
    onSuccess: ({ summary }, _vars, seq) => {
      if (seq !== previewSeq.current) return;
      // A dry run still introspects live and refreshes the schema cache.
      void queryClient.invalidateQueries({ queryKey: ['schema', connectionId] });
      focusAfterClose.current = 'panel';
      setDriftPreview(summary);
    },
  });

  // A run keeps going when the page that started it unmounts (the route remounts the page
  // per connection, so leaving mid-run and coming back yields a new instance). That run is
  // invisible to this instance's mutation, so count it from the mutation cache: while it
  // is in flight this page must stay inert exactly as during its own run.
  const runsInFlight = useIsMutating({ mutationKey: ['semantic-generation', connectionId] });
  const runningElsewhere = runsInFlight > 0 && !generation.isPending;
  const runInFlight = generation.isPending || runningElsewhere;
  // The same for an Apply Drift started by an earlier instance of this page.
  const appliesInFlight = useIsMutating({ mutationKey: ['semantic-apply-drift', connectionId] });
  const applyingElsewhere = appliesInFlight > 0 && !applyDrift.isPending;
  const applyInFlight = applyDrift.isPending || applyingElsewhere;
  // Every control that can start a run or write the model is disabled while a run or an
  // Apply Drift is in flight, whichever page instance owns it.
  const anyGenerationPending = runInFlight || applyInFlight;

  // The owner's handlers run on its unmounted instance, where setDraft is a no-op, so this
  // page reseeds itself when such a run or apply ends, success or failure alike. Not for
  // ones started here: their own handlers seed (or reseed) the draft.
  const busyElsewhere = runningElsewhere || applyingElsewhere;
  const wasBusyElsewhere = useRef(busyElsewhere);
  useEffect(() => {
    const ended = wasBusyElsewhere.current && !busyElsewhere;
    wasBusyElsewhere.current = busyElsewhere;
    if (ended && !draft) reseedDraft();
  }, [busyElsewhere]); // eslint-disable-line react-hooks/exhaustive-deps

  // The outcome of an apply an earlier mount started: its handlers ran on that unmounted
  // instance (the report and the cache are handled there), so this one shows the result
  // or the error. Only for an apply it saw in flight — not one that settled before this
  // mount, which the person has already seen or left behind.
  const applies = useMutationState({
    filters: { mutationKey: ['semantic-apply-drift', connectionId] },
    select: (m) => m.state,
  });
  const latestApply = applies[applies.length - 1];
  const wasApplyingElsewhere = useRef(applyingElsewhere);
  useEffect(() => {
    const ended = wasApplyingElsewhere.current && !applyingElsewhere;
    wasApplyingElsewhere.current = applyingElsewhere;
    if (!ended || !latestApply) return;
    if (latestApply.status === 'success') {
      const { summary } = latestApply.data as ApplyDriftResponse;
      setDriftSummary(summary);
      // Nothing on this mount had focus to move to the result. A failure needs no status
      // line: its alert speaks for itself.
      setDriftStatus(summaryAnnouncement(summary));
    } else if (latestApply.status === 'error') {
      setElsewhereApplyError(apiErrorMessage(latestApply.error, 'Unknown error'));
    }
  }, [applyingElsewhere]); // eslint-disable-line react-hooks/exhaustive-deps

  // Moves focus as `focusAfterClose` asks, to the first focusable element of its chain:
  // 'panel' (a preview landed) and 'result' (an apply succeeded) the panel itself;
  // 'preview' (Cancel) Preview & apply; every chain ends at Check Drift. After an apply,
  // Check Drift is enabled again by the time this runs: the mutation settles in the
  // microtasks after its onSuccess, before React renders the state that onSuccess set.
  useEffect(() => {
    const target = focusAfterClose.current;
    if (!target) return;
    focusAfterClose.current = null;
    // Only focus that was lost: every trigger unmounts the control that had it, leaving
    // <body>. A preview or apply can take long enough for the user to move on, and
    // pulling focus from a field would make the next keystroke land somewhere else — so
    // the status line says what arrived instead.
    const active = document.activeElement;
    if (active && active !== document.body) {
      const arrived =
        target === 'panel' ? driftPreview : target === 'result' ? driftSummary : null;
      if (arrived) setDriftStatus(summaryAnnouncement(arrived));
      return;
    }
    const chain: { current: HTMLElement | null }[] =
      target === 'panel'
        ? [previewPanelRef, checkDriftButtonRef]
        : target === 'result'
          ? [resultPanelRef, checkDriftButtonRef]
          : target === 'preview'
            ? [previewButtonRef, checkDriftButtonRef]
            : [checkDriftButtonRef];
    chain
      .map((r) => r.current)
      .find((el) => el && !(el instanceof HTMLButtonElement && el.disabled))
      ?.focus();
  }, [driftPreview, driftSummary]);

  // Seed an empty draft from every successful fetch — first load, and any later fetch
  // once a failed run's own reseed (handleGenerationError) failed too; when that reseed
  // succeeds, both fill the same null draft and the second is a no-op. Keyed on
  // `dataUpdatedAt`, not `model`: structural sharing hands back the same reference when a
  // refetch returns identical data, and the draft would then stay empty (and every
  // editor inert) until a reload.
  // Not while a run is in flight: a focus refetch would otherwise re-arm the editor mid-run.
  useEffect(() => {
    if (model && !draft && !anyGenerationPending) setDraft(model);
  }, [dataUpdatedAt]); // eslint-disable-line react-hooks/exhaustive-deps

  // The sent draft is the mutation variable so onSuccess can tell whether the user kept
  // typing while the PUT was in flight.
  const save = useMutation({
    mutationFn: (sent: SemanticModel) => semanticApi.update(connectionId!, sent),
    onSuccess: (m, sent) => {
      queryClient.setQueryData(['semantic', connectionId], m);
      // The store normalises and fits what it is given, so the editor shows what was
      // stored — unless the draft moved on mid-save, when replacing it would drop edits.
      setDraft((cur) => (cur === sent ? m : cur));
      // A preview is stale once the stored model changes.
      dropPreview();
      setSaveError(null);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    },
    onError: () => setSaveError('Failed to save — please try again'),
  });

  const applySuggestion = useMutation({
    mutationFn: (suggestionId: string) =>
      semanticApi.applySuggestion(connectionId!, suggestionId),
    onSuccess: (updated) => {
      queryClient.setQueryData(['semantic', connectionId], updated);
      setDraft(updated);
      dropPreview();
      queryClient.invalidateQueries({ queryKey: ['semantic-suggestions', connectionId] });
    },
    // E.g. a 409 because another tab applied it first: the list and the model are both
    // out of date. Apply is disabled over unsaved edits, so the draft is the model as
    // loaded and can be replaced — unless the user started editing meanwhile.
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['semantic-suggestions', connectionId] });
      const before = draft;
      // A run started before the refresh lands has dropped the draft on purpose.
      const run = runSeq.current;
      fetchFreshModel()
        .then((m) => {
          if (m && runSeq.current === run) setDraft((cur) => (cur === before ? m : cur));
        })
        .catch(() => {});
    },
  });

  const connMatch = connections?.find((c) => c.id === connectionId);
  const connName = connMatch?.name ?? (connections === undefined ? 'Loading…' : connectionId);
  const displayModel = draft ?? model;
  const hasModel = !!displayModel;

  // Unsaved edits. Not `draft !== model`: the cache is structurally shared, so after a
  // save or a run the two are equal in value but rarely the same reference.
  const isDirty = useMemo(
    () => !!draft && !!model && replaceEqualDeep(model, draft) !== model,
    [draft, model],
  );

  // Why no generation can start, shared by the toolbar and the interrupted banner (and so
  // Resume, Finish and Restart). Every run drops the draft, so unsaved edits block one.
  const generateDisabledReason = !canGenerate
    ? 'Sign in to generate the semantic model'
    : !selectedProvider
      ? 'Configure an LLM provider in Settings first'
      : isDirty
        ? 'Save your changes first — starting a run discards unsaved edits'
        : applySuggestion.isPending
          ? SUGGESTION_PENDING_REASON
          : null;

  // Why a drift preview or apply cannot start. Not this page's own apply: its button
  // reads "Applying…" instead.
  const driftWaitReason = runInFlight
    ? RUN_PENDING_REASON
    : applyingElsewhere
      ? DRIFT_APPLY_PENDING_REASON
      : null;
  // A suggestion or a save landing mid-apply would refill the draft the apply dropped,
  // and a save would drop the preview the apply reports its failure in.
  const previewReasonOnceChecked = isDirty
    ? 'Save your changes first — applying drift discards unsaved edits'
    : (driftWaitReason ??
      (applySuggestion.isPending
        ? SUGGESTION_PENDING_REASON
        : save.isPending
          ? 'Wait for the save to finish'
          : null));
  // Read by checkDrift once its report lands, to say whether Preview & apply is usable.
  previewReasonOnceCheckedRef.current = previewReasonOnceChecked;
  // A check in flight too: an apply landing before it would be followed by its news of
  // the drift the apply just removed.
  const previewDisabledReason =
    !isDirty && !driftWaitReason && driftChecking
      ? 'Wait for the drift check to finish'
      : previewReasonOnceChecked;
  // New tables and new columns are annotated by the LLM. Without a provider the backend
  // fails an apply that adds tables, or one whose only changes are new columns (it has
  // nothing else to do), with a 400. New columns beside a removed table, a removed column
  // or a renamed column do not block it: those apply with no LLM call, and the summary
  // warns that the new columns were not annotated.
  const needsProvider =
    driftPreview !== null &&
    (driftPreview.tables_added.length > 0 ||
      (Object.keys(driftPreview.columns_added).length > 0 &&
        driftPreview.tables_removed.length === 0 &&
        Object.keys(driftPreview.columns_removed).length === 0 &&
        Object.keys(driftPreview.columns_renamed).length === 0));
  const applyDisabledReason =
    previewDisabledReason ??
    (needsProvider && !selectedProvider ? 'Configure an LLM provider in Settings first' : null);
  // A report against the cached schema cannot be previewed or applied: both introspect
  // the live database, which that check could not reach.
  const previewButtonReason =
    driftReport?.schema_source === 'cache'
      ? 'The database could not be reached — check drift again once it is back'
      : previewDisabledReason;

  // A metric missing a field its type requires would save (empty strings pass the
  // backend's `str` validation) and then render as broken SQL in every prompt.
  const invalidMetricCount = (displayModel?.business_metrics ?? []).filter(
    (m) => metricValidationError(m) !== null,
  ).length;

  const pendingSuggestions = (suggestions ?? []).filter(
    (s) => !s.is_applied && !dismissedIds.has(s.id),
  );


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
    { id: 'segments', label: 'Segments', count: displayModel?.segments?.length ?? 0 },
    { id: 'notes', label: 'Notes', count: displayModel?.notes?.length ?? 0 },
    { id: 'time', label: 'Time Expressions' },
    { id: 'suggestions', label: 'Suggestions', count: pendingSuggestions.length },
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
      {confirmAction && (
        <GenerateConfirmDialog
          action={confirmAction}
          onConfirm={() => {
            setConfirmAction(null);
            if (confirmAction === 'restart') generation.mutate('restart');
            else generation.mutate('full');
          }}
          onCancel={() => setConfirmAction(null)}
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
                  disabled={!draft || applySuggestion.isPending}
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

            {/* Held while any generation run is in flight: two runs would write the same
                connection's rows concurrently. */}
            <button
              onClick={() => hasModel ? setConfirmAction('regenerate') : generation.mutate('full')}
              disabled={!!generateDisabledReason || anyGenerationPending}
              title={generateDisabledReason ?? undefined}
              className="rounded-md border border-primary px-4 py-1.5 text-sm text-primary transition-colors hover:bg-primary/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runInFlight ? 'Generating…' : hasModel ? 'Regenerate' : 'Generate'}
            </button>

            {/* Held during a run or an apply: the report would be stale the moment it
                lands, and would outlive it with Preview & apply enabled. */}
            <button
              ref={checkDriftButtonRef}
              onClick={() => void checkDrift()}
              disabled={driftChecking || !displayModel || anyGenerationPending}
              title={
                applyInFlight
                  ? DRIFT_APPLY_PENDING_REASON
                  : runInFlight
                    ? RUN_PENDING_REASON
                    : undefined
              }
              className="rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
            >
              {driftChecking ? 'Checking…' : 'Check Drift'}
            </button>

            {displayModel && (
              <button
                onClick={() => draft && save.mutate(draft)}
                // A draft saved mid-run is stale: it would overwrite the values the run is
                // regenerating and mark them user-edited, so regeneration skips them for good.
                disabled={save.isPending || !draft || anyGenerationPending}
                title={
                  applyInFlight
                    ? `${DRIFT_APPLY_PENDING_REASON} — saving now would overwrite the applied values`
                    : anyGenerationPending
                      ? 'Wait for generation to finish — saving now would overwrite the regenerated values'
                      : undefined
                }
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
                <span>
                  {genProgress.phase === 'globals'
                    ? 'Building metrics and joins…'
                    : 'Analysing tables…'}
                </span>
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
              <p className="text-xs text-muted-foreground">
                Editing is paused while generation runs.
              </p>
            </div>
          )}

          {/* A run with no bar: one started by this page before its first progress, or one
              still going from before the user left and came back. */}
          {anyGenerationPending && !genProgress && (
            <p className="text-xs text-muted-foreground">
              {applyInFlight ? 'Applying schema drift.' : 'Generation is running.'} Editing
              is paused until it finishes.
            </p>
          )}

          {/* Interrupted generation. Unmounted while any run is in flight, so the user
              cannot double-fire it. */}
          {model?.generation_status === 'tables_partial' && !anyGenerationPending && (
            <GenerationBanner
              tablesDone={model.generation_progress?.tables_done ?? 0}
              tablesTotal={model.generation_progress?.tables_total ?? null}
              canGenerate={!generateDisabledReason}
              disabledReason={generateDisabledReason}
              hasTables={Object.keys(model.tables ?? {}).length > 0}
              onResume={() => generation.mutate('resume')}
              onFinish={() => generation.mutate('finish')}
              onRestart={() => setConfirmAction('restart')}
            />
          )}

          {/* Generation error */}
          {generation.error && (
            <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              Generation failed: {apiErrorMessage(generation.error, 'Unknown error')}
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

          {/* Drift check outcome — an error, a clean result, or the warnings below */}
          {driftError && !driftChecking && (
            <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              Drift check failed: {apiErrorMessage(driftError, 'Unknown error')}
            </div>
          )}
          {driftReport && !driftError && !driftChecking && driftReport.warning_count === 0 && (
            <div className="flex items-center gap-2 rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
              <Check className="h-4 w-4" />
              No drift — the model matches the{' '}
              {driftReport.schema_source === 'live' ? 'live' : 'cached'} schema (checked at{' '}
              {new Date(driftReport.checked_at).toLocaleTimeString()}).
            </div>
          )}
          {driftReport?.schema_source === 'cache' && !driftError && !driftChecking && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-700 dark:text-amber-400">
              Could not reach the database
              {driftReport.refresh_error ? ` (${driftReport.refresh_error})` : ''}, so drift was
              checked against the schema cached
              {driftReport.schema_cached_at
                ? ` at ${new Date(driftReport.schema_cached_at).toLocaleString()}`
                : ''}
              .
            </div>
          )}

          {/* Drift warning banner — from fresh drift check */}
          {/* Not after a failed re-check: TanStack keeps the last report's data then. */}
          {(driftReport?.warnings?.length ?? 0) > 0 && !driftError && (
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
                Checked at {new Date(driftReport!.checked_at).toLocaleTimeString()}
              </p>
              {driftReport!.has_actionable_changes && !driftPreview && (
                <div className="mt-3 flex flex-col gap-2">
                  <p className="text-xs text-destructive/70">
                    Removes deleted tables, with the metrics, joins and derived columns that
                    list them, segments that apply only to them, and relationships over them,
                    and deleted columns, with the relationships and references that used them.
                    Renames a column stored under a near-miss spelling to its real name,
                    keeping its annotation and your edits. Annotates new tables and the new
                    columns of existing ones, and adds new cross-table items beside the
                    existing ones. Hidden tables are kept, and so are tables the connection
                    cannot read when PostgreSQL can tell them apart from dropped ones. You
                    will see what it removes and renames, and which tables and columns it
                    annotates, before anything is applied.
                  </p>
                  <button
                    ref={previewButtonRef}
                    onClick={() => previewDrift.mutate()}
                    disabled={
                      !!previewButtonReason || anyGenerationPending || previewDrift.isPending
                    }
                    title={previewButtonReason ?? undefined}
                    className="self-start rounded-md border border-destructive/40 bg-destructive/20 px-3 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-destructive/30 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {previewDrift.isPending ? 'Preparing preview…' : 'Preview & apply'}
                  </button>
                </div>
              )}
              {previewDrift.error && !previewDrift.isPending && (
                <p role="alert" className="mt-2 text-xs text-destructive">
                  Preview failed: {apiErrorMessage(previewDrift.error, 'Unknown error')}
                </p>
              )}
            </div>
          )}

          {elsewhereApplyError && (
            <div
              role="alert"
              className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            >
              Apply drift failed: {elsewhereApplyError}
            </div>
          )}

          {/* Only a preview with something to apply has Cancel; one that found nothing is
              closed with Dismiss instead. */}
          {driftPreview && (
            <DriftSummaryPanel
              summary={driftPreview}
              region
              panelRef={previewPanelRef}
              onDismiss={
                driftPreview.changed
                  ? undefined
                  : () => {
                      focusAfterClose.current = 'check';
                      dropPreview();
                    }
              }
            >
              {driftPreview.changed && (
                <div className="mt-3 space-y-2">
                  {/* Beside the buttons it concerns: the preview stays, to retry or cancel. */}
                  {applyDrift.error && !applyDrift.isPending && (
                    <p role="alert" className="text-xs text-destructive">
                      Apply drift failed: {apiErrorMessage(applyDrift.error, 'Unknown error')}
                    </p>
                  )}
                  {/* Cancel first: the first Tab from the focused preview reaches it, and the
                      destructive confirm only after. */}
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        focusAfterClose.current = 'preview';
                        dropPreview();
                      }}
                      disabled={applyDrift.isPending}
                      className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={() => applyDrift.mutate()}
                      disabled={!!applyDisabledReason || applyInFlight}
                      title={applyDisabledReason ?? undefined}
                      className="rounded-md border border-destructive/40 bg-destructive/20 px-3 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-destructive/30 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {applyDrift.isPending ? 'Applying…' : 'Apply these changes'}
                    </button>
                  </div>
                </div>
              )}
            </DriftSummaryPanel>
          )}

          {driftSummary && (
            <DriftSummaryPanel
              summary={driftSummary}
              region
              panelRef={resultPanelRef}
              onDismiss={() => {
                focusAfterClose.current = 'check';
                setDriftSummary(null);
                setDriftStatus('');
              }}
            />
          )}

          <DriftHistory entries={driftHistory ?? []} limit={DRIFT_HISTORY_LIMIT} />

          {/* Always mounted, so a change to its text is announced (see driftStatus). */}
          <p role="status" className="sr-only">
            {driftStatus}
          </p>


          {/* Empty state */}
          {(!!error || (!displayModel && !anyGenerationPending)) && (
            <div className="rounded-xl border border-dashed border-border p-8 text-center">
              <p className="mb-3 text-sm text-muted-foreground">No semantic model yet.</p>
              {canGenerate ? (
                <button
                  onClick={() => generation.mutate('full')}
                  disabled={anyGenerationPending || !selectedProvider}
                  className="rounded-md bg-brand-gradient px-6 py-2 text-sm font-medium text-white shadow-gradient-btn transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  {anyGenerationPending ? 'Generating…' : 'Generate Semantic Model'}
                </button>
              ) : (
                <p className="text-xs text-muted-foreground">Contact an admin to generate the semantic model.</p>
              )}
            </div>
          )}

          {/* Model content */}
          {displayModel && (
            <>
              {/* Pending suggestions nudge — corrections are invisible on other tabs */}
              {pendingSuggestions.length > 0 && activeTab !== 'suggestions' && (
                <div className="flex items-center gap-3 rounded-xl border border-primary/50 bg-primary/10 px-4 py-3">
                  <p className="flex-1 text-sm text-foreground">
                    {pendingSuggestions.length} pending correction
                    {pendingSuggestions.length !== 1 ? 's' : ''} from chat feedback.
                  </p>
                  <button
                    onClick={() => setActiveTab('suggestions')}
                    className="shrink-0 text-sm font-medium text-primary hover:underline"
                  >
                    Review
                  </button>
                </div>
              )}

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
              {/* Every editor ignores input while the draft is empty (a run in flight, or a
                  failed run awaiting its reseed); disabling them says so instead of
                  swallowing keystrokes. The model is shown read-only meanwhile and every
                  control inside is paused; only the tabs stay outside and keep working. */}
              {/* Inert while an apply is in flight too: its success and error handlers both
                  replace the draft, which would drop anything typed during the POST. */}
              <fieldset disabled={!draft || applySuggestion.isPending} className="contents">

              {/* Tables — existing editor (v2 fields are preserved via spread) */}
              {activeTab === 'tables' && (
                <SemanticModelEditor
                  model={displayModel as SemanticModel}
                  schemaTables={schemaTables}
                  // Ignored while the draft is empty, like every other tab: seeding one from
                  // `displayModel` here would resurrect the stale pre-run cache.
                  onChange={(m) => setDraft((d) => (d ? { ...d, ...m } : d))}
                />
              )}

              {/* Column intelligence — semantic types + cardinality per column */}
              {activeTab === 'intelligence' && (
                <ColumnIntelligencePanel model={displayModel} />
              )}

              {/* Business metrics */}
              {activeTab === 'metrics' && (
                <>
                {invalidMetricCount > 0 && (
                  <div className="mb-3 flex items-start gap-3 rounded-xl border border-warning/50 bg-warning/15 px-4 py-3">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                    <p className="text-sm text-warning-foreground">
                      {invalidMetricCount} metric{invalidMetricCount !== 1 ? 's are' : ' is'}{' '}
                      missing a required field. Empty values save but produce broken SQL in
                      every prompt.
                    </p>
                  </div>
                )}
                <BusinessMetricEditor
                  metrics={displayModel.business_metrics ?? []}
                  onChange={(metrics) =>
                    setDraft((d) => (d ? { ...d, business_metrics: metrics } : d))
                  }
                />
                </>
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

              {/* Segments — named reusable filter expressions */}
              {activeTab === 'segments' && (
                <div className="space-y-3">
                  {(displayModel.segments ?? []).map((seg, i) => {
                    const updateSeg = (patch: Partial<Segment>) =>
                      setDraft((d) =>
                        d
                          ? {
                              ...d,
                              segments: (d.segments ?? []).map((s, idx) =>
                                idx === i ? { ...s, ...patch } : s,
                              ),
                            }
                          : d,
                      );
                    return (
                      <div
                        key={i}
                        className="space-y-2 rounded-xl border border-border bg-card p-4"
                      >
                        <div className="flex items-center gap-2">
                          <input
                            aria-label={`Segment name ${i + 1}`}
                            value={seg.name}
                            onChange={(e) => updateSeg({ name: e.target.value })}
                            placeholder="active_customers"
                            className="flex-1 rounded border border-border bg-background px-2 py-1 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                          <button
                            onClick={() =>
                              setDraft((d) =>
                                d
                                  ? {
                                      ...d,
                                      segments: (d.segments ?? []).filter((_, idx) => idx !== i),
                                    }
                                  : d,
                              )
                            }
                            className="text-sm text-destructive hover:text-destructive/80"
                          >
                            Delete
                          </button>
                        </div>
                        <div>
                          <label className="text-xs text-muted-foreground">SQL expression</label>
                          <input
                            aria-label={`Segment SQL expression ${i + 1}`}
                            value={seg.sql_expression}
                            onChange={(e) => updateSeg({ sql_expression: e.target.value })}
                            placeholder="status = 'active' AND deleted_at IS NULL"
                            className="mt-1 w-full rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </div>
                        <div>
                          <label className="text-xs text-muted-foreground">Description</label>
                          <input
                            aria-label={`Segment description ${i + 1}`}
                            value={seg.description}
                            onChange={(e) => updateSeg({ description: e.target.value })}
                            className="mt-1 w-full rounded border border-border bg-background px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </div>
                        <div>
                          <label className="text-xs text-muted-foreground">
                            Applicable tables (comma-separated)
                          </label>
                          <input
                            aria-label={`Segment applicable tables ${i + 1}`}
                            value={seg.applicable_tables.join(', ')}
                            onChange={(e) =>
                              updateSeg({
                                applicable_tables: e.target.value
                                  .split(',')
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              })
                            }
                            placeholder="store.customers"
                            className="mt-1 w-full rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                          />
                        </div>
                      </div>
                    );
                  })}
                  {(displayModel.segments ?? []).length === 0 && (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No segments defined. Segments are named filters the model can reuse —
                      regenerate to auto-populate, or add one below.
                    </p>
                  )}
                  <button
                    onClick={() =>
                      setDraft((d) =>
                        d
                          ? {
                              ...d,
                              segments: [
                                ...(d.segments ?? []),
                                {
                                  name: '',
                                  sql_expression: '',
                                  description: '',
                                  applicable_tables: [],
                                },
                              ],
                            }
                          : d,
                      )
                    }
                    className="flex w-full items-center justify-center gap-1.5 rounded-lg border-2 border-dashed border-border py-2 text-sm text-muted-foreground transition-colors hover:border-ring hover:text-foreground"
                  >
                    <Plus className="h-4 w-4" />
                    Add Segment
                  </button>
                </div>
              )}

              {/* Notes — free-form domain context injected at the top of every prompt */}
              {activeTab === 'notes' && (
                <div className="space-y-3">
                  <p className="text-xs text-muted-foreground">
                    Cross-table facts that don't fit table or column metadata. Each note is
                    injected verbatim at the top of every prompt.
                  </p>
                  {(displayModel.notes ?? []).map((note, i) => (
                    <div key={i} className="flex items-start gap-2">
                      <textarea
                        aria-label={`Note ${i + 1}`}
                        value={note}
                        onChange={(e) =>
                          setDraft((d) =>
                            d
                              ? {
                                  ...d,
                                  notes: (d.notes ?? []).map((n, idx) =>
                                    idx === i ? e.target.value : n,
                                  ),
                                }
                              : d,
                          )
                        }
                        rows={2}
                        placeholder="finance.budgets is quarterly; for monthly revenue use finance.transactions"
                        className="flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                      />
                      <button
                        onClick={() =>
                          setDraft((d) =>
                            d
                              ? { ...d, notes: (d.notes ?? []).filter((_, idx) => idx !== i) }
                              : d,
                          )
                        }
                        title={`Delete note ${i + 1}`}
                        className="mt-1 rounded-md p-1.5 text-destructive hover:bg-muted"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  ))}
                  {(displayModel.notes ?? []).length === 0 && (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No notes yet.
                    </p>
                  )}
                  <button
                    onClick={() =>
                      setDraft((d) => (d ? { ...d, notes: [...(d.notes ?? []), ''] } : d))
                    }
                    className="flex w-full items-center justify-center gap-1.5 rounded-lg border-2 border-dashed border-border py-2 text-sm text-muted-foreground transition-colors hover:border-ring hover:text-foreground"
                  >
                    <Plus className="h-4 w-4" />
                    Add Note
                  </button>
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

              {/* Pending suggestions — corrections captured from chat feedback */}
              {activeTab === 'suggestions' && (
                <div className="space-y-3">
                  {applySuggestion.error && (
                    <p
                      role="alert"
                      className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
                    >
                      Could not apply the suggestion:{' '}
                      {apiErrorMessage(applySuggestion.error, 'Unknown error')}
                    </p>
                  )}
                  {pendingSuggestions.length === 0 ? (
                    <p className="py-4 text-center text-sm text-muted-foreground">
                      No pending suggestions. Thumbs-down feedback on chat answers will
                      surface corrections here.
                    </p>
                  ) : (
                    pendingSuggestions.map((s) => (
                      <div
                        key={s.id}
                        className="space-y-2 rounded-xl border border-border bg-card p-4"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 space-y-1">
                            <p className="text-sm font-medium text-foreground">
                              {correctionLabel(s)}
                            </p>
                            <p className="text-xs text-muted-foreground">
                              Type:{' '}
                              <span className="font-mono">
                                {s.correction_type.replace(/_/g, ' ')}
                              </span>
                            </p>
                            <pre className="overflow-x-auto rounded bg-muted px-2 py-1.5 font-mono text-xs text-muted-foreground">
                              {JSON.stringify(s.value, null, 2)}
                            </pre>
                            <p className="text-[11px] text-muted-foreground">
                              {new Date(s.created_at).toLocaleDateString('en-US', {
                                month: 'short',
                                day: 'numeric',
                                hour: '2-digit',
                                minute: '2-digit',
                              })}
                            </p>
                          </div>
                          <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                            <button
                              onClick={() => applySuggestion.mutate(s.id)}
                              // Applying writes the model and replaces the draft with the
                              // result: mid-run it would race the run's writes and be replaced
                              // in turn, and over unsaved edits it would discard them.
                              disabled={
                                applySuggestion.isPending || anyGenerationPending || isDirty
                              }
                              title={
                                applyInFlight
                                  ? `${DRIFT_APPLY_PENDING_REASON} before applying suggestions`
                                  : anyGenerationPending
                                    ? 'Wait for generation to finish before applying suggestions'
                                    : isDirty
                                      ? 'Save your changes first — applying a suggestion discards unsaved edits'
                                      : undefined
                              }
                              className="flex items-center gap-1 rounded-md bg-brand-gradient px-3 py-1.5 text-xs font-medium text-white shadow-gradient-btn hover:opacity-90 disabled:opacity-50"
                            >
                              <Check className="h-3 w-3" />
                              Apply
                            </button>
                            <button
                              onClick={() =>
                                setDismissedIds((prev) => new Set([...prev, s.id]))
                              }
                              className="flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted"
                            >
                              <X className="h-3 w-3" />
                              Dismiss
                            </button>
                          </div>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}

              </fieldset>
              </div>{/* end tabpanel */}
            </>
          )}
        </div>
      </div>
    </>
  );
}
