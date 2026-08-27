// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useMemo, useState } from 'react';
import { useSemanticModel } from '../hooks/useSemanticModel';
import type { SemanticCorrectionPayload } from '../types';

interface Props {
  connectionId: string;
  /** Send thumbs-down together with a semantic correction. */
  onSubmit: (correction: SemanticCorrectionPayload) => void;
  /** Send a plain thumbs-down with no correction. */
  onSkip: () => void;
}

type CorrectionType = SemanticCorrectionPayload['correction_type'];

const CORRECTION_TYPES: { value: CorrectionType; label: string }[] = [
  { value: 'add_value_mapping', label: 'Add value mapping' },
  { value: 'update_filter', label: 'Add default filter' },
  { value: 'update_description', label: 'Update description' },
];

const selectClass =
  'rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring';
const inputClass =
  'w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring';

/**
 * Inline form shown after a thumbs-down on a message with a generated query.
 * Collects a semantic-model correction and submits it alongside the negative
 * feedback. Auto-skips (plain thumbs-down) when the connection has no semantic
 * model, so the parent never has to special-case that.
 */
export default function FeedbackCorrectionForm({ connectionId, onSubmit, onSkip }: Props) {
  const { data: semanticModel, isLoading } = useSemanticModel(connectionId);

  const tableKeys = useMemo(
    () => (semanticModel ? Object.keys(semanticModel.tables ?? {}) : []),
    [semanticModel],
  );

  const [tableKey, setTableKey] = useState('');
  const [column, setColumn] = useState('');
  const [correctionType, setCorrectionType] = useState<CorrectionType>('add_value_mapping');
  // Per-type value fields
  const [rawValue, setRawValue] = useState('');
  const [displayValue, setDisplayValue] = useState('');
  const [filterText, setFilterText] = useState('');
  const [description, setDescription] = useState('');

  const noModel = !isLoading && (!semanticModel || tableKeys.length === 0);

  // When the model is absent/empty there is nothing to correct — fall back to a
  // plain thumbs-down so the user's negative signal is still recorded.
  useEffect(() => {
    if (noModel) onSkip();
  }, [noModel, onSkip]);

  const columnKeys = useMemo(() => {
    if (!semanticModel || !tableKey) return [];
    const table = semanticModel.tables[tableKey];
    return table ? Object.keys(table.columns ?? {}) : [];
  }, [semanticModel, tableKey]);

  if (isLoading) {
    return (
      <div className="mt-2 rounded-xl border border-border bg-surface-elevated p-3 text-sm text-muted-foreground">
        Loading semantic model…
      </div>
    );
  }

  if (noModel) return null;

  const columnRequired = correctionType === 'add_value_mapping';
  const columnUsed = correctionType !== 'update_filter';

  const isValid = (() => {
    if (!tableKey) return false;
    if (columnRequired && !column) return false;
    if (correctionType === 'add_value_mapping') return !!rawValue && !!displayValue;
    if (correctionType === 'update_filter') return !!filterText;
    if (correctionType === 'update_description') return !!description;
    return false;
  })();

  const handleSubmit = () => {
    if (!isValid) return;
    let field = '';
    let value: Record<string, string> = {};
    if (correctionType === 'add_value_mapping') {
      field = column;
      value = { raw_value: rawValue, display_value: displayValue };
    } else if (correctionType === 'update_filter') {
      field = '';
      value = { filter: filterText };
    } else {
      field = column;
      value = { target: column ? 'column' : 'table', description };
    }
    onSubmit({ table_key: tableKey, field, correction_type: correctionType, value });
  };

  return (
    <div className="mt-2 space-y-2 rounded-xl border border-border bg-surface-elevated p-3">
      <p className="text-xs text-muted-foreground">
        Help improve future answers — tell us what was wrong.
      </p>
      <div className="flex flex-wrap gap-2">
        <select
          aria-label="Table"
          value={tableKey}
          onChange={(e) => {
            setTableKey(e.target.value);
            setColumn('');
          }}
          className={selectClass}
        >
          <option value="">Select table…</option>
          {tableKeys.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>

        <select
          aria-label="Correction type"
          value={correctionType}
          onChange={(e) => setCorrectionType(e.target.value as CorrectionType)}
          className={selectClass}
        >
          {CORRECTION_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>

        {columnUsed && (
          <select
            aria-label="Column"
            value={column}
            onChange={(e) => setColumn(e.target.value)}
            disabled={!tableKey}
            className={selectClass}
          >
            <option value="">{columnRequired ? 'Select column…' : 'Column (optional)…'}</option>
            {columnKeys.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        )}
      </div>

      {correctionType === 'add_value_mapping' && (
        <div className="flex flex-wrap gap-2">
          <input
            aria-label="Raw value"
            placeholder="Raw value (as stored)"
            value={rawValue}
            onChange={(e) => setRawValue(e.target.value)}
            className={inputClass}
          />
          <input
            aria-label="Display value"
            placeholder="Display value (what users say)"
            value={displayValue}
            onChange={(e) => setDisplayValue(e.target.value)}
            className={inputClass}
          />
        </div>
      )}

      {correctionType === 'update_filter' && (
        <input
          aria-label="Filter"
          placeholder="SQL condition, e.g. status = 'active'"
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          className={inputClass}
        />
      )}

      {correctionType === 'update_description' && (
        <textarea
          aria-label="Description"
          placeholder="Corrected description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className={inputClass}
        />
      )}

      <div className="flex items-center justify-end gap-2">
        <button
          onClick={onSkip}
          className="rounded-md px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          Skip
        </button>
        <button
          onClick={handleSubmit}
          disabled={!isValid}
          className="rounded-md bg-brand-gradient px-2.5 py-1 text-xs text-white shadow-gradient-btn transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          Submit
        </button>
      </div>
    </div>
  );
}
