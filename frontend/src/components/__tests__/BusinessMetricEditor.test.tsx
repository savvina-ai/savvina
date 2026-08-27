// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BusinessMetricEditor from '../BusinessMetricEditor';
import { metricValidationError } from '../../lib/semanticUtils';
import type { BusinessMetric } from '../../types';

function simpleMetric(overrides: Partial<BusinessMetric> = {}): BusinessMetric {
  return {
    metric_type: 'simple',
    name: 'Revenue',
    definition: 'SUM(orders.total)',
    description: 'Total revenue',
    filters: [],
    related_tables: [],
    aggregation: 'sum',
    ...overrides,
  };
}

describe('BusinessMetricEditor — new metric payload', () => {
  // The backend models BusinessMetric as a discriminated union tagged by metric_type,
  // with no tag-defaulting validator on the update path. A metric without metric_type
  // (or without the aggregation SimpleMetric requires) 422s the whole model save.
  it('seeds metric_type and aggregation so a new metric does not 422', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<BusinessMetricEditor metrics={[]} onChange={onChange} />);

    await user.click(screen.getByText('+ Add metric'));

    expect(onChange).toHaveBeenCalledTimes(1);
    const [added] = onChange.mock.calls[0][0] as BusinessMetric[];
    expect(added.metric_type).toBe('simple');
    expect(added.aggregation).toBe('sum');
  });

  it('seeds the fields the new variant requires when the type is switched', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<BusinessMetricEditor metrics={[simpleMetric()]} onChange={onChange} />);

    await user.click(screen.getByText('▶'));
    await user.selectOptions(screen.getByLabelText('Type'), 'ratio');

    const [updated] = onChange.mock.calls[0][0] as BusinessMetric[];
    expect(updated.metric_type).toBe('ratio');
    expect(updated.numerator_expr).toBe('');
    expect(updated.denominator_expr).toBe('');
  });

  it('renders a conversion metric without a definition field', async () => {
    const user = userEvent.setup();
    const metric = simpleMetric({
      metric_type: 'conversion',
      definition: undefined,
      base_measure: 'sessions',
      conversion_measure: 'signups',
      calculation: 'conversion_rate',
    });
    render(<BusinessMetricEditor metrics={[metric]} onChange={vi.fn()} />);

    await user.click(screen.getByText('▶'));

    expect(screen.queryByLabelText('Definition')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Base measure')).toHaveValue('sessions');
  });
});

describe('metricValidationError', () => {
  it('accepts a fully specified simple metric', () => {
    expect(metricValidationError(simpleMetric())).toBeNull();
  });

  it('flags a ratio metric missing its expressions', () => {
    const m = simpleMetric({ metric_type: 'ratio', numerator_expr: '', denominator_expr: '' });
    expect(metricValidationError(m)).toBe('Numerator expression is required');
  });

  it('flags a simple metric with no aggregation', () => {
    expect(metricValidationError(simpleMetric({ aggregation: undefined }))).toBe(
      'Aggregation is required',
    );
  });
});
