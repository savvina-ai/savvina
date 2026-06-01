// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { useState as useReactState } from 'react';
import userEvent from '@testing-library/user-event';
import DynamicConnectionForm from '../DynamicConnectionForm';
import type { ConfigSchema } from '../../types';

// Stateful wrapper so the controlled input actually updates between keystrokes
function StatefulForm({
  schema,
  onChange,
  initialValues = {},
}: {
  schema: ConfigSchema;
  onChange: (vals: Record<string, unknown>) => void;
  initialValues?: Record<string, unknown>;
}) {
  const [vals, setVals] = useReactState(initialValues);
  return (
    <DynamicConnectionForm
      schema={schema}
      values={vals}
      onChange={(v) => { setVals(v); onChange(v); }}
    />
  );
}

// Helper — build a schema with just one field for unambiguous querying
const singleField = (field: ConfigSchema['fields'][0]): ConfigSchema => ({
  fields: [field],
});

describe('DynamicConnectionForm', () => {
  describe('Field rendering', () => {
    it('type=string renders <input type="text">', () => {
      render(
        <DynamicConnectionForm
          schema={singleField({ name: 'host', type: 'string', label: 'Host' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(screen.getByRole('textbox')).toHaveAttribute('type', 'text');
    });

    it('type=integer renders <input type="number">', () => {
      render(
        <DynamicConnectionForm
          schema={singleField({ name: 'port', type: 'integer', label: 'Port' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(screen.getByRole('spinbutton')).toHaveAttribute('type', 'number');
    });

    it('type=password renders <input type="password"> with toggle button', () => {
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({ name: 'pw', type: 'password', label: 'Password' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      const pwInput = container.querySelector('input[type="password"]');
      expect(pwInput).toBeInTheDocument();
      expect(screen.getByRole('button')).toBeInTheDocument();
    });

    it('type=select renders <select> with all options', () => {
      render(
        <DynamicConnectionForm
          schema={singleField({
            name: 'ssl',
            type: 'select',
            label: 'SSL',
            options: ['disable', 'require', 'verify-full'],
          })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(screen.getByRole('combobox')).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'disable' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'require' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'verify-full' })).toBeInTheDocument();
    });

    it('required field shows asterisk (*) in label', () => {
      render(
        <DynamicConnectionForm
          schema={singleField({ name: 'host', type: 'string', label: 'Host', required: true })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(screen.getByText('*')).toBeInTheDocument();
    });
  });

  describe('Password toggle', () => {
    it('input type is "password" initially', () => {
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({ name: 'pw', type: 'password', label: 'Password' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(container.querySelector('input')).toHaveAttribute('type', 'password');
    });

    it('clicking toggle changes type to "text"', async () => {
      const user = userEvent.setup();
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({ name: 'pw', type: 'password', label: 'Password' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      await user.click(screen.getByRole('button'));
      expect(container.querySelector('input')).toHaveAttribute('type', 'text');
    });

    it('clicking toggle again changes back to "password"', async () => {
      const user = userEvent.setup();
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({ name: 'pw', type: 'password', label: 'Password' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('button'));
      expect(container.querySelector('input')).toHaveAttribute('type', 'password');
    });
  });

  describe('onChange', () => {
    it('called with updated field value merged into existing values', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      // Use StatefulForm so the controlled value updates as the user types
      render(
        <StatefulForm
          schema={singleField({ name: 'host', type: 'string', label: 'Host' })}
          initialValues={{ host: 'localhost', port: 5432 }}
          onChange={onChange}
        />,
      );
      const input = screen.getByRole('textbox');
      await user.clear(input);
      await user.type(input, 'myhost');
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(lastCall.host).toBe('myhost');
      // Existing non-rendered fields should be preserved via spread
      expect(lastCall.port).toBe(5432);
    });

    it('integer field passes parsed integer, not string', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(
        <StatefulForm
          schema={singleField({ name: 'port', type: 'integer', label: 'Port' })}
          initialValues={{}}
          onChange={onChange}
        />,
      );
      await user.type(screen.getByRole('spinbutton'), '5432');
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(typeof lastCall.port).toBe('number');
      expect(lastCall.port).toBe(5432);
    });
  });

  describe('type=textarea', () => {
    it('renders a <textarea> element', () => {
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({
            name: 'tables',
            type: 'textarea',
            label: 'Table Definitions',
            placeholder: '[{"name": "sales", "path": "s3://..."}]',
          })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(container.querySelector('textarea')).toBeInTheDocument();
    });

    it('does not render an <input> for textarea fields', () => {
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({ name: 'tables', type: 'textarea', label: 'Tables' })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(container.querySelector('input')).not.toBeInTheDocument();
    });

    it('onChange fires with string value', async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      render(
        <StatefulForm
          schema={singleField({ name: 'tables', type: 'textarea', label: 'Tables' })}
          onChange={onChange}
        />,
      );
      // <textarea> has role "textbox" in ARIA
      await user.type(screen.getByRole('textbox'), 'hello');
      const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(typeof lastCall.tables).toBe('string');
      expect(lastCall.tables).toContain('hello');
    });

    it('renders placeholder text', () => {
      const { container } = render(
        <DynamicConnectionForm
          schema={singleField({
            name: 'tables',
            type: 'textarea',
            label: 'Tables',
            placeholder: 'enter json here',
          })}
          values={{}}
          onChange={() => {}}
        />,
      );
      expect(container.querySelector('textarea')).toHaveAttribute('placeholder', 'enter json here');
    });
  });

  describe('required_if', () => {
    const conditionalSchema: ConfigSchema = {
      fields: [
        { name: 'mode', type: 'select', label: 'Mode', options: ['standard', 'custom'] },
        { name: 'custom_url', type: 'string', label: 'Custom URL', required_if: { mode: 'custom' } },
      ],
    };

    it('field hidden when required_if condition not met', () => {
      render(
        <DynamicConnectionForm
          schema={conditionalSchema}
          values={{ mode: 'standard' }}
          onChange={() => {}}
        />,
      );
      // Only the select should exist; the custom_url textbox should not
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });

    it('field shown when required_if condition is met', () => {
      render(
        <DynamicConnectionForm
          schema={conditionalSchema}
          values={{ mode: 'custom' }}
          onChange={() => {}}
        />,
      );
      expect(screen.getByRole('textbox')).toBeInTheDocument();
    });

    it('field shown when required_if is undefined', () => {
      render(
        <DynamicConnectionForm
          schema={conditionalSchema}
          values={{}}
          onChange={() => {}}
        />,
      );
      // mode field (select) is always visible since it has no required_if
      expect(screen.getByRole('combobox')).toBeInTheDocument();
    });
  });
});
