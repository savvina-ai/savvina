// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import type { ConfigSchema, ConfigField } from '../types';

interface Props {
  schema: ConfigSchema;
  values: Record<string, unknown>;
  onChange: (values: Record<string, unknown>) => void;
  excludeFields?: string[];
}

function isFieldVisible(
  field: ConfigField,
  values: Record<string, unknown>,
  allFields: ConfigField[],
): boolean {
  if (!field.required_if) return true;
  return Object.entries(field.required_if).every(([key, val]) => {
    const depField = allFields.find((f) => f.name === key);
    const current = values[key] ?? depField?.default ?? '';
    return current === val;
  });
}

function isFieldDisabled(
  field: ConfigField,
  values: Record<string, unknown>,
  allFields: ConfigField[],
): boolean {
  if (!field.disabled_if) return false;
  return Object.entries(field.disabled_if).every(([key, val]) => {
    const depField = allFields.find((f) => f.name === key);
    const current = values[key] ?? depField?.default ?? '';
    return current === val;
  });
}

export default function DynamicConnectionForm({ schema, values, onChange, excludeFields }: Props) {
  const [showPassword, setShowPassword] = useState<Record<string, boolean>>({});

  const handleChange = (name: string, value: string | number | boolean) => {
    onChange({ ...values, [name]: value });
  };

  const togglePassword = (name: string) => {
    setShowPassword((prev) => ({ ...prev, [name]: !prev[name] }));
  };

  return (
    <div className="space-y-4">
      {schema.fields.map((field) => {
        if (excludeFields?.includes(field.name)) return null;
        if (!isFieldVisible(field, values, schema.fields)) return null;
        const value = (values[field.name] ?? field.default ?? '') as string | number;
        const disabled = isFieldDisabled(field, values, schema.fields);
        const disabledClass = disabled ? ' opacity-50 cursor-not-allowed' : '';
        return (
          <div key={field.name}>
            <label
              htmlFor={field.type === 'boolean' ? `conn-field-${field.name}` : undefined}
              className={`block text-sm font-medium text-foreground mb-1${disabled ? ' opacity-50' : ''}`}
            >
              {field.label}
              {field.required && <span className="text-destructive ml-1">*</span>}
            </label>
            {field.type === 'select' ? (
              <select
                value={value}
                disabled={disabled}
                onChange={(e) => handleChange(field.name, e.target.value)}
                className={`w-full px-3 py-2 bg-background text-foreground border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring${disabledClass}`}
              >
                <option value="">Select…</option>
                {field.options?.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : field.type === 'integer' ? (
              <input
                type="number"
                value={value}
                placeholder={field.placeholder}
                disabled={disabled}
                autoComplete="new-password"
                onChange={(e) => handleChange(field.name, parseInt(e.target.value, 10))}
                className={`w-full px-3 py-2 bg-background text-foreground border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring${disabledClass}`}
              />
            ) : field.type === 'password' ? (
              <div className="relative">
                <input
                  type={showPassword[field.name] ? 'text' : 'password'}
                  value={value}
                  placeholder={field.placeholder}
                  disabled={disabled}
                  autoComplete="new-password"
                  onChange={(e) => handleChange(field.name, e.target.value)}
                  className={`w-full px-3 py-2 pr-10 bg-background text-foreground border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring${disabledClass}`}
                />
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => togglePassword(field.name)}
                  className={`absolute inset-y-0 right-0 flex items-center px-3 text-muted-foreground hover:text-foreground${disabledClass}`}
                >
                  {showPassword[field.name] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            ) : field.type === 'textarea' ? (
              <textarea
                value={value}
                placeholder={field.placeholder}
                rows={6}
                disabled={disabled}
                autoComplete="off"
                onChange={(e) => handleChange(field.name, e.target.value)}
                className={`w-full px-3 py-2 bg-background text-foreground border border-border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring resize-y${disabledClass}`}
              />
            ) : field.type === 'boolean' ? (
              <input
                id={`conn-field-${field.name}`}
                type="checkbox"
                checked={Boolean(value)}
                disabled={disabled}
                onChange={(e) => handleChange(field.name, e.target.checked)}
                className={`h-4 w-4 rounded border-border text-primary focus:ring-ring${disabledClass}`}
              />
            ) : (
              <input
                type="text"
                value={value}
                placeholder={field.placeholder}
                disabled={disabled}
                autoComplete="new-password"
                onChange={(e) => handleChange(field.name, e.target.value)}
                className={`w-full px-3 py-2 bg-background text-foreground border border-border rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-ring${disabledClass}`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
