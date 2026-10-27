// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

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
    row_filter_sql: null,
    ...overrides,
  };
}

describe('PrivacySettingsForm row filter', () => {
  it('shows an empty input when no filter is stored', () => {
    render(<PrivacySettingsForm settings={makeSettings()} onChange={vi.fn()} />);
    expect(screen.getByLabelText('Row filter')).toHaveValue('');
  });

  it('shows the stored filter text', () => {
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filter_sql: 'tenant_id = 42' })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Row filter')).toHaveValue('tenant_id = 42');
  });

  it('reports the typed text as row_filter_sql', () => {
    const onChange = vi.fn();
    render(<PrivacySettingsForm settings={makeSettings()} onChange={onChange} />);

    // The form is controlled and the settings prop never changes here, so a change
    // event carries the whole value; use it rather than per-key typing.
    fireEvent.change(screen.getByLabelText('Row filter'), { target: { value: 'tenant_id = 7' } });

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toMatchObject({ row_filter_sql: 'tenant_id = 7' });
  });

  it('keeps the row filter when resetting to defaults', async () => {
    // A null filter on PUT means "unchanged", so Reset must not blank the input while the
    // server keeps the filter.
    const onChange = vi.fn();
    render(
      <PrivacySettingsForm
        settings={makeSettings({ row_filter_sql: 'tenant_id = 42' })}
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as PrivacySettings;
    expect(next.row_filter_sql).toBe('tenant_id = 42');
    expect(next.excluded_tables).toEqual([]);
  });
});
