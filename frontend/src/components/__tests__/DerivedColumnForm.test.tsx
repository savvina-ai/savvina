// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { useState as useReactState } from 'react';
import userEvent from '@testing-library/user-event';
import { DerivedColumnForm } from '../../pages/SemanticModelPage';
import type { ComponentProps } from 'react';

type FormState = ComponentProps<typeof DerivedColumnForm>['form'];

// Stateful wrapper so controlled inputs update between keystrokes
function StatefulDerivedForm({
  initialForm,
  onChange,
  onConfirm = () => {},
  onCancel = () => {},
}: {
  initialForm: FormState;
  onChange: (f: FormState) => void;
  onConfirm?: () => void;
  onCancel?: () => void;
}) {
  const [form, setForm] = useReactState(initialForm);
  return (
    <DerivedColumnForm
      form={form}
      onChange={(f) => { setForm(f); onChange(f); }}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

const emptyForm = {
  name: '',
  sql_expression: '',
  description: '',
  format_hint: '',
  base_tables_str: '',
};

const filledForm = {
  name: 'revenue_ytd',
  sql_expression: 'SUM(amount)',
  description: 'Year-to-date revenue',
  format_hint: 'currency_usd',
  base_tables_str: 'public.orders, public.order_items',
};

describe('DerivedColumnForm', () => {
  describe('Rendering', () => {
    it('renders Name, SQL Expression, Description, Base Tables inputs', () => {
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByPlaceholderText('revenue_ytd')).toBeInTheDocument();
      expect(screen.getByPlaceholderText('SUM(amount) FILTER (WHERE ...)')).toBeInTheDocument();
      expect(screen.getByPlaceholderText('What this column calculates')).toBeInTheDocument();
      expect(screen.getByPlaceholderText('schema.table1, schema.table2')).toBeInTheDocument();
    });

    it('renders Format Hint select with all options', () => {
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      const select = screen.getByRole('combobox');
      expect(select).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'None' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'percentage' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'currency_usd' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'currency_eur' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'integer' })).toBeInTheDocument();
    });

    it('renders Cancel and Confirm buttons', () => {
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument();
    });
  });

  describe('Confirm button disabled state', () => {
    it('Confirm is disabled when both name and sql_expression are empty', () => {
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByRole('button', { name: /confirm/i })).toBeDisabled();
    });

    it('Confirm is disabled when name is empty but sql_expression is filled', () => {
      render(
        <DerivedColumnForm
          form={{ ...emptyForm, sql_expression: 'SUM(x)' }}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByRole('button', { name: /confirm/i })).toBeDisabled();
    });

    it('Confirm is disabled when sql_expression is empty but name is filled', () => {
      render(
        <DerivedColumnForm
          form={{ ...emptyForm, name: 'my_col' }}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByRole('button', { name: /confirm/i })).toBeDisabled();
    });

    it('Confirm is enabled when both name and sql_expression are filled', () => {
      render(
        <DerivedColumnForm
          form={filledForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByRole('button', { name: /confirm/i })).not.toBeDisabled();
    });
  });

  describe('Callbacks', () => {
    it('calls onConfirm when Confirm is clicked (and enabled)', async () => {
      const user = userEvent.setup();
      const onConfirm = vi.fn();
      render(
        <DerivedColumnForm
          form={filledForm}
          onChange={() => {}}
          onConfirm={onConfirm}
          onCancel={() => {}}
        />,
      );
      await user.click(screen.getByRole('button', { name: /confirm/i }));
      expect(onConfirm).toHaveBeenCalledOnce();
    });

    it('does not call onConfirm when Confirm is disabled', async () => {
      const user = userEvent.setup();
      const onConfirm = vi.fn();
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={() => {}}
          onConfirm={onConfirm}
          onCancel={() => {}}
        />,
      );
      await user.click(screen.getByRole('button', { name: /confirm/i }));
      expect(onConfirm).not.toHaveBeenCalled();
    });

    it('calls onCancel when Cancel is clicked', async () => {
      const user = userEvent.setup();
      const onCancel = vi.fn();
      render(
        <DerivedColumnForm
          form={filledForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={onCancel}
        />,
      );
      await user.click(screen.getByRole('button', { name: /cancel/i }));
      expect(onCancel).toHaveBeenCalledOnce();
    });

    it('calls onChange with updated name when Name input changes', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(
        <StatefulDerivedForm initialForm={emptyForm} onChange={onChange} />,
      );
      await user.type(screen.getByPlaceholderText('revenue_ytd'), 'profit');
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(lastCall.name).toBe('profit');
    });

    it('calls onChange with updated sql_expression when SQL input changes', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(
        <StatefulDerivedForm initialForm={emptyForm} onChange={onChange} />,
      );
      await user.type(
        screen.getByPlaceholderText('SUM(amount) FILTER (WHERE ...)'),
        'COUNT(*)',
      );
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(lastCall.sql_expression).toBe('COUNT(*)');
    });

    it('calls onChange with updated format_hint when select changes', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(
        <DerivedColumnForm
          form={emptyForm}
          onChange={onChange}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      await user.selectOptions(screen.getByRole('combobox'), 'percentage');
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(lastCall.format_hint).toBe('percentage');
    });
  });

  describe('Populated form values', () => {
    it('pre-fills all input values from the form prop', () => {
      render(
        <DerivedColumnForm
          form={filledForm}
          onChange={() => {}}
          onConfirm={() => {}}
          onCancel={() => {}}
        />,
      );
      expect(screen.getByDisplayValue('revenue_ytd')).toBeInTheDocument();
      expect(screen.getByDisplayValue('SUM(amount)')).toBeInTheDocument();
      expect(screen.getByDisplayValue('Year-to-date revenue')).toBeInTheDocument();
      expect(screen.getByDisplayValue('public.orders, public.order_items')).toBeInTheDocument();
      expect(screen.getByDisplayValue('currency_usd')).toBeInTheDocument();
    });
  });
});
