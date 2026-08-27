// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useId, useState } from 'react';
import {
  AGGREGATIONS,
  FORMAT_HINTS,
  METRIC_TYPES,
  metricValidationError,
} from '../lib/semanticUtils';
import type { AggregationType, BusinessMetric, MetricType } from '../types';

interface Props {
  metrics: BusinessMetric[];
  onChange: (metrics: BusinessMetric[]) => void;
}

/**
 * Seed the fields the backend's discriminated union requires for `next`, so
 * switching type never produces a payload that 422s on save. Existing values
 * are preserved — switching away and back keeps what was already typed.
 */
function withTypeDefaults(metric: BusinessMetric, next: MetricType): BusinessMetric {
  const m: BusinessMetric = { ...metric, metric_type: next };
  if (next === 'simple' || next === 'cumulative') {
    m.aggregation = m.aggregation ?? 'sum';
    m.definition = m.definition ?? '';
  } else if (next === 'derived') {
    m.definition = m.definition ?? '';
  } else if (next === 'ratio') {
    m.numerator_expr = m.numerator_expr ?? '';
    m.denominator_expr = m.denominator_expr ?? '';
  } else if (next === 'conversion') {
    m.base_measure = m.base_measure ?? '';
    m.conversion_measure = m.conversion_measure ?? '';
    m.calculation = m.calculation ?? 'conversion_rate';
  }
  return m;
}

const inputClass =
  'w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded font-mono focus:outline-none focus:ring-1 focus:ring-ring';

const DEFINITION_LABEL: Record<string, string> = {
  simple: 'Definition (SQL expression)',
  cumulative: 'Definition (SQL expression)',
  derived: 'Definition (formula combining other metrics)',
};

// metric_type is the backend discriminator — a metric without it is rejected with a 422.
const EMPTY_METRIC: BusinessMetric = {
  metric_type: 'simple',
  name: '',
  definition: '',
  description: '',
  filters: [],
  related_tables: [],
  format_hint: null,
  aggregation: 'sum',
};

