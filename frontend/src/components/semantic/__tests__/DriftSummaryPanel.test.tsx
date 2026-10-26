// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { createRef } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DriftSummaryPanel from '../DriftSummaryPanel';
import { summaryDescription, summaryRows } from '../driftSummaryRows';
import type { DriftApplySummary } from '@/types';

const EMPTY: DriftApplySummary = {
  dry_run: false,
  changed: false,
  tables_added: [],
  tables_removed: [],
  columns_added: {},
  columns_removed: {},
  columns_renamed: {},
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

describe('DriftSummaryPanel', () => {
  it('says when there was nothing to apply', () => {
    render(<DriftSummaryPanel summary={EMPTY} onDismiss={() => {}} />);
    expect(screen.getByText(/Nothing to apply/)).toBeInTheDocument();
    // E.g. another tab applied it first: not a success.
    expect(screen.getByText('Nothing was applied')).toBeInTheDocument();
    expect(screen.queryByText('Schema drift applied')).not.toBeInTheDocument();
  });

  it('does not call an apply that annotated nothing a success', () => {
    // The only drift was a new table the LLM returned nothing for: unchanged, with a skipped table.
    render(
      <DriftSummaryPanel
        summary={{ ...EMPTY, changed: false, tables_not_annotated: ['public.audit'] }}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText('Nothing was applied')).toBeInTheDocument();
    expect(screen.queryByText('Schema drift applied')).not.toBeInTheDocument();
    expect(screen.getByText(/1 new table was not annotated/)).toBeInTheDocument();
    // There was something to apply; it is not "already matches".
    expect(screen.queryByText(/Nothing to apply/)).not.toBeInTheDocument();
  });

  it('can be a focusable region named by its title and described by its counts', () => {
    const many = Array.from({ length: 12 }, (_, i) => `public.t${String(i).padStart(2, '0')}`);
    const ref = createRef<HTMLDivElement>();
    render(
      <DriftSummaryPanel
        summary={{ ...EMPTY, changed: true, dry_run: true, tables_removed: many, metrics_removed: ['A'] }}
        region
        panelRef={ref}
      >
        <button>Apply these changes</button>
      </DriftSummaryPanel>,
    );
    const panel = screen.getByRole('region', { name: 'Apply Drift will:' });
    expect(panel).toBe(ref.current);
    expect(panel).toHaveAttribute('tabindex', '-1');
    // The counts, not the names: the lists are there to read on from the region.
    expect(panel).toHaveAccessibleDescription('Will remove 12 tables · Will remove 1 metric');
    // Focus carries it; nothing in it is a live region.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('is neither a region nor focusable unless asked', () => {
    const { container } = render(
      <DriftSummaryPanel summary={{ ...EMPTY, changed: true, tables_removed: ['a.b'] }} />,
    );
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(container.querySelector('[tabindex]')).toBeNull();
  });

  it('names every removal with a count', () => {
    render(
      <DriftSummaryPanel
        summary={{
          ...EMPTY,
          changed: true,
          tables_removed: ['ecommerce.coupons'],
          columns_removed: { 'ecommerce.orders': ['coupon_id', 'discount'] },
          metrics_removed: ['Coupon uses'],
          relationships_removed: ['ecommerce.orders.coupon_id → ecommerce.coupons.id'],
        }}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText('Schema drift applied')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 table')).toBeInTheDocument();
    expect(screen.getByText('ecommerce.coupons')).toBeInTheDocument();
    expect(screen.getByText('Removed 2 columns')).toBeInTheDocument();
    expect(screen.getByText('ecommerce.orders.coupon_id, ecommerce.orders.discount')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 metric')).toBeInTheDocument();
    expect(screen.getByText('Removed 1 relationship')).toBeInTheDocument();
    // Empty categories are not listed.
    expect(screen.queryByText(/Added/)).not.toBeInTheDocument();
  });

  it('shows additions, skipped tables and warnings', () => {
    render(
      <DriftSummaryPanel
        summary={{
          ...EMPTY,
          changed: true,
          tables_added: ['public.events'],
          metrics_added: ['Events'],
          tables_not_annotated: ['public.audit'],
          warnings: ['A hand edit named a dropped column'],
        }}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText('Added 1 table')).toBeInTheDocument();
    expect(screen.getByText('Added 1 metric')).toBeInTheDocument();
    expect(screen.getByText(/1 new table was not annotated/)).toBeInTheDocument();
    expect(screen.getByText(/Run Check Drift again and apply to retry:/)).toBeInTheDocument();
    expect(screen.getByText('public.audit')).toBeInTheDocument();
    expect(screen.getByText('A hand edit named a dropped column')).toBeInTheDocument();
  });

  it('caps a long list', () => {
    const many = Array.from({ length: 12 }, (_, i) => `public.t${String(i).padStart(2, '0')}`);
    render(
      <DriftSummaryPanel summary={{ ...EMPTY, changed: true, tables_removed: many }} onDismiss={() => {}} />,
    );
    expect(screen.getByText('Removed 12 tables')).toBeInTheDocument();
    expect(screen.getByText(many.slice(0, 8).join(', '))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+4 more' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('expands a capped list to every name', async () => {
    const many = Array.from({ length: 12 }, (_, i) => `public.t${String(i).padStart(2, '0')}`);
    render(<DriftSummaryPanel summary={{ ...EMPTY, changed: true, dry_run: true, tables_removed: many }} />);
    const user = userEvent.setup();
    // From the keyboard: what the banner promises the preview shows is all of it.
    screen.getByRole('button', { name: '+4 more' }).focus();
    await user.keyboard('{Enter}');

    expect(screen.getByText(many.join(', '))).toBeInTheDocument();
    const less = screen.getByRole('button', { name: 'Show fewer' });
    expect(less).toHaveAttribute('aria-expanded', 'true');
    await user.click(less);
    expect(screen.getByText(many.slice(0, 8).join(', '))).toBeInTheDocument();
  });

  it('can be dismissed', async () => {
    const onDismiss = vi.fn();
    render(<DriftSummaryPanel summary={EMPTY} onDismiss={onDismiss} />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it('words a preview in the future tense and renders its actions', () => {
    render(
      <DriftSummaryPanel
        summary={{
          ...EMPTY,
          changed: true,
          dry_run: true,
          tables_removed: ['ecommerce.coupons'],
          tables_added: ['public.events'],
        }}
      >
        <button>Apply these changes</button>
      </DriftSummaryPanel>,
    );
    expect(screen.getByText('Apply Drift will:')).toBeInTheDocument();
    expect(screen.getByText('Will remove 1 table')).toBeInTheDocument();
    expect(screen.getByText('Will add 1 table')).toBeInTheDocument();
    // The preview makes no LLM call: what the apply builds over the new tables, including
    // the catalog's relationships to them, is not in it.
    expect(screen.getByText(/annotated by the LLM when you apply/)).toBeInTheDocument();
    expect(screen.getByText(/Relationships to them, .* appear only then/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply these changes' })).toBeInTheDocument();
    // No onDismiss, no Dismiss button.
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument();
  });

  it('takes a title', () => {
    render(<DriftSummaryPanel summary={EMPTY} title="Applied yesterday" />);
    expect(screen.getByText('Applied yesterday')).toBeInTheDocument();
  });

  it('lists the columns added to existing tables', () => {
    const summary: DriftApplySummary = {
      ...EMPTY,
      changed: true,
      columns_added: { 'public.orders': ['loyalty_tier', 'region'] },
    };
    expect(summaryRows(summary)).toEqual([
      {
        key: 'columns_added',
        label: 'Added 2 columns',
        names: ['public.orders.loyalty_tier', 'public.orders.region'],
      },
    ]);
  });

  it('words a preview of new columns as a plan', () => {
    const summary: DriftApplySummary = {
      ...EMPTY,
      dry_run: true,
      changed: true,
      columns_added: { 'public.orders': ['loyalty_tier'] },
    };
    expect(summaryRows(summary)[0].label).toBe('Will add 1 column');
  });

  it('lists renamed columns as old key to live name, between the removals and the additions', () => {
    const summary: DriftApplySummary = {
      ...EMPTY,
      changed: true,
      columns_removed: { 'public.orders': ['legacy'] },
      columns_renamed: { 'public.orders': { '"status"': 'status', Region: 'region' } },
      columns_added: { 'public.orders': ['tier'] },
    };
    expect(summaryRows(summary)).toEqual([
      { key: 'columns_removed', label: 'Removed 1 column', names: ['public.orders.legacy'] },
      {
        key: 'columns_renamed',
        label: 'Renamed 2 columns',
        names: ['public.orders."status" → status', 'public.orders.Region → region'],
      },
      { key: 'columns_added', label: 'Added 1 column', names: ['public.orders.tier'] },
    ]);
  });

  it('words a preview of a rename as a plan', () => {
    const summary: DriftApplySummary = {
      ...EMPTY,
      dry_run: true,
      changed: true,
      columns_renamed: { 'public.orders': { '"status"': 'status' } },
    };
    expect(summaryRows(summary).map((r) => r.label)).toEqual(['Will rename 1 column']);
  });

  it('does not call a failed new-column batch nothing to apply', () => {
    // Unchanged, but a column batch failed: the warnings say columns are missing, so the
    // panel must not say the model matches the live schema.
    const summary: DriftApplySummary = { ...EMPTY, column_tables_failed: ['public.orders'] };
    expect(summaryDescription(summary)).toBe('New columns on 1 table not annotated');
    render(<DriftSummaryPanel summary={summary} onDismiss={() => {}} />);
    expect(screen.queryByText(/Nothing to apply/)).not.toBeInTheDocument();
  });

  it('says a preview of new columns annotates them on apply, with the catalog relationships it adds', () => {
    render(
      <DriftSummaryPanel
        summary={{
          ...EMPTY,
          changed: true,
          dry_run: true,
          columns_added: { 'public.orders': ['customer_id'] },
          relationships_added: ['public.orders.customer_id → public.customers.id'],
        }}
      />,
    );
    expect(screen.getByText(/New columns are annotated by the LLM when you apply/)).toBeInTheDocument();
    expect(screen.getByText(/relationships the database catalog adds on them between tables the model already holds/)).toBeInTheDocument();
  });

  it('adds no new-column note to a real apply', () => {
    render(
      <DriftSummaryPanel
        summary={{ ...EMPTY, changed: true, columns_added: { 'public.orders': ['region'] } }}
      />,
    );
    expect(screen.queryByText(/annotated by the LLM when you apply/)).not.toBeInTheDocument();
  });

  it('labels the non-empty rows for reuse', () => {
    expect(
      summaryRows({ ...EMPTY, changed: true, metrics_removed: ['A', 'B'] }).map((r) => r.label),
    ).toEqual(['Removed 2 metrics']);
  });
});
