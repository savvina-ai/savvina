// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PrivacySettingsForm from '../PrivacySettings';
import type { PrivacySettings } from '../../types';

function makeSettings(overrides?: Partial<PrivacySettings>): PrivacySettings {
  return {
    include_sample_values: false,
    include_column_comments: false,
    include_row_counts: false,
    sensitive_column_patterns: ['password'],
    excluded_schemas: [],
    excluded_tables: ['audit_log'],
    excluded_columns: [],
    row_filters: {},
    ...overrides,
  };
}

function lastEmitted(onChange: ReturnType<typeof vi.fn>): PrivacySettings {
  return onChange.mock.calls[onChange.mock.calls.length - 1][0] as PrivacySettings;
}

describe('PrivacySettingsForm row filters', () => {
  it('shows no rows when the map is empty', () => {
    render(<PrivacySettingsForm settings={makeSettings()} onChange={vi.fn()} />);
    expect(screen.queryByLabelText('Table 1')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add filter' })).toBeInTheDocument();
  });

  it('shows one row per stored entry', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: { 'public.orders': 'tenant_id = 42' } })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Table 1')).toHaveValue('public.orders');
    expect(screen.getByLabelText('Condition 1')).toHaveValue('tenant_id = 42');
  });

  it('renders an empty list for a legacy settings object without row_filters', () => {
    const legacy = { ...makeSettings(), row_filters: undefined, row_filter_sql: 'tenant_id = 42' };
    render(
      <PrivacySettingsForm settings={legacy as unknown as PrivacySettings} onChange={vi.fn()} />,
    );
    expect(screen.queryByLabelText('Table 1')).not.toBeInTheDocument();
  });

  it('adding a row and filling it emits the map', async () => {
    const onChange = vi.fn();
    render(<PrivacySettingsForm settings={makeSettings()} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public.orders' } });
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });

    expect(lastEmitted(onChange).row_filters).toEqual({ 'public.orders': 'tenant_id = 7' });
  });

  it('a row with an empty table name is kept locally but not emitted', async () => {
    const onChange = vi.fn();
    render(<PrivacySettingsForm settings={makeSettings()} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });

    expect(screen.getByLabelText('Condition 1')).toHaveValue('tenant_id = 7');
    expect(lastEmitted(onChange).row_filters).toEqual({});
  });

  it('removing a row drops its key', async () => {
    const onChange = vi.fn();
    render(
      <PrivacySettingsForm
        settings={makeSettings({
          row_filters: { 'public.orders': 'tenant_id = 42', 'public.items': 'org_id = 1' },
        })}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Remove filter 1' }));

    expect(lastEmitted(onChange).row_filters).toEqual({ 'public.items': 'org_id = 1' });
  });

  it('a row added after the parent replaces the map does not share an id with a stored row', async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <PrivacySettingsForm settings={makeSettings()} onChange={onChange} />,
    );
    rerender(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: { 'public.orders': 'tenant_id = 42' } })}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 2'), { target: { value: 'public.items' } });

    expect(screen.getByLabelText('Table 1')).toHaveValue('public.orders');
    expect(lastEmitted(onChange).row_filters).toEqual({
      'public.orders': 'tenant_id = 42',
      'public.items': '',
    });
  });

  it('lists the known tables in the datalist', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings()}
        onChange={vi.fn()}
        knownTables={['public.orders', 'public.items']}
      />,
    );
    const options = Array.from(document.querySelectorAll('datalist option')).map(
      (o) => (o as HTMLOptionElement).value,
    );
    expect(options).toEqual(['public.orders', 'public.items']);
  });

  it('keeps the row filters when resetting to defaults', async () => {
    const onChange = vi.fn();
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: { 'public.orders': 'tenant_id = 42' } })}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));

    const next = lastEmitted(onChange);
    expect(next.row_filters).toEqual({ 'public.orders': 'tenant_id = 42' });
    expect(next.excluded_tables).toEqual([]);
  });
});

describe('PrivacySettingsForm row filter validation', () => {
  const dupes = { 'public.orders': 'tenant_id = 42' };

  it('flags the later of two rows with the same table and reports invalid', async () => {
    const onValidityChange = vi.fn();
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: dupes })}
        onChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );
    expect(onValidityChange).toHaveBeenLastCalledWith(true);

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 2'), { target: { value: ' public.orders ' } });

    expect(screen.getByText('This table already has a filter')).toBeInTheDocument();
    expect(screen.getByLabelText('Table 2')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Table 1')).not.toHaveAttribute('aria-invalid', 'true');
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    const describedBy = screen.getByLabelText('Table 2').getAttribute('aria-describedby');
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      'This table already has a filter',
    );

    fireEvent.change(screen.getByLabelText('Table 2'), { target: { value: 'public.items' } });

    expect(screen.queryByText('This table already has a filter')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Table 2')).not.toHaveAttribute('aria-invalid', 'true');
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it('flags a condition without a table but not a fully empty row', async () => {
    const onValidityChange = vi.fn();
    render(
      <PrivacySettingsForm
        settings={makeSettings()}
        onChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    expect(screen.queryByText('Choose a table for this condition')).not.toBeInTheDocument();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);

    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });

    expect(screen.getByText('Choose a table for this condition')).toBeInTheDocument();
    expect(screen.getByLabelText('Table 1')).toHaveAttribute('aria-invalid', 'true');
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public.orders' } });

    expect(screen.queryByText('Choose a table for this condition')).not.toBeInTheDocument();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it('keeps a half-typed row across parent re-renders when the parent holds the state', async () => {
    function Wrapper() {
      const [settings, setSettings] = useState(makeSettings());
      return <PrivacySettingsForm settings={settings} onChange={setSettings} />;
    }
    render(<Wrapper />);

    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'tenant_id = 7' } });
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 2'), { target: { value: 'public.items' } });

    expect(screen.getByLabelText('Table 1')).toHaveValue('');
    expect(screen.getByLabelText('Condition 1')).toHaveValue('tenant_id = 7');
    expect(screen.getByLabelText('Table 2')).toHaveValue('public.items');
  });
});
