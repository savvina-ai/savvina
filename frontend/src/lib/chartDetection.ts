// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Suggest default chart configuration based on QueryResults shape. */

import type { QueryResults } from '../types';
import type { UserChart } from '../types/charts';

const NUMERIC_FRAGMENTS = [
  'int', 'bigint', 'smallint', 'tinyint', 'mediumint',
  'float', 'double', 'decimal', 'numeric', 'number', 'real', 'money',
];
const DATE_FRAGMENTS = ['date', 'timestamp', 'datetime', 'time'];

function isNumericType(t: string): boolean {
  const lower = t.toLowerCase();
  return NUMERIC_FRAGMENTS.some((f) => lower.includes(f));
}

function isDateType(t: string): boolean {
  const lower = t.toLowerCase();
  return DATE_FRAGMENTS.some((f) => lower.includes(f));
}

/**
 * Analyse a QueryResults object and return suggested defaults for a new chart.
 * The caller adds the `id` field when persisting to state.
 *
 * Rules (first match wins):
 *  1. Date column + numeric column(s)  → line chart (time series)
 *  2. Label column(s) + numeric col(s) → bar chart
 *  3. Fallback                         → bar chart with first two columns
 *
 * Pie is intentionally not auto-suggested — bar is a safe default for all
 * categorical queries and the user can switch to pie via the chart type picker.
 */
export function suggestChart(results: QueryResults): Omit<UserChart, 'id'> {
  const { columns, column_types, rows } = results;

  const dateCols = columns.filter((_, i) => isDateType(column_types[i] ?? ''));
  const numericCols = columns.filter((_, i) => isNumericType(column_types[i] ?? ''));
  const labelCols = columns.filter(
    (_, i) => !isDateType(column_types[i] ?? '') && !isNumericType(column_types[i] ?? ''),
  );

  const defaults: Omit<UserChart, 'id' | 'title' | 'type' | 'xKey' | 'yKeys'> = {
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

  // 1. Single scalar value — Number card or Gauge
  const GAUGE_PATTERNS = ['rate', 'pct', 'percent', 'utilisation', 'utilization', 'score'];
  if (rows.length === 1 && numericCols.length === 1) {
    const col = numericCols[0];
    const isGauge = GAUGE_PATTERNS.some((p) => col.toLowerCase().includes(p));
    return {
      ...defaults,
      title: 'Chart 1',
      type: isGauge ? 'gauge' : 'number',
      xKey: '',
      yKeys: [col],
    };
  }

  // 2. Time series
  if (dateCols.length >= 1 && numericCols.length >= 1) {
    return { ...defaults, title: 'Chart 1', type: 'line', xKey: dateCols[0], yKeys: numericCols };
  }

  // 3. Bar — label + numeric
  if (labelCols.length >= 1 && numericCols.length >= 1) {
    return { ...defaults, title: 'Chart 1', type: 'bar', xKey: labelCols[0], yKeys: numericCols };
  }

  // 4. Fallback — first two columns
  return {
    ...defaults,
    title: 'Chart 1',
    type: 'bar',
    xKey: columns[0] ?? '',
    yKeys: columns.length > 1 ? [columns[1]] : [],
  };
}
