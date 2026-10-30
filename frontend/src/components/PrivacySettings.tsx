// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { displayRowFilterKey, displayRowFilterMap } from '../lib/rowFilterKeys';
import type { PrivacySettings, PrivilegeAudit } from '../types';

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
  row_filters: {},
};

interface Props {
  settings: PrivacySettings;
  onChange: (settings: PrivacySettings) => void;
  /** `schema.table` names offered by the table input; free text when absent. */
  knownTables?: string[];
  /** The connection's SQL dialect (`DataSourceInfo.query_dialect`). A stored key part the
   *  engine would fold or refuse unquoted is shown with the dialect's quotes, so the next
   *  save sends the same key back. Keys are shown as stored when absent. */
  dialect?: string;
  /** Called with `false` while a row filter is incomplete or duplicated, so the parent can
   *  block saving; `true` otherwise. */
  onValidityChange?: (valid: boolean) => void;
  /** The connection's last privilege audit; a clean one is acknowledged at the end of the
   *  form (findings are shown by the banner above the form). */
  audit?: PrivilegeAudit | null;
}

function TagList({
  label,
  items,
  onChange,
  description,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  description?: string;
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
      {description && <p className="text-xs text-muted-foreground mb-2">{description}</p>}
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

interface RowFilterRow {
  id: number;
  table: string;
  condition: string;
}

function rowsFromMap(map: Record<string, string>, dialect?: string): RowFilterRow[] {
  return Object.entries(map).map(([table, condition], id) => ({
    id,
    table: displayRowFilterKey(table, dialect),
    condition,
  }));
}

function mapFromRows(rows: RowFilterRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const table = row.table.trim();
    if (table) out[table] = row.condition;
  }
  return out;
}

const DUPLICATE_TABLE_ERROR = 'This table already has a filter';
const MISSING_TABLE_ERROR = 'Choose a table for this condition';

/** Per-row error for the table input, by row id. A row that is empty in both fields is
 *  valid (it is dropped); a duplicate is flagged on the later row only. */
function rowErrors(rows: RowFilterRow[]): Map<number, string> {
  const errors = new Map<number, string>();
  const seen = new Set<string>();
  for (const row of rows) {
    const table = row.table.trim();
    if (!table) {
      if (row.condition.trim()) errors.set(row.id, MISSING_TABLE_ERROR);
    } else if (seen.has(table)) {
      errors.set(row.id, DUPLICATE_TABLE_ERROR);
    } else {
      seen.add(table);
    }
  }
  return errors;
}

function sameMap(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => b[k] === a[k]);
}

