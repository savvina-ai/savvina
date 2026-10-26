// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DriftHistory from '../DriftHistory';
import type { DriftApplyHistoryEntry, DriftApplySummary } from '@/types';

const EMPTY: DriftApplySummary = {
  dry_run: false,
  changed: false,
  tables_added: [],
  tables_removed: [],
  columns_added: {},
  columns_removed: {},
  metrics_added: [],
  metrics_removed: [],
  joins_added: [],
  joins_removed: [],
  derived_columns_added: [],
  derived_columns_removed: [],
  segments_added: [],
  segments_removed: [],
  relationships_added: [],
  relationships_removed: [],
  tables_not_annotated: [],
  column_tables_failed: [],
  warnings: [],
};

const ENTRY: DriftApplyHistoryEntry = {
  applied_at: '2026-10-16T09:30:00Z',
  applied_by: 'u1',
  summary: {
    ...EMPTY,
    changed: true,
    tables_removed: ['ecommerce.coupons'],
    metrics_removed: ['A', 'B'],
  },
};

describe('DriftHistory', () => {
  it('renders nothing without entries', () => {
    const { container } = render(<DriftHistory entries={[]} limit={10} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('lists each apply with its counts', () => {
    render(<DriftHistory entries={[ENTRY, { ...ENTRY, summary: EMPTY }]} limit={10} />);
    expect(screen.getByText('Drift history (2)')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Removed 1 table · Removed 2 metrics/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /No changes/ })).toBeInTheDocument();
  });

  it('agrees with the panel on an apply that annotated nothing', async () => {
    render(
      <DriftHistory
        entries={[{ ...ENTRY, summary: { ...EMPTY, changed: false, tables_not_annotated: ['public.audit'] } }]}
        limit={10}
      />,
    );
    const entry = screen.getByRole('button', { name: /No changes/ });
    await userEvent.setup().click(entry);
    // Its own title, but not the success styling or wording.
    expect(screen.getByText(/^Applied /)).toBeInTheDocument();
    expect(screen.getByText(/1 new table was not annotated/)).toBeInTheDocument();
    expect(screen.getByText(/^Applied /).closest('.rounded-xl')).not.toHaveClass('bg-success/10');
    // A past apply is looked up, not announced or focused.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
  });

  it('does not pass a full page off as the total', () => {
    render(
      <DriftHistory
        entries={[ENTRY, { ...ENTRY, applied_at: '2026-10-15T09:30:00Z' }]}
        limit={2}
      />,
    );
    expect(screen.getByText('Drift history (last 2)')).toBeInTheDocument();
  });

  it('expands an entry to its full summary', async () => {
    render(<DriftHistory entries={[ENTRY]} limit={10} />);
    const entry = screen.getByRole('button', { name: /Removed 1 table/ });
    expect(entry).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('ecommerce.coupons')).not.toBeInTheDocument();

    await userEvent.setup().click(entry);
    expect(entry).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('ecommerce.coupons')).toBeInTheDocument();
    expect(screen.getByText(/^Applied /)).toBeInTheDocument();
  });
});
