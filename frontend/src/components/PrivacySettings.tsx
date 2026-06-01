// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import type { PrivacySettings } from '../types';

const DEFAULTS: PrivacySettings = {
  include_sample_values: false,
  include_column_comments: false,
  include_row_counts: false,
  sensitive_column_patterns: [
    'password', 'passwd', 'secret', 'token', 'api_key',
    'email', 'ssn', 'social_security', 'credit_card', 'card_number', 'cvv',
    'phone', 'mobile', 'address', 'salary', 'wage', 'income',
    'bank_account', 'routing_number', 'dob', 'date_of_birth',
    'national_id', 'passport', 'license_number', 'tax_id',
  ],
  excluded_schemas: [],
  excluded_tables: [],
  excluded_columns: [],
};

interface Props {
  settings: PrivacySettings;
  onChange: (settings: PrivacySettings) => void;
}

function TagList({
  label,
  items,
  onChange,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed && !items.includes(trimmed)) {
      onChange([...items, trimmed]);
      setDraft('');
    }
  };

  return (
    <div>
      <p className="text-sm font-medium text-foreground mb-1">{label}</p>
      <div className="flex gap-2 mb-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), add())}
          placeholder="Add and press Enter"
          className="flex-1 bg-background text-foreground px-3 py-1.5 border border-border rounded text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        />
        <button
          type="button"
          onClick={add}
          className="px-3 py-1.5 bg-muted hover:bg-muted/80 rounded text-sm"
        >
          Add
        </button>
      </div>
      <div className="flex flex-wrap gap-2">
        {items.map((item) => (
          <span
            key={item}
            className="flex items-center gap-1 px-2 py-0.5 bg-accent border border-border rounded text-xs text-accent-foreground"
          >
            {item}
            <button
              type="button"
              onClick={() => onChange(items.filter((i) => i !== item))}
              className="hover:text-destructive ml-1"
            >
              ×
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}

function Toggle({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <div className="relative mt-0.5 flex-shrink-0">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="sr-only"
        />
        <div
          className={`w-10 h-6 rounded-full transition-colors ring-1 ring-inset ${checked ? 'bg-primary ring-primary' : 'bg-input ring-border'}`}
        />
        <div
          className={`absolute top-1 left-1 w-4 h-4 bg-white rounded-full shadow transition-transform ${checked ? 'translate-x-4' : ''}`}
        />
      </div>
      <div>
        <p className="text-sm font-medium text-foreground">{label}</p>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    </label>
  );
}

export default function PrivacySettingsForm({ settings, onChange }: Props) {
  const set = <K extends keyof PrivacySettings>(key: K, val: PrivacySettings[K]) =>
    onChange({ ...settings, [key]: val });

  return (
    <div className="space-y-5">
      <Toggle
        label="Include sample values"
        description="Sends up to 5 sample values per column to improve accuracy. Disable for sensitive data."
        checked={settings.include_sample_values}
        onChange={(v) => set('include_sample_values', v)}
      />
      <Toggle
        label="Include column comments"
        description="Sends database column comments to help the LLM understand field meanings."
        checked={settings.include_column_comments}
        onChange={(v) => set('include_column_comments', v)}
      />
      <Toggle
        label="Include row counts"
        description="Sends approximate row counts for each table."
        checked={settings.include_row_counts}
        onChange={(v) => set('include_row_counts', v)}
      />
      <TagList
        label="Sensitive column patterns (excluded from schema)"
        items={settings.sensitive_column_patterns}
        onChange={(v) => set('sensitive_column_patterns', v)}
      />
      <TagList
        label="Excluded schemas"
        items={settings.excluded_schemas}
        onChange={(v) => set('excluded_schemas', v)}
      />
      <TagList
        label="Excluded tables"
        items={settings.excluded_tables}
        onChange={(v) => set('excluded_tables', v)}
      />
      <TagList
        label="Excluded columns"
        items={settings.excluded_columns}
        onChange={(v) => set('excluded_columns', v)}
      />
      <button
        type="button"
        onClick={() => onChange({ ...DEFAULTS })}
        className="text-sm text-muted-foreground hover:text-foreground underline"
      >
        Reset to defaults
      </button>
    </div>
  );
}