function MetricRow({
  metric,
  onUpdate,
  onDelete,
}: {
  metric: BusinessMetric;
  onUpdate: (m: BusinessMetric) => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const aggregationId = useId();
  const metricTypeId = useId();
  const type = metric.metric_type;
  const needsAggregation = type === 'simple' || type === 'cumulative';
  const needsDefinition = type === 'simple' || type === 'cumulative' || type === 'derived';
  const validationError = metricValidationError(metric);

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <div className="flex items-center gap-3 px-3 py-2 bg-muted">
        <button onClick={() => setOpen((o) => !o)} className="text-muted-foreground text-xs">
          {open ? '▼' : '▶'}
        </button>
        <input
          value={metric.name}
          onChange={(e) => onUpdate({ ...metric, name: e.target.value })}
          placeholder="Metric name"
          className="flex-1 text-sm font-medium bg-transparent text-foreground focus:outline-none"
        />
        {validationError && (
          <span role="alert" className="text-xs text-destructive">
            {validationError}
          </span>
        )}
        <button onClick={onDelete} className="text-destructive hover:text-destructive/80 text-sm">
          Delete
        </button>
      </div>
      {open && (
        <div className="p-3 space-y-3">
          <div>
            <label className="text-xs text-muted-foreground">Name</label>
            <input
              value={metric.name}
              onChange={(e) => onUpdate({ ...metric, name: e.target.value })}
              placeholder="e.g. Customer Total Spend"
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={metricTypeId} className="text-xs text-muted-foreground">
              Type
            </label>
            <select
              id={metricTypeId}
              value={type}
              onChange={(e) => onUpdate(withTypeDefaults(metric, e.target.value as MetricType))}
              className="px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {METRIC_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            {needsAggregation && (
              <>
                <label htmlFor={aggregationId} className="text-xs text-muted-foreground">
                  Aggregation
                </label>
                <select
                  id={aggregationId}
                  value={metric.aggregation ?? 'sum'}
                  onChange={(e) =>
                    onUpdate({ ...metric, aggregation: e.target.value as AggregationType })
                  }
                  className="px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {AGGREGATIONS.map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
          {needsDefinition && (
            <div>
              <label className="text-xs text-muted-foreground">
                {DEFINITION_LABEL[type] ?? 'Definition (SQL expression)'}
              </label>
              <input
                aria-label="Definition"
                value={metric.definition ?? ''}
                onChange={(e) => onUpdate({ ...metric, definition: e.target.value })}
                placeholder={
                  type === 'derived'
                    ? 'e.g. revenue - cost'
                    : 'e.g. SUM(orders.total_amount)'
                }
                className={inputClass}
              />
            </div>
          )}

          {type === 'ratio' && (
            <div className="flex gap-3">
              <div className="flex-1">
                <label className="text-xs text-muted-foreground">Numerator expression</label>
                <input
                  aria-label="Numerator expression"
                  value={metric.numerator_expr ?? ''}
                  onChange={(e) => onUpdate({ ...metric, numerator_expr: e.target.value })}
                  placeholder="SUM(orders.total_amount)"
                  className={inputClass}
                />
              </div>
              <div className="flex-1">
                <label className="text-xs text-muted-foreground">Denominator expression</label>
                <input
                  aria-label="Denominator expression"
                  value={metric.denominator_expr ?? ''}
                  onChange={(e) => onUpdate({ ...metric, denominator_expr: e.target.value })}
                  placeholder="COUNT(DISTINCT orders.id)"
                  className={inputClass}
                />
              </div>
            </div>
          )}

          {type === 'conversion' && (
            <>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label className="text-xs text-muted-foreground">Base measure</label>
                  <input
                    aria-label="Base measure"
                    value={metric.base_measure ?? ''}
                    onChange={(e) => onUpdate({ ...metric, base_measure: e.target.value })}
                    placeholder="sessions"
                    className={inputClass}
                  />
                </div>
                <div className="flex-1">
                  <label className="text-xs text-muted-foreground">Conversion measure</label>
                  <input
                    aria-label="Conversion measure"
                    value={metric.conversion_measure ?? ''}
                    onChange={(e) => onUpdate({ ...metric, conversion_measure: e.target.value })}
                    placeholder="signups"
                    className={inputClass}
                  />
                </div>
              </div>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label className="text-xs text-muted-foreground">
                    Entity (join column, optional)
                  </label>
                  <input
                    aria-label="Entity"
                    value={metric.entity ?? ''}
                    onChange={(e) => onUpdate({ ...metric, entity: e.target.value || null })}
                    placeholder="user_id"
                    className={inputClass}
                  />
                </div>
                <div className="flex-1">
                  <label className="text-xs text-muted-foreground">Calculation</label>
                  <select
                    aria-label="Calculation"
                    value={metric.calculation ?? 'conversion_rate'}
                    onChange={(e) =>
                      onUpdate({
                        ...metric,
                        calculation: e.target.value as 'conversion_rate' | 'conversions',
                      })
                    }
                    className={inputClass}
                  >
                    <option value="conversion_rate">conversion_rate</option>
                    <option value="conversions">conversions</option>
                  </select>
                </div>
              </div>
            </>
          )}

          {(type === 'cumulative' || type === 'conversion') && (
            <div>
              <label className="text-xs text-muted-foreground">
                {type === 'cumulative'
                  ? 'Window (OVER clause, optional)'
                  : 'Window (time window, optional)'}
              </label>
              <input
                aria-label="Window"
                value={metric.window ?? ''}
                onChange={(e) => onUpdate({ ...metric, window: e.target.value || null })}
                placeholder={type === 'cumulative' ? 'ORDER BY order_date' : '7 days'}
                className={inputClass}
              />
            </div>
          )}

          {needsAggregation && (
            <div>
              <label className="text-xs text-muted-foreground">
                Measure filters (pre-aggregation, comma-separated)
              </label>
              <input
                aria-label="Measure filters"
                value={(metric.measure_filters ?? []).join(', ')}
                onChange={(e) =>
                  onUpdate({
                    ...metric,
                    measure_filters: e.target.value
                      .split(',')
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
                placeholder="status = 'completed'"
                className={inputClass}
              />
            </div>
          )}
          <div>
            <label className="text-xs text-muted-foreground">Description</label>
            <input
              value={metric.description}
              onChange={(e) => onUpdate({ ...metric, description: e.target.value })}
              placeholder="Human-readable explanation"
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">
              Filters (comma-separated SQL conditions)
            </label>
            <input
              value={metric.filters.join(', ')}
              onChange={(e) =>
                onUpdate({
                  ...metric,
                  filters: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                })
              }
              placeholder="e.g. status NOT IN ('cancelled', 'refunded')"
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded font-mono focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Related tables (comma-separated)</label>
            <input
              value={metric.related_tables.join(', ')}
              onChange={(e) =>
                onUpdate({
                  ...metric,
                  related_tables: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                })
              }
              placeholder="store.orders, store.customers"
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Format hint</label>
            <select
              value={metric.format_hint ?? ''}
              onChange={(e) => onUpdate({ ...metric, format_hint: e.target.value || null })}
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">None</option>
              {FORMAT_HINTS.map((h) => (
                <option key={h} value={h}>{h}</option>
              ))}
            </select>
          </div>
        </div>
      )}
    </div>
  );
}

export default function BusinessMetricEditor({ metrics, onChange }: Props) {
  const addMetric = () => onChange([...metrics, { ...EMPTY_METRIC }]);

  const updateAt = (i: number, m: BusinessMetric) =>
    onChange(metrics.map((old, idx) => (idx === i ? m : old)));

  const deleteAt = (i: number) => onChange(metrics.filter((_, idx) => idx !== i));

  return (
    <div className="space-y-3">
      {metrics.map((m, i) => (
        <MetricRow
          key={i}
          metric={m}
          onUpdate={(updated) => updateAt(i, updated)}
          onDelete={() => deleteAt(i)}
        />
      ))}
      <button
        onClick={addMetric}
        className="w-full py-2 border-2 border-dashed border-border hover:border-ring rounded-lg text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        + Add metric
      </button>
    </div>
  );
}
