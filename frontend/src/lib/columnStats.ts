// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Client-side column statistics computed from QueryResults rows. */

import type { QueryResults } from '../types';

export interface ColumnStats {
  name: string;
  type: string;
  nonNullPct: number;
  uniqueCount: number;
  min: number | string | null;
  max: number | string | null;
  mean: number | null;
  topValue: string | null;
}

const NUMERIC_FRAGMENTS = [
  'int', 'bigint', 'smallint', 'tinyint', 'mediumint',
  'float', 'double', 'decimal', 'numeric', 'number', 'real', 'money',
];

function isNumericType(t: string): boolean {
  const lower = t.toLowerCase();
  return NUMERIC_FRAGMENTS.some((f) => lower.includes(f));
}

export function computeColumnStats(results: QueryResults): ColumnStats[] {
  const { columns, column_types, rows, row_count } = results;
  const total = row_count > 0 ? row_count : rows.length;

  return columns.map((name, colIdx) => {
    const type = column_types[colIdx] ?? 'unknown';
    const values = rows.map((r) => r[colIdx]);

    const nonNullValues = values.filter((v) => v !== null && v !== undefined && v !== '');
    const nonNullPct = total > 0 ? (nonNullValues.length / total) * 100 : 0;

    const uniqueSet = new Set(nonNullValues.map(String));
    const uniqueCount = uniqueSet.size;

    if (isNumericType(type)) {
      const nums = nonNullValues.map((v) => parseFloat(String(v))).filter(isFinite);
      if (nums.length === 0) {
        return { name, type, nonNullPct, uniqueCount, min: null, max: null, mean: null, topValue: null };
      }
      const min = Math.min(...nums);
      const max = Math.max(...nums);
      const mean = nums.reduce((a, b) => a + b, 0) / nums.length;
      return { name, type, nonNullPct, uniqueCount, min, max, mean, topValue: null };
    }

    // String / date / other — find most frequent value
    if (nonNullValues.length === 0) {
      return { name, type, nonNullPct, uniqueCount, min: null, max: null, mean: null, topValue: null };
    }

    const freq = new Map<string, number>();
    for (const v of nonNullValues) {
      const s = String(v);
      freq.set(s, (freq.get(s) ?? 0) + 1);
    }
    let topKey = '';
    let topCount = 0;
    for (const [k, c] of freq) {
      if (c > topCount) { topCount = c; topKey = k; }
    }
    const topPct = ((topCount / nonNullValues.length) * 100).toFixed(0);
    const topValue = `${topKey} (${topPct}%)`;

    // min/max as lexicographic for strings, chronological for dates
    const sorted = [...nonNullValues].map(String).sort();
    return {
      name,
      type,
      nonNullPct,
      uniqueCount,
      min: sorted[0] ?? null,
      max: sorted[sorted.length - 1] ?? null,
      mean: null,
      topValue,
    };
  });
}
