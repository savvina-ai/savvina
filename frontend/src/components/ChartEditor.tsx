// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Chart configuration panel — inspired by Microsoft Fabric notebook chart editor. */

import { useState } from 'react';
import { X, ChevronRight, ChevronDown, Plus } from 'lucide-react';

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { cn } from '@/lib/utils';
import type {
  UserChart,
  AggregationMethod,
  LegendPosition,
  YAxisScale,
  ChartFilter,
  FilterOperator,
  NumberFormat,
} from '../types/charts';
import {
  CHART_TYPE_LABELS,
  CHART_TYPES,
  CHART_TYPE_ICONS,
  AGGREGATION_LABELS,
  LEGEND_POSITION_LABELS,
  NUMBER_FORMAT_LABELS,
} from '../types/charts';

const FILTER_OPERATORS: { value: FilterOperator; label: string }[] = [
  { value: '=', label: '=' },
  { value: '!=', label: '≠' },
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
  { value: 'contains', label: 'contains' },
];

interface Props {
  chart: UserChart;
  columns: string[];
  onChange: (updated: UserChart) => void;
}
const AGGREGATIONS: AggregationMethod[] = ['none', 'sum', 'count', 'avg', 'min', 'max'];
const LEGEND_POSITIONS: LegendPosition[] = ['top', 'bottom', 'left', 'right'];
const NUMBER_FORMATS: NumberFormat[] = ['integer', 'decimal', 'currency', 'percent'];

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3">
      <Label className="w-24 shrink-0 text-xs text-muted-foreground">{label}</Label>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

