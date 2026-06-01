// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import type { BusinessMetric } from '../types';

interface Props {
  metrics: BusinessMetric[];
  onChange: (metrics: BusinessMetric[]) => void;
}

const EMPTY_METRIC: BusinessMetric = {
  name: '',
  definition: '',
  description: '',
  filters: [],
  related_tables: [],
  format_hint: null,
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
          <div>
            <label className="text-xs text-muted-foreground">Definition (SQL expression)</label>
            <input
              value={metric.definition}
              onChange={(e) => onUpdate({ ...metric, definition: e.target.value })}
              placeholder="e.g. SUM(orders.total_amount)"
              className="w-full mt-1 px-2 py-1 text-sm bg-background text-foreground border border-border rounded font-mono focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
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
              <option value="percentage">percentage</option>
              <option value="currency_usd">currency_usd</option>
              <option value="currency_eur">currency_eur</option>
              <option value="currency_gbp">currency_gbp</option>
              <option value="integer">integer</option>
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
