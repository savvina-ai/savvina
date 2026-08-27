// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import type {
  AggregationType,
  BusinessMetric,
  MetricType,
  SchemaTable,
  TimeGranularity,
} from '../types';

// ── Enum value lists — mirror backend/app/semantic/models.py ────────────────

export const METRIC_TYPES: MetricType[] = [
  'simple',
  'ratio',
  'derived',
  'cumulative',
  'conversion',
];

export const AGGREGATIONS: AggregationType[] = [
  'sum',
  'count',
  'count_distinct',
  'count_distinct_approx',
  'average',
  'max',
  'min',
  'median',
];

export const TIME_GRANULARITIES: TimeGranularity[] = [
  'second',
  'minute',
  'hour',
  'day',
  'week',
  'month',
  'quarter',
  'year',
];

export const SEMANTIC_TYPES: string[] = [
  'identifier',
  'status_flag',
  'monetary',
  'percentage',
  'timestamp',
  'date',
  'free_text',
  'categorical',
  'boolean_flag',
  'foreign_key',
  'url',
  'email',
  'phone',
  'measurement',
  'count',
  'unknown',
];

export const CARDINALITY_CLASSES: string[] = ['unique', 'low', 'medium', 'high'];

export const FORMAT_HINTS: string[] = [
  'currency_eur',
  'currency_usd',
  'currency_gbp',
  'percentage',
  'integer',
];

// ── Business metric validation ──────────────────────────────────────────────

/**
 * Return why `m` is not a usable metric, or null if it is fine.
 *
 * Mirrors the required fields of the backend's `BusinessMetric` discriminated
 * union. The editor seeds those fields with `''` so switching type never 422s —
 * but an empty string still validates server-side, so a half-filled metric would
 * persist and render as e.g. `() / NULLIF(, 0)` in every later LLM prompt.
 * This is what stops it at the point of editing instead.
 */
export function metricValidationError(m: BusinessMetric): string | null {
  if (!m.name.trim()) return 'Name is required';
  switch (m.metric_type) {
    case 'simple':
    case 'cumulative':
      if (!m.definition?.trim()) return 'Definition is required';
      if (!m.aggregation) return 'Aggregation is required';
      return null;
    case 'derived':
      if (!m.definition?.trim()) return 'Definition is required';
      return null;
    case 'ratio':
      if (!m.numerator_expr?.trim()) return 'Numerator expression is required';
      if (!m.denominator_expr?.trim()) return 'Denominator expression is required';
      return null;
    case 'conversion':
      if (!m.base_measure?.trim()) return 'Base measure is required';
      if (!m.conversion_measure?.trim()) return 'Conversion measure is required';
      return null;
    default:
      return null;
  }
}

// ── Schema lookup ───────────────────────────────────────────────────────────

/**
 * Index schema tables under both `"table"` and `"schema.table"` so a semantic-model
 * key resolves regardless of whether the source has a schema concept.
 */
export function buildSchemaTableMap(
  schemaTables: SchemaTable[] | undefined,
): Map<string, SchemaTable> {
  const m = new Map<string, SchemaTable>();
  schemaTables?.forEach((t) => {
    m.set(t.name, t);
    m.set(`${t.schema_name}.${t.name}`, t);
  });
  return m;
}
