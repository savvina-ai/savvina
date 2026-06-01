// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Transform QueryResults rows into chart-ready data with optional grouping and aggregation. */

import type { QueryResults } from '../types';
import type { AggregationMethod, ChartFilter, UserChart } from '../types/charts';

function filterPasses(value: unknown, op: ChartFilter['operator'], target: string): boolean {
  if (value === null || value === undefined) return false;
  const str = String(value);
  if (op === '=') return str === target;
  if (op === '!=') return str !== target;
  if (op === 'contains') return str.toLowerCase().includes(target.toLowerCase());
  const num = parseFloat(str);
  const tgt = parseFloat(target);
  if (isNaN(num) || isNaN(tgt)) return false;
  if (op === '>') return num > tgt;
  if (op === '>=') return num >= tgt;
  if (op === '<') return num < tgt;
  if (op === '<=') return num <= tgt;
  return false;
}

function applyChartFilters(
  records: Record<string, unknown>[],
  filters: ChartFilter[],
): Record<string, unknown>[] {
  if (!filters || filters.length === 0) return records;
  return records.filter((rec) =>
    filters.every((f) => f.column && f.value !== '' && filterPasses(rec[f.column], f.operator, f.value)),
  );
}

function applyAggregation(values: number[], method: AggregationMethod): number {
  if (values.length === 0) return 0;
  switch (method) {
    case 'sum': return values.reduce((a, b) => a + b, 0);
    case 'count': return values.length;
    case 'avg': return values.reduce((a, b) => a + b, 0) / values.length;
    case 'min': return Math.min(...values);
    case 'max': return Math.max(...values);
    default: return values[0];
  }
}

export interface TransformedData {
  data: Record<string, unknown>[];
  /** Actual yKeys to use in the chart — may differ from chart.yKeys when pivoted. */
  yKeys: string[];
}

/**
 * Convert QueryResults to the record format Recharts expects, applying
 * grouping and aggregation as specified by the UserChart configuration.
 *
 * Three modes:
 *  - aggregation=none  : raw data, no transformation
 *  - aggregation+groupBy : group by [xKey, seriesGroup], pivot seriesGroup into columns
 *  - aggregation only  : group by xKey, aggregate each yKey
 */
