// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Chart types for the user-configurable chart system. */

import type { ElementType } from 'react';
import { BarChart2, LineChart, AreaChart, PieChart, ScatterChart, TrendingUp, Hash, Gauge } from 'lucide-react';

export type ChartType = 'bar' | 'line' | 'area' | 'pie' | 'scatter' | 'combo' | 'number' | 'gauge';
export type NumberFormat = 'integer' | 'decimal' | 'currency' | 'percent';
export type FilterOperator = '=' | '!=' | '>' | '>=' | '<' | '<=' | 'contains';

export interface ChartFilter {
  column: string;
  operator: FilterOperator;
  value: string;
}
export type AggregationMethod = 'none' | 'sum' | 'count' | 'avg' | 'min' | 'max';
export type LegendPosition = 'top' | 'bottom' | 'left' | 'right';
export type YAxisScale = 'linear' | 'log';

/** A single user-created chart attached to a query result. */
export interface UserChart {
  id: string;
  title: string;
  type: ChartType;
  /** Column used as x-axis / category / pie label. */
  xKey: string;
  /** One or more columns used as numeric values (y-axis series). */
  yKeys: string[];
  /**
   * Optional column to group/pivot by — each unique value in this column
   * becomes a separate series. Requires aggregation !== 'none'.
   */
  seriesGroup: string | null;
  /** How to aggregate y-values when grouping (or when reducing duplicate x-values). */
  aggregation: AggregationMethod;
  /** Stack series on top of each other (bar / area / combo-bar). */
  stacked: boolean;
  showLegend: boolean;
  legendPosition: LegendPosition;
  /** Show value labels directly on bars or line points. */
  showDataLabels: boolean;
  /** Y-axis scale: linear (default) or logarithmic. */
  yAxisScale: YAxisScale;
  /** Y-axis domain minimum — null means auto. */
  yAxisMin: number | null;
  /** Y-axis domain maximum — null means auto. */
  yAxisMax: number | null;
  /** X-axis tick label rotation angle in degrees (0, -45, or -90). */
  xAxisAngle: 0 | -45 | -90;
  /** Drop rows where the x-axis column value is null/undefined before rendering. */
  filterNullX: boolean;
  /** For line/area: connect data points across null values instead of showing a gap. */
  connectNulls: boolean;
  /** Combo chart only: maps each yKey to 'bar' or 'line'. Defaults to bar for first, line for rest. */
  comboSeriesTypes: Record<string, 'bar' | 'line'>;
  /** Column filters applied before aggregation (client-side, and server-side when truncated). */
  filters: ChartFilter[];

  // ── Trend line overlay (bar / line / area / combo) ────────────────────────
  showTrendLine: boolean;
  trendLineType: 'linear' | 'moving_avg';
  /** Window size for moving-average trend line (default 3). */
  movingAvgWindow: number;

  // ── Number / KPI card (type === 'number') ─────────────────────────────────
  numberFormat: NumberFormat;
  currencySymbol: string;
  /** Optional reference value shown below the main metric with a trend arrow. */
  comparisonValue: number | null;
  comparisonLabel: string;

  // ── Gauge / radial progress (type === 'gauge') ────────────────────────────
  gaugeMin: number;
  gaugeMax: number;
  /** Threshold bands that control gauge colour. Each entry sets the fill colour
   *  for values >= its `value`. Should be CSS custom properties, e.g. 'hsl(var(--chart-1))'. */
  gaugeThresholds: Array<{ value: number; color: string }>;
}

export const CHART_TYPE_LABELS: Record<ChartType, string> = {
  bar: 'Bar',
  line: 'Line',
  area: 'Area',
  pie: 'Pie',
  scatter: 'Scatter',
  combo: 'Combo',
  number: 'Number',
  gauge: 'Gauge',
};

export const NUMBER_FORMAT_LABELS: Record<NumberFormat, string> = {
  integer: 'Integer',
  decimal: 'Decimal',
  currency: 'Currency',
  percent: 'Percent',
};

export const AGGREGATION_LABELS: Record<AggregationMethod, string> = {
  none: 'None (raw)',
  sum: 'Sum',
  count: 'Count',
  avg: 'Average',
  min: 'Min',
  max: 'Max',
};

export const LEGEND_POSITION_LABELS: Record<LegendPosition, string> = {
  top: 'Top',
  bottom: 'Bottom',
  left: 'Left',
  right: 'Right',
};

export const CHART_TYPES: ChartType[] = [
  'bar', 'line', 'area', 'pie', 'scatter', 'combo', 'number', 'gauge',
];

export const CHART_TYPE_ICONS: Record<ChartType, ElementType> = {
  bar: BarChart2,
  line: LineChart,
  area: AreaChart,
  pie: PieChart,
  scatter: ScatterChart,
  combo: TrendingUp,
  number: Hash,
  gauge: Gauge,
};