function ColSelect({
  value,
  columns,
  placeholder,
  nullable,
  onValueChange,
}: {
  value: string | null;
  columns: string[];
  placeholder: string;
  nullable?: boolean;
  onValueChange: (v: string | null) => void;
}) {
  return (
    <Select
      value={value ?? '__none__'}
      onValueChange={(v) => onValueChange(v === '__none__' ? null : v)}
    >
      <SelectTrigger className="h-7 text-xs">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {nullable && (
          <SelectItem value="__none__" className="text-xs text-muted-foreground">
            — None —
          </SelectItem>
        )}
        {columns.map((col) => (
          <SelectItem key={col} value={col} className="text-xs">
            {col}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export default function ChartEditor({ chart, columns, onChange }: Props) {
  const [axisOpen, setAxisOpen] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [trendOpen, setTrendOpen] = useState(false);
  const set = <K extends keyof UserChart>(key: K, value: UserChart[K]) =>
    onChange({ ...chart, [key]: value });

  const filters = chart.filters ?? [];
  const isNumber = chart.type === 'number';
  const isGauge = chart.type === 'gauge';
  const isScalar = isNumber || isGauge;

  const addFilter = () => {
    set('filters', [...filters, { column: columns[0] ?? '', operator: '=' as FilterOperator, value: '' }]);
    setFiltersOpen(true);
  };

  const updateFilter = (idx: number, patch: Partial<ChartFilter>) => {
    const next = filters.map((f, i) => (i === idx ? { ...f, ...patch } : f));
    set('filters', next);
  };

  const removeFilter = (idx: number) => {
    set('filters', filters.filter((_, i) => i !== idx));
  };

  const availableYCols = columns.filter((c) => !chart.yKeys.includes(c));

  const addYKey = (col: string) => {
    if (!chart.yKeys.includes(col)) {
      set('yKeys', [...chart.yKeys, col]);
    }
  };

  const removeYKey = (col: string) => {
    set('yKeys', chart.yKeys.filter((k) => k !== col));
  };

  const toggleComboSeriesType = (key: string) => {
    const current = (chart.comboSeriesTypes ?? {})[key] ?? 'bar';
    onChange({
      ...chart,
      comboSeriesTypes: { ...(chart.comboSeriesTypes ?? {}), [key]: current === 'bar' ? 'line' : 'bar' },
    });
  };

  const needsAggregation = !!chart.seriesGroup;
  const showStacked = chart.type === 'bar' || chart.type === 'area' || chart.type === 'combo';
  const showGroupBy = !isScalar && chart.type !== 'pie' && chart.type !== 'scatter';
  const showDataLabelsOption = !isScalar && chart.type !== 'pie' && chart.type !== 'scatter';
  const showConnectNulls = chart.type === 'line' || chart.type === 'area' || chart.type === 'combo';
  const showAxisControls = !isScalar && chart.type !== 'pie' && chart.type !== 'scatter';
  const showTrendToggle =
    chart.type === 'bar' || chart.type === 'line' || chart.type === 'area' || chart.type === 'combo';

  return (
    <div className="border-t border-border bg-muted/30 px-4 py-3 space-y-3">
      {/* Chart type row */}
      <div className="flex flex-wrap items-center gap-1.5">
        {CHART_TYPES.map((type) => {
          const Icon = CHART_TYPE_ICONS[type];
          return (
            <button
              key={type}
              onClick={() => {
                const noGroup = type === 'pie' || type === 'scatter' || type === 'number' || type === 'gauge';
                const noStack = type === 'line' || type === 'pie' || type === 'scatter' || type === 'number' || type === 'gauge';
                onChange({
                  ...chart,
                  type,
                  ...(noGroup && { seriesGroup: null, aggregation: 'none' }),
                  ...(noStack && { stacked: false }),
                });
              }}
              title={CHART_TYPE_LABELS[type]}
              className={cn(
                'flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors',
                chart.type === type
                  ? 'bg-brand-gradient text-white shadow-gradient-btn'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {CHART_TYPE_LABELS[type]}
            </button>
          );
        })}
      </div>

      {/* Title */}
      <FieldRow label="Title">
        <Input
          value={chart.title}
          onChange={(e) => set('title', e.target.value)}
          placeholder="Chart title"
          className="h-7 text-xs"
        />
      </FieldRow>

      {/* Scalar types (number / gauge) — only need a single value column */}
      {isScalar ? (
        <FieldRow label="Value col">
          <ColSelect
            value={chart.yKeys[0] ?? null}
            columns={columns}
            placeholder="Select column"
            onValueChange={(v) => set('yKeys', v ? [v] : [])}
          />
        </FieldRow>
      ) : (
        <>
          {/* X-axis */}
          <FieldRow label="X-axis">
            <ColSelect
              value={chart.xKey}
              columns={columns}
              placeholder="Select column"
              onValueChange={(v) => set('xKey', v ?? '')}
            />
          </FieldRow>

          {/* Y-axis (multi) + combo series type toggles */}
          <FieldRow label="Y-axis">
            <div className="flex flex-wrap items-center gap-1">
              {chart.yKeys.map((col) => {
                const comboType = (chart.comboSeriesTypes ?? {})[col] ?? 'bar';
                return (
                  <span
                    key={col}
                    className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary"
                  >
                    {chart.type === 'combo' && (
                      <button
                        onClick={() => toggleComboSeriesType(col)}
                        title={`Switch to ${comboType === 'bar' ? 'line' : 'bar'}`}
                        className="rounded px-1 text-[10px] font-medium text-primary/70 hover:text-primary"
                      >
                        {comboType === 'bar' ? '▌' : '∿'}
                      </button>
                    )}
                    {col}
                    <button
                      onClick={() => removeYKey(col)}
                      className="rounded-full hover:text-destructive"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </span>
                );
              })}
              {availableYCols.length > 0 && (
                <Select onValueChange={addYKey} value="">
                  <SelectTrigger className="h-6 w-auto gap-1 rounded-full border-dashed px-2 text-xs text-muted-foreground">
                    <SelectValue placeholder="+ Add column" />
                  </SelectTrigger>
                  <SelectContent>
                    {availableYCols.map((col) => (
                      <SelectItem key={col} value={col} className="text-xs">
                        {col}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          </FieldRow>
        </>
      )}

      {/* Number card options */}
      {isNumber && (
        <>
          <FieldRow label="Format">
            <Select
              value={chart.numberFormat ?? 'decimal'}
              onValueChange={(v) => set('numberFormat', v as NumberFormat)}
            >
              <SelectTrigger className="h-7 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {NUMBER_FORMATS.map((f) => (
                  <SelectItem key={f} value={f} className="text-xs">
                    {NUMBER_FORMAT_LABELS[f]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldRow>
          {(chart.numberFormat ?? 'decimal') === 'currency' && (
            <FieldRow label="Symbol">
              <Input
                value={chart.currencySymbol ?? '$'}
                onChange={(e) => set('currencySymbol', e.target.value)}
                placeholder="$"
                className="h-7 w-16 text-xs"
              />
            </FieldRow>
          )}
          <FieldRow label="Vs.">
            <div className="flex items-center gap-2">
              <Input
                type="number"
                placeholder="Comparison value"
                value={chart.comparisonValue ?? ''}
                onChange={(e) =>
                  set('comparisonValue', e.target.value === '' ? null : Number(e.target.value))
                }
                className="h-7 flex-1 text-xs"
              />
              <Input
                placeholder="Label (e.g. vs last month)"
                value={chart.comparisonLabel ?? ''}
                onChange={(e) => set('comparisonLabel', e.target.value)}
                className="h-7 flex-1 text-xs"
              />
            </div>
          </FieldRow>
        </>
      )}

      {/* Gauge options */}
      {isGauge && (
        <>
          <FieldRow label="Range">
            <div className="flex items-center gap-1.5">
              <Input
                type="number"
                placeholder="Min"
                value={chart.gaugeMin ?? 0}
                onChange={(e) => set('gaugeMin', Number(e.target.value))}
                className="h-7 w-20 text-xs"
              />
              <span className="text-xs text-muted-foreground">–</span>
              <Input
                type="number"
                placeholder="Max"
                value={chart.gaugeMax ?? 100}
                onChange={(e) => set('gaugeMax', Number(e.target.value))}
                className="h-7 w-20 text-xs"
              />
            </div>
          </FieldRow>
          <FieldRow label="Format">
            <Select
              value={chart.numberFormat ?? 'decimal'}
              onValueChange={(v) => set('numberFormat', v as NumberFormat)}
            >
              <SelectTrigger className="h-7 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {NUMBER_FORMATS.map((f) => (
                  <SelectItem key={f} value={f} className="text-xs">
                    {NUMBER_FORMAT_LABELS[f]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldRow>
        </>
      )}

      {/* Group by + Aggregation (non-scalar, non-pie, non-scatter) */}
      {showGroupBy && (
        <FieldRow label="Group by">
          <div className="flex items-center gap-2">
            <ColSelect
              value={chart.seriesGroup}
              columns={columns.filter((c) => c !== chart.xKey)}
              placeholder="— None —"
              nullable
              onValueChange={(v) => {
                const next: UserChart = { ...chart, seriesGroup: v };
                if (v && next.aggregation === 'none') next.aggregation = 'sum';
                if (!v) next.aggregation = 'none';
                onChange(next);
              }}
            />
            {(needsAggregation || chart.aggregation !== 'none') && (
              <Select
                value={chart.aggregation}
                onValueChange={(v) => set('aggregation', v as AggregationMethod)}
              >
                <SelectTrigger className="h-7 w-32 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AGGREGATIONS.map((a) => (
                    <SelectItem key={a} value={a} className="text-xs">
                      {AGGREGATION_LABELS[a]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        </FieldRow>
      )}

      {/* Options row — stacked, data labels, connect nulls, legend */}
      {!isScalar && (
        <div className="flex flex-wrap items-center gap-4">
          {showStacked && (
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="rounded"
                checked={chart.stacked}
                onChange={(e) => set('stacked', e.target.checked)}
              />
              Stacked
            </label>
          )}
          {showDataLabelsOption && (
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="rounded"
                checked={chart.showDataLabels}
                onChange={(e) => set('showDataLabels', e.target.checked)}
              />
              Data labels
            </label>
          )}
          {chart.type !== 'scatter' && (
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="rounded"
                checked={chart.filterNullX}
                onChange={(e) => set('filterNullX', e.target.checked)}
              />
              Exclude nulls
            </label>
          )}
          {showConnectNulls && (
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="rounded"
                checked={chart.connectNulls}
                onChange={(e) => set('connectNulls', e.target.checked)}
              />
              Connect nulls
            </label>
          )}
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              className="rounded"
              checked={chart.showLegend}
              onChange={(e) => set('showLegend', e.target.checked)}
            />
            Show legend
          </label>
          {chart.showLegend && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Position</span>
              <Select
                value={chart.legendPosition}
                onValueChange={(v) => set('legendPosition', v as LegendPosition)}
              >
                <SelectTrigger className="h-6 w-24 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {LEGEND_POSITIONS.map((p) => (
                    <SelectItem key={p} value={p} className="text-xs">
                      {LEGEND_POSITION_LABELS[p]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}

      {/* Collapsible trend line section */}
      {showTrendToggle && (
        <div>
          <button
            onClick={() => setTrendOpen((o) => !o)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {trendOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            Trend line
            {chart.showTrendLine && (
              <span className="ml-1 rounded-full bg-primary/15 px-1.5 py-0 text-[10px] font-medium text-primary">
                on
              </span>
            )}
          </button>
          {trendOpen && (
            <div className="mt-2 space-y-2 pl-4">
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  className="rounded"
                  checked={chart.showTrendLine ?? false}
                  onChange={(e) => set('showTrendLine', e.target.checked)}
                />
                Show trend line
              </label>
              {chart.showTrendLine && (
                <>
                  <div className="flex items-center gap-3">
                    <Label className="w-20 shrink-0 text-xs text-muted-foreground">Type</Label>
                    <Select
                      value={chart.trendLineType ?? 'linear'}
                      onValueChange={(v) => set('trendLineType', v as 'linear' | 'moving_avg')}
                    >
                      <SelectTrigger className="h-7 w-36 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="linear" className="text-xs">Linear regression</SelectItem>
                        <SelectItem value="moving_avg" className="text-xs">Moving average</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  {(chart.trendLineType ?? 'linear') === 'moving_avg' && (
                    <div className="flex items-center gap-3">
                      <Label className="w-20 shrink-0 text-xs text-muted-foreground">Window</Label>
                      <Input
                        type="number"
                        min={2}
                        max={20}
                        value={chart.movingAvgWindow ?? 3}
                        onChange={(e) => set('movingAvgWindow', Math.max(2, Number(e.target.value)))}
                        className="h-7 w-20 text-xs"
                      />
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* Collapsible filters */}
      <div>
        <div className="flex items-center justify-between">
          <button
            onClick={() => setFiltersOpen((o) => !o)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {filtersOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            Filters
            {filters.length > 0 && (
              <span className="ml-1 rounded-full bg-primary/15 px-1.5 py-0 text-[10px] font-medium text-primary">
                {filters.length}
              </span>
            )}
          </button>
          <button
            onClick={addFilter}
            className="flex items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <Plus className="h-3 w-3" />
            Add
          </button>
        </div>
        {filtersOpen && filters.length > 0 && (
          <div className="mt-2 space-y-1.5 pl-4">
            {filters.map((f, idx) => (
              <div key={idx} className="flex items-center gap-1.5">
                <select
                  value={f.column}
                  onChange={(e) => updateFilter(idx, { column: e.target.value })}
                  className="h-7 flex-1 rounded border border-border bg-background px-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {columns.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <select
                  value={f.operator}
                  onChange={(e) => updateFilter(idx, { operator: e.target.value as FilterOperator })}
                  className="h-7 w-20 rounded border border-border bg-background px-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                >
                  {FILTER_OPERATORS.map((op) => (
                    <option key={op.value} value={op.value}>{op.label}</option>
                  ))}
                </select>
                <Input
                  value={f.value}
                  onChange={(e) => updateFilter(idx, { value: e.target.value })}
                  placeholder="value"
                  className="h-7 flex-1 text-xs"
                />
                <button
                  onClick={() => removeFilter(idx)}
                  className="text-muted-foreground hover:text-destructive"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Collapsible axis options */}
      {showAxisControls && (
        <div>
          <button
            onClick={() => setAxisOpen((o) => !o)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {axisOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            Axis options
          </button>
          {axisOpen && (
            <div className="mt-2 space-y-2 pl-4">
              <div className="flex items-center gap-3">
                <Label className="w-20 shrink-0 text-xs text-muted-foreground">Y scale</Label>
                <Select
                  value={chart.yAxisScale ?? 'linear'}
                  onValueChange={(v) => set('yAxisScale', v as YAxisScale)}
                >
                  <SelectTrigger className="h-7 w-28 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="linear" className="text-xs">Linear</SelectItem>
                    <SelectItem value="log" className="text-xs">Logarithmic</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-center gap-3">
                <Label className="w-20 shrink-0 text-xs text-muted-foreground">Y range</Label>
                <div className="flex items-center gap-1.5">
                  <Input
                    type="number"
                    placeholder="Min"
                    value={chart.yAxisMin ?? ''}
                    onChange={(e) =>
                      set('yAxisMin', e.target.value === '' ? null : Number(e.target.value))
                    }
                    className="h-7 w-20 text-xs"
                  />
                  <span className="text-xs text-muted-foreground">–</span>
                  <Input
                    type="number"
                    placeholder="Max"
                    value={chart.yAxisMax ?? ''}
                    onChange={(e) =>
                      set('yAxisMax', e.target.value === '' ? null : Number(e.target.value))
                    }
                    className="h-7 w-20 text-xs"
                  />
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="w-20 shrink-0 text-xs text-muted-foreground">X labels</Label>
                <Select
                  value={String(chart.xAxisAngle ?? 0)}
                  onValueChange={(v) => set('xAxisAngle', Number(v) as 0 | -45 | -90)}
                >
                  <SelectTrigger className="h-7 w-28 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="0" className="text-xs">Horizontal</SelectItem>
                    <SelectItem value="-45" className="text-xs">−45°</SelectItem>
                    <SelectItem value="-90" className="text-xs">−90°</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