function RowFilterList({
  filters,
  onChange,
  knownTables,
  dialect,
  onValidityChange,
}: {
  filters: Record<string, string>;
  onChange: (filters: Record<string, string>) => void;
  knownTables?: string[];
  dialect?: string;
  onValidityChange?: (valid: boolean) => void;
}) {
  // Rows are local so a half-typed row (empty table name) survives without being emitted;
  // when the parent hands in a map the rows do not already describe (the initial load),
  // the rows are rebuilt from it.
  const [rows, setRows] = useState<RowFilterRow[]>(() => rowsFromMap(filters, dialect));
  const nextId = useRef(rows.length);
  const listId = useId();

  useEffect(() => {
    setRows((current) => {
      const emitted = mapFromRows(current);
      // The rows already describe the map when the parent holds what they just emitted
      // (the admin is typing: the raw keys) or the stored map whose keys they show in
      // displayed form (after a load or a save). Only another map rebuilds them.
      if (sameMap(emitted, filters) || sameMap(emitted, displayRowFilterMap(filters, dialect))) {
        return current;
      }
      const rebuilt = rowsFromMap(filters, dialect);
      // Ids restart at 0 on a rebuild: keep the counter ahead of them so a row added
      // afterwards never shares an id with a stored one.
      nextId.current = Math.max(nextId.current, rebuilt.length);
      return rebuilt;
    });
  }, [filters, dialect]);

  const errors = useMemo(() => rowErrors(rows), [rows]);
  const valid = errors.size === 0;
  useEffect(() => {
    onValidityChange?.(valid);
  }, [valid, onValidityChange]);

  const update = (next: RowFilterRow[]) => {
    setRows(next);
    onChange(mapFromRows(next));
  };
  const edit = (id: number, patch: Partial<RowFilterRow>) =>
    update(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const add = () => update([...rows, { id: nextId.current++, table: '', condition: '' }]);
  const remove = (id: number) => update(rows.filter((r) => r.id !== id));

  const inputClass =
    'bg-background text-foreground px-3 py-1.5 border border-border rounded text-sm font-mono placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring';

  return (
    <div>
      <p className="text-sm font-medium text-foreground mb-1">Row filters</p>
      <p className="text-xs text-muted-foreground mb-2">
        One condition per table, applied to every query that reads the table. Name the table
        as the database stores it (schema.table); quote a part that has uppercase letters or
        other characters the database would not accept bare. Tables not listed are unfiltered.
      </p>
      {knownTables && (
        <datalist id={listId}>
          {knownTables
            .map((t) => displayRowFilterKey(t, dialect))
            .map((t) => (
              <option key={t} value={t} />
            ))}
        </datalist>
      )}
      <div className="space-y-2">
        {rows.map((row, index) => {
          const error = errors.get(row.id);
          const errorId = `${listId}-error-${row.id}`;
          return (
            <div key={row.id} className="flex gap-2">
              <div className="w-2/5">
                <input
                  aria-label={`Table ${index + 1}`}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? errorId : undefined}
                  type="text"
                  list={knownTables ? listId : undefined}
                  value={row.table}
                  onChange={(e) => edit(row.id, { table: e.target.value })}
                  placeholder="public.orders"
                  spellCheck={false}
                  className={`w-full ${inputClass} aria-[invalid=true]:border-destructive`}
                />
                {error && (
                  <p id={errorId} className="mt-1 text-xs text-destructive">
                    {error}
                  </p>
                )}
              </div>
              <input
                aria-label={`Condition ${index + 1}`}
                type="text"
                value={row.condition}
                onChange={(e) => edit(row.id, { condition: e.target.value })}
                placeholder="tenant_id = 42"
                spellCheck={false}
                className={`flex-1 ${inputClass}`}
              />
              <button
                type="button"
                aria-label={`Remove filter ${index + 1}`}
                onClick={() => remove(row.id)}
                className="px-2 text-muted-foreground hover:text-destructive"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>
      <button
        type="button"
        onClick={add}
        className="mt-2 px-3 py-1.5 bg-muted hover:bg-muted/80 rounded text-sm"
      >
        Add filter
      </button>
    </div>
  );
}

export default function PrivacySettingsForm({
  settings,
  onChange,
  knownTables,
  dialect,
  onValidityChange,
  audit,
}: Props) {
  const set = <K extends keyof PrivacySettings>(key: K, val: PrivacySettings[K]) =>
    onChange({ ...settings, [key]: val });

  return (
    <div className="space-y-5">
      <Toggle
        label="Include sample values"
        description="Off by default. When on, sends up to 3 example values per column (an ENUM's declared members on MySQL) to your LLM provider to improve filter accuracy. Sensitive columns never send sample values."
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
        label="Sensitive column patterns (values masked)"
        description="Columns whose name contains one of these words (case-insensitive) stay in the
          schema tagged [SENSITIVE]. The model is told not to select them and to use other
          identifiers instead; sample values are never sent for them, and if a query does return
          one its values are redacted. To hide a column from the model entirely, use Excluded
          columns below."
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
        description="Removed from the schema and the semantic layer entirely — the model never
          sees the name, so it cannot filter or join on it either."
        items={settings.excluded_columns}
        onChange={(v) => set('excluded_columns', v)}
      />
      <RowFilterList
        filters={settings.row_filters ?? {}}
        onChange={(row_filters) => set('row_filters', row_filters)}
        knownTables={knownTables}
        dialect={dialect}
        onValidityChange={onValidityChange}
      />
      <button
        type="button"
        // Keep the row filters: resetting the other settings must not drop the per-table
        // filters the admin defined. Remove rows one by one to clear them.
        onClick={() => onChange({ ...DEFAULTS, row_filters: settings.row_filters ?? {} })}
        className="text-sm text-muted-foreground hover:text-foreground underline"
      >
        Reset to defaults
      </button>
      {audit?.status === 'ok' && audit.findings.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Privileges checked {new Date(audit.checked_at).toLocaleString()}: the role has only the
          access Savvina needs.
        </p>
      )}
    </div>
  );
}
