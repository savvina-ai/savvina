// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FeedbackCorrectionForm from '../FeedbackCorrectionForm';

const mockUseSemanticModel = vi.fn();
vi.mock('../../hooks/useSemanticModel', () => ({
  useSemanticModel: (id: string) => mockUseSemanticModel(id),
}));

const MODEL = {
  tables: {
    'store.orders': {
      display_name: 'Orders',
      description: null,
      default_filters: [],
      columns: {
        status: {
          display_name: 'Status',
          description: null,
          value_mappings: [],
          is_sensitive: false,
        },
      },
    },
  },
};

beforeEach(() => {
  mockUseSemanticModel.mockReset();
});

describe('FeedbackCorrectionForm', () => {
  it('submits a value-mapping correction in the backend payload shape', async () => {
    const user = userEvent.setup();
    mockUseSemanticModel.mockReturnValue({ data: MODEL, isLoading: false });
    const onSubmit = vi.fn();
    render(
      <FeedbackCorrectionForm connectionId="conn-1" onSubmit={onSubmit} onSkip={vi.fn()} />,
    );

    await user.selectOptions(screen.getByLabelText('Table'), 'store.orders');
    await user.selectOptions(screen.getByLabelText('Column'), 'status');
    await user.type(screen.getByLabelText('Raw value'), 'CMPLT');
    await user.type(screen.getByLabelText('Display value'), 'Completed');
    await user.click(screen.getByText('Submit'));

    expect(onSubmit).toHaveBeenCalledWith({
      table_key: 'store.orders',
      field: 'status',
      correction_type: 'add_value_mapping',
      value: { raw_value: 'CMPLT', display_value: 'Completed' },
    });
  });

  it('submits a default-filter correction with no column', async () => {
    const user = userEvent.setup();
    mockUseSemanticModel.mockReturnValue({ data: MODEL, isLoading: false });
    const onSubmit = vi.fn();
    render(
      <FeedbackCorrectionForm connectionId="conn-1" onSubmit={onSubmit} onSkip={vi.fn()} />,
    );

    await user.selectOptions(screen.getByLabelText('Table'), 'store.orders');
    await user.selectOptions(screen.getByLabelText('Correction type'), 'update_filter');
    await user.type(screen.getByLabelText('Filter'), "deleted_at IS NULL");
    await user.click(screen.getByText('Submit'));

    expect(onSubmit).toHaveBeenCalledWith({
      table_key: 'store.orders',
      field: '',
      correction_type: 'update_filter',
      value: { filter: 'deleted_at IS NULL' },
    });
  });

  // Without a model there is nothing to correct against, so the form must hand the
  // negative signal straight back rather than stranding the user in an empty form.
  it('auto-skips when the connection has no semantic model', async () => {
    mockUseSemanticModel.mockReturnValue({ data: undefined, isLoading: false });
    const onSkip = vi.fn();
    render(
      <FeedbackCorrectionForm connectionId="conn-1" onSubmit={vi.fn()} onSkip={onSkip} />,
    );

    await waitFor(() => expect(onSkip).toHaveBeenCalled());
  });

  it('disables submit until the required fields are filled', async () => {
    const user = userEvent.setup();
    mockUseSemanticModel.mockReturnValue({ data: MODEL, isLoading: false });
    render(
      <FeedbackCorrectionForm connectionId="conn-1" onSubmit={vi.fn()} onSkip={vi.fn()} />,
    );

    expect(screen.getByText('Submit')).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('Table'), 'store.orders');
    expect(screen.getByText('Submit')).toBeDisabled();
  });
});
