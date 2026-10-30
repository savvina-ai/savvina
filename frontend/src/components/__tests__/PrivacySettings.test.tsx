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

  it('shows a PostgreSQL key part with quotes where an unquoted save would fold it', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings({
          row_filters: { 'public.Orders': 'x = 1', 'public.orders_2024': 'y = 1' },
        })}
        onChange={vi.fn()}
        dialect="postgresql"
      />,
    );
    expect(screen.getByLabelText('Table 1')).toHaveValue('public."Orders"');
    expect(screen.getByLabelText('Table 2')).toHaveValue('public.orders_2024');
  });

  it('shows a MySQL key as stored unless a part needs quoting', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: { 'db.Orders': 'x = 1', 'db.order-items': 'y = 1' } })}
        onChange={vi.fn()}
        dialect="mysql"
      />,
    );
    expect(screen.getByLabelText('Table 1')).toHaveValue('db.Orders');
    expect(screen.getByLabelText('Table 2')).toHaveValue('db.`order-items`');
  });

  it('shows a key as stored when the dialect is unknown', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filters: { 'public.Orders': 'x = 1' } })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Table 1')).toHaveValue('public.Orders');
  });

  it('a quoted key the admin types is emitted as typed and kept after the parent stores it', async () => {
    function Parent() {
      const [settings, setSettings] = useState(makeSettings());
      return (
        <PrivacySettingsForm settings={settings} onChange={setSettings} dialect="postgresql" />
      );
    }
    render(<Parent />);
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Table 1'), { target: { value: 'public."Orders"' } });
    fireEvent.change(screen.getByLabelText('Condition 1'), { target: { value: 'x = 1' } });
    // The parent now holds {'public."Orders"': 'x = 1'}; the row must not be re-quoted.
    expect(screen.getByLabelText('Table 1')).toHaveValue('public."Orders"');
  });

  it('leaves a bare key alone while the admin is typing it', async () => {
    // Each keystroke hands the parent the raw map; quoting applies to a stored key,
    // never to what the rows themselves just emitted.
    function Parent() {
      const [settings, setSettings] = useState(makeSettings());
      return (
        <PrivacySettingsForm settings={settings} onChange={setSettings} dialect="postgresql" />
      );
    }
    render(<Parent />);
    await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    await userEvent.type(screen.getByLabelText('Table 1'), 'public.Order-items');
    expect(screen.getByLabelText('Table 1')).toHaveValue('public.Order-items');
  });

  it('lists the known tables with the dialect quotes', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings()}
        onChange={vi.fn()}
        knownTables={['public.Orders', 'public.items']}
        dialect="postgresql"
      />,
    );
    const options = Array.from(document.querySelectorAll('datalist option')).map(
      (o) => (o as HTMLOptionElement).value,
    );
    expect(options).toEqual(['public."Orders"', 'public.items']);
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

describe('PrivacySettingsForm privilege check line', () => {
  const checked = {
    checked_at: '2026-10-29T10:00:00+00:00',
    role: 'savvina',
    status: 'ok' as const,
    error: null,
    findings: [],
    rls_in_use: false,
    rls_effective: {},
  };

  it('shows the checked line for ok without findings', () => {
    render(<PrivacySettingsForm settings={makeSettings()} onChange={vi.fn()} audit={checked} />);
    const line = screen.getByText(/^Privileges checked .*: the role has only the access Savvina needs\.$/);
    expect(line).toHaveClass('text-muted-foreground');
    expect(line.textContent).toContain(new Date(checked.checked_at).toLocaleString());
  });

  it('shows nothing with findings, when unavailable, or without an audit', () => {
    const { rerender } = render(
      <PrivacySettingsForm
        settings={makeSettings()}
        onChange={vi.fn()}
        audit={{ ...checked, findings: [{ code: 'superuser', tables: [], functions: [], more_tables: 0, remediation: 'x' }] }}
      />,
    );
    expect(screen.queryByText(/Privileges checked/)).not.toBeInTheDocument();
    rerender(
      <PrivacySettingsForm
        settings={makeSettings()}
        onChange={vi.fn()}
        audit={{ ...checked, status: 'unavailable', error: 'x' }}
      />,
    );
    expect(screen.queryByText(/Privileges checked/)).not.toBeInTheDocument();
    rerender(<PrivacySettingsForm settings={makeSettings()} onChange={vi.fn()} audit={null} />);
    expect(screen.queryByText(/Privileges checked/)).not.toBeInTheDocument();
  });
});
