// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import type { ValueMapping } from '../types';

interface Props {
  mappings: ValueMapping[];
  onChange: (mappings: ValueMapping[]) => void;
}

export default function ValueMappingEditor({ mappings, onChange }: Props) {
  const [draft, setDraft] = useState<Omit<ValueMapping, 'description'>>({
    raw_value: '',
    display_value: '',
  });

  const add = () => {
    if (!draft.raw_value.trim() || !draft.display_value.trim()) return;
    onChange([...mappings, { ...draft, description: null }]);
    setDraft({ raw_value: '', display_value: '' });
  };

  const remove = (i: number) => onChange(mappings.filter((_, idx) => idx !== i));

  const update = (i: number, field: keyof ValueMapping, value: string) => {
    onChange(mappings.map((m, idx) => (idx === i ? { ...m, [field]: value } : m)));
  };

  return (
    <div className="space-y-2">
      {mappings.map((m, i) => (
        <div key={i} className="flex gap-2 items-center">
          <input
            value={m.raw_value}
            onChange={(e) => update(i, 'raw_value', e.target.value)}
            placeholder="Raw value"
            className="flex-1 px-2 py-1 text-xs bg-background text-foreground border border-border rounded font-mono focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <span className="text-muted-foreground text-xs">→</span>
          <input
            value={m.display_value}
            onChange={(e) => update(i, 'display_value', e.target.value)}
            placeholder="Display value"
            className="flex-1 px-2 py-1 text-xs bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <button
            onClick={() => remove(i)}
            className="text-destructive hover:text-destructive/80 text-sm"
          >
            ×
          </button>
        </div>
      ))}
      <div className="flex gap-2 items-center">
        <input
          value={draft.raw_value}
          onChange={(e) => setDraft((d) => ({ ...d, raw_value: e.target.value }))}
          placeholder="Raw value"
          className="flex-1 px-2 py-1 text-xs bg-background text-foreground border border-dashed border-border rounded font-mono focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <span className="text-muted-foreground text-xs">→</span>
        <input
          value={draft.display_value}
          onChange={(e) => setDraft((d) => ({ ...d, display_value: e.target.value }))}
          onKeyDown={(e) => e.key === 'Enter' && add()}
          placeholder="Display value"
          className="flex-1 px-2 py-1 text-xs bg-background text-foreground border border-dashed border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <button
          onClick={add}
          className="text-primary hover:text-primary/80 text-sm font-medium"
        >
          + Add
        </button>
      </div>
    </div>
  );
}