export function transformChartData(results: QueryResults, chart: UserChart): TransformedData {
  const { columns, rows } = results;

  // Row → object
  const rawRecords: Record<string, unknown>[] = rows.map((row) => {
    const obj: Record<string, unknown> = {};
    columns.forEach((col, i) => {
      obj[col] = row[i];
    });
    return obj;
  });

  const filtered = chart.filterNullX && chart.xKey
    ? rawRecords.filter((rec) => rec[chart.xKey] !== null && rec[chart.xKey] !== undefined)
    : rawRecords;

  const records = applyChartFilters(filtered, chart.filters ?? []);

  // No aggregation or no x-axis selected — return records with yKey values coerced
  // to numbers so that Recharts charts (especially Pie) can compute sizes correctly.
  // SQL DECIMAL/MONEY types often arrive as strings from the backend JSON serializer.
  if (chart.aggregation === 'none' || !chart.xKey) {
    const coerced = records.map((rec) => {
      const obj = { ...rec };
      for (const yKey of chart.yKeys) {
        const raw = rec[yKey];
        if (typeof raw !== 'number') {
          const n = parseFloat(String(raw ?? ''));
          obj[yKey] = isNaN(n) ? null : n;
        }
      }
      return obj;
    });
    return { data: coerced, yKeys: chart.yKeys };
  }

  // ── Aggregation WITH series group (pivot) ──────────────────────────────────
  if (chart.seriesGroup) {
    const yKey = chart.yKeys[0];
    if (!yKey) return { data: records, yKeys: chart.yKeys };

    const xOrder: string[] = [];
    const seenX = new Set<string>();
    const seriesValues = new Set<string>();
    // Map: xValue → (seriesValue → numeric values[])
    const groups = new Map<string, Map<string, number[]>>();

    for (const rec of records) {
      const x = String(rec[chart.xKey] ?? '');
      const s = String(rec[chart.seriesGroup] ?? '');
      const raw = rec[yKey];
      const y = typeof raw === 'number' ? raw : parseFloat(String(raw ?? '0'));

      seriesValues.add(s);
      if (!seenX.has(x)) {
        xOrder.push(x);
        seenX.add(x);
      }
      if (!groups.has(x)) groups.set(x, new Map());
      const xGroup = groups.get(x)!;
      if (!xGroup.has(s)) xGroup.set(s, []);
      xGroup.get(s)!.push(isNaN(y) ? 0 : y);
    }

    const pivotYKeys = Array.from(seriesValues).sort();
    const data = xOrder.map((x) => {
      const obj: Record<string, unknown> = { [chart.xKey]: x };
      const xGroup = groups.get(x)!;
      for (const s of pivotYKeys) {
        obj[s] = applyAggregation(xGroup.get(s) ?? [], chart.aggregation);
      }
      return obj;
    });

    return { data, yKeys: pivotYKeys };
  }

  // ── Aggregation WITHOUT series group (group by xKey) ───────────────────────
  const xOrder: string[] = [];
  const seenX = new Set<string>();
  // Map: xValue → (yKey → numeric values[])
  const groups = new Map<string, Map<string, number[]>>();

  for (const rec of records) {
    const x = String(rec[chart.xKey] ?? '');
    if (!seenX.has(x)) {
      xOrder.push(x);
      seenX.add(x);
    }
    if (!groups.has(x)) groups.set(x, new Map());
    const xGroup = groups.get(x)!;
    for (const yKey of chart.yKeys) {
      const raw = rec[yKey];
      const y = typeof raw === 'number' ? raw : parseFloat(String(raw ?? '0'));
      if (!xGroup.has(yKey)) xGroup.set(yKey, []);
      xGroup.get(yKey)!.push(isNaN(y) ? 0 : y);
    }
  }

  const data = xOrder.map((x) => {
    const obj: Record<string, unknown> = { [chart.xKey]: x };
    const xGroup = groups.get(x)!;
    for (const yKey of chart.yKeys) {
      obj[yKey] = applyAggregation(xGroup.get(yKey) ?? [], chart.aggregation);
    }
    return obj;
  });

  return { data, yKeys: chart.yKeys };
}

/**
 * Compute a trend line over `data[yKey]` and attach it as `__trend__` on each
 * data point. Returns a new array; the original is not mutated.
 *
 * - linear     : least-squares linear regression over the row index as x-axis.
 * - moving_avg : causal (trailing) moving average with the given window size.
 */
export function computeTrendLine(
  data: Record<string, unknown>[],
  yKey: string,
  type: 'linear' | 'moving_avg',
  window: number,
): Record<string, unknown>[] {
  const n = data.length;
  if (n === 0) return data;

  const ys = data.map((d) => {
    const raw = d[yKey];
    const v = typeof raw === 'number' ? raw : parseFloat(String(raw ?? ''));
    return isNaN(v) ? 0 : v;
  });

  const trends: number[] = new Array(n);

  if (type === 'moving_avg') {
    const w = Math.max(1, window);
    for (let i = 0; i < n; i++) {
      const start = Math.max(0, i - w + 1);
      const slice = ys.slice(start, i + 1);
      trends[i] = slice.reduce((a, b) => a + b, 0) / slice.length;
    }
  } else {
    // Least-squares linear regression: x = row index
    const sumX = (n * (n - 1)) / 2;
    const sumX2 = (n * (n - 1) * (2 * n - 1)) / 6;
    const sumY = ys.reduce((a, b) => a + b, 0);
    const sumXY = ys.reduce((acc, y, i) => acc + i * y, 0);
    const denom = n * sumX2 - sumX * sumX;
    if (denom === 0) {
      trends.fill(sumY / n);
    } else {
      const slope = (n * sumXY - sumX * sumY) / denom;
      const intercept = (sumY - slope * sumX) / n;
      for (let i = 0; i < n; i++) {
        trends[i] = slope * i + intercept;
      }
    }
  }

  return data.map((d, i) => ({ ...d, __trend__: trends[i] }));
}
