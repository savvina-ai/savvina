// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useMemo, useState } from 'react';
import { cn } from '@/lib/utils';
import ValueMappingEditor from './ValueMappingEditor';
import {
  AGGREGATIONS,
  CARDINALITY_CLASSES,
  SEMANTIC_TYPES,
  TIME_GRANULARITIES,
  buildSchemaTableMap,
} from '../lib/semanticUtils';
import type {
  AggregationType,
  ColumnSemantic,
  SchemaTable,
  SemanticModel,
  TableSemantic,
  TimeGranularity,
} from '../types';

const fieldClass =
  'w-full mt-1 px-2 py-1 text-xs bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring';

interface Props {
  model: SemanticModel;
  onChange: (model: SemanticModel) => void;
  /** Authoritative column list per table, used for the primary-column dropdowns. */
  schemaTables?: SchemaTable[];
}

function ColumnEditor({
  colName,
  col,
  onUpdate,
}: {
  colName: string;
  col: ColumnSemantic;
  onUpdate: (col: ColumnSemantic) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="border-l-2 border-border pl-3">
      <div
        className="flex items-center gap-2 py-1 cursor-pointer hover:bg-muted rounded"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-xs text-muted-foreground">{open ? '▼' : '▶'}</span>
        <span className="text-xs font-mono text-foreground">{colName}</span>
        <span className="text-xs text-muted-foreground">→</span>
        <span className="text-xs text-foreground">{col.display_name || <em className="text-muted-foreground">unnamed</em>}</span>
        {col.is_sensitive && (
          <span className="text-xs text-destructive ml-auto">🔒 sensitive</span>
        )}
      </div>
      {open && (
        <div className="space-y-3 py-2 pr-2">
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Display name</label>
              <input
                value={col.display_name}
                onChange={(e) => onUpdate({ ...col, display_name: e.target.value })}
                className={fieldClass}
              />
            </div>
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Description</label>
              <input
                value={col.description ?? ''}
                onChange={(e) =>
                  onUpdate({ ...col, description: e.target.value || null })
                }
                className={fieldClass}
              />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="text-xs text-muted-foreground">Semantic type</label>
              <select
                aria-label={`Semantic type for ${colName}`}
                value={col.semantic_type ?? 'unknown'}
                onChange={(e) => onUpdate({ ...col, semantic_type: e.target.value })}
                className={fieldClass}
              >
                {SEMANTIC_TYPES.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-muted-foreground">Default aggregation</label>
              <select
                aria-label={`Default aggregation for ${colName}`}
                value={col.default_aggregation ?? ''}
                onChange={(e) =>
                  onUpdate({
                    ...col,
                    default_aggregation: (e.target.value || null) as AggregationType | null,
                  })
                }
                className={fieldClass}
              >
                <option value="">None</option>
                {AGGREGATIONS.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-muted-foreground">Time granularity</label>
              <select
                aria-label={`Time granularity for ${colName}`}
                value={col.time_granularity ?? ''}
                onChange={(e) =>
                  onUpdate({
                    ...col,
                    time_granularity: (e.target.value || null) as TimeGranularity | null,
                  })
                }
                className={fieldClass}
              >
                <option value="">None</option>
                {TIME_GRANULARITIES.map((g) => (
                  <option key={g} value={g}>{g}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-32">
              <label className="text-xs text-muted-foreground">Cardinality</label>
              <select
                aria-label={`Cardinality for ${colName}`}
                value={col.cardinality ?? ''}
                onChange={(e) => onUpdate({ ...col, cardinality: e.target.value || undefined })}
                className={fieldClass}
              >
                <option value="">None</option>
                {CARDINALITY_CLASSES.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <div className="w-24">
              <label className="text-xs text-muted-foreground">Currency</label>
              <input
                aria-label={`Currency for ${colName}`}
                value={col.currency ?? ''}
                onChange={(e) => onUpdate({ ...col, currency: e.target.value || null })}
                placeholder="EUR"
                className={fieldClass}
              />
            </div>
            <div className="w-24">
              <label className="text-xs text-muted-foreground">Unit</label>
              <input
                aria-label={`Unit for ${colName}`}
                value={col.unit ?? ''}
                onChange={(e) => onUpdate({ ...col, unit: e.target.value || null })}
                placeholder="kg"
                className={fieldClass}
              />
            </div>
            <label className="flex items-center gap-1.5 pb-1 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={col.is_sensitive}
                onChange={(e) => onUpdate({ ...col, is_sensitive: e.target.checked })}
                className="rounded accent-primary"
              />
              Sensitive
            </label>
            <label
              className="flex items-center gap-1.5 pb-1 text-xs text-muted-foreground"
              title="Never SUM this column across dimensions"
            >
              <input
                type="checkbox"
                checked={col.is_non_additive ?? false}
                onChange={(e) => onUpdate({ ...col, is_non_additive: e.target.checked })}
                className="rounded accent-primary"
              />
              Non-additive
            </label>
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Value mappings</label>
            <ValueMappingEditor
              mappings={col.value_mappings}
              onChange={(mappings) => onUpdate({ ...col, value_mappings: mappings })}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Fields the generator writes but `TableSemanticUpdate` does not accept — shown so the
 * user can see what the LLM is being told, but not editable, since a PUT would silently
 * drop any change.
 */
function GeneratedTableFields({ table }: { table: TableSemantic }) {
  const rows: { label: string; value: string }[] = [];
  if (table.base_sql) rows.push({ label: 'Base SQL', value: table.base_sql });
  if (table.partition_columns?.length) {
    rows.push({ label: 'Partition columns', value: table.partition_columns.join(', ') });
  }
  if (table.cluster_columns?.length) {
    rows.push({ label: 'Cluster columns', value: table.cluster_columns.join(', ') });
  }
  table.hierarchies?.forEach((h) => {
    rows.push({ label: `Hierarchy: ${h.name}`, value: h.levels.join(' → ') });
  });

  if (rows.length === 0) return null;

  return (
    <div>
      <h4 className="text-xs font-semibold text-muted-foreground uppercase mb-2">
        Generated (read-only)
      </h4>
      <dl className="space-y-1">
        {rows.map((r) => (
          <div key={r.label} className="flex gap-2 text-xs">
            <dt className="w-36 shrink-0 text-muted-foreground">{r.label}</dt>
            <dd className="min-w-0 flex-1 break-words font-mono text-foreground">{r.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function TableEditor({
  tableName,
  table,
  onUpdate,
  schemaTable,
}: {
  tableName: string;
  table: TableSemantic;
  onUpdate: (t: TableSemantic) => void;
  schemaTable?: SchemaTable;
}) {
  const [open, setOpen] = useState(false);

  // The schema is the authoritative column list — table.columns only holds the
  // columns the generator annotated, which is a subset on a tables_partial model.
  // The currently-stored values are folded in so a configured column always has
  // a matching <option>; without one the select renders as "None" and reads as unset.
  const columnOptions = useMemo(
    () =>
      Array.from(
        new Set([
          ...(schemaTable?.columns.map((c) => c.name) ?? []),
          ...Object.keys(table.columns),
          ...(table.primary_timestamp_column ? [table.primary_timestamp_column] : []),
          ...(table.primary_date_column ? [table.primary_date_column] : []),
        ]),
      ),
    [schemaTable, table.columns, table.primary_timestamp_column, table.primary_date_column],
  );

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <div
        className="flex items-center gap-3 px-4 py-3 bg-muted cursor-pointer"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-muted-foreground text-xs">{open ? '▼' : '▶'}</span>
        <span className="text-sm font-mono text-foreground">{tableName}</span>
        <span className="text-sm text-foreground font-medium">{table.display_name}</span>
        <span className="text-xs text-muted-foreground ml-auto">
          {Object.keys(table.columns).length} columns
        </span>
      </div>
      {open && (
        <div className="p-4 space-y-4 border-t border-border">
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Display name</label>
              <input
                value={table.display_name}
                onChange={(e) => onUpdate({ ...table, display_name: e.target.value })}
                className="w-full mt-1 px-3 py-1.5 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Description</label>
              <input
                value={table.description ?? ''}
                onChange={(e) =>
                  onUpdate({ ...table, description: e.target.value || null })
                }
                className="w-full mt-1 px-3 py-1.5 text-sm bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Grain</label>
              <input
                aria-label={`Grain for ${tableName}`}
                value={table.grain ?? ''}
                onChange={(e) => onUpdate({ ...table, grain: e.target.value || null })}
                placeholder="one row per order"
                className={fieldClass}
              />
            </div>
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Domain</label>
              <input
                aria-label={`Domain for ${tableName}`}
                value={table.domain ?? ''}
                onChange={(e) => onUpdate({ ...table, domain: e.target.value || null })}
                placeholder="finance"
                className={fieldClass}
              />
            </div>
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Primary timestamp column</label>
              <select
                aria-label={`Primary timestamp column for ${tableName}`}
                value={table.primary_timestamp_column ?? ''}
                onChange={(e) =>
                  onUpdate({ ...table, primary_timestamp_column: e.target.value || null })
                }
                className={fieldClass}
              >
                <option value="">None</option>
                {columnOptions.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Primary date column</label>
              <select
                aria-label={`Primary date column for ${tableName}`}
                value={table.primary_date_column ?? ''}
                onChange={(e) =>
                  onUpdate({ ...table, primary_date_column: e.target.value || null })
                }
                className={fieldClass}
              >
                <option value="">None</option>
                {columnOptions.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="text-xs text-muted-foreground">
              Default filters (one SQL condition per line)
            </label>
            <textarea
              aria-label={`Default filters for ${tableName}`}
              value={(table.default_filters ?? []).join('\n')}
              onChange={(e) =>
                onUpdate({
                  ...table,
                  default_filters: e.target.value
                    .split('\n')
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
              rows={2}
              placeholder="deleted_at IS NULL"
              className={cn(fieldClass, 'font-mono')}
            />
          </div>
          <GeneratedTableFields table={table} />
          <div>
            <h4 className="text-xs font-semibold text-muted-foreground uppercase mb-2">Columns</h4>
            <div className="space-y-1">
              {Object.entries(table.columns).map(([colName, col]) => (
                <ColumnEditor
                  key={colName}
                  colName={colName}
                  col={col}
                  onUpdate={(updated) =>
                    onUpdate({
                      ...table,
                      columns: { ...table.columns, [colName]: updated },
                    })
                  }
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SemanticModelEditor({ model, onChange, schemaTables }: Props) {
  const updateTable = (name: string, table: TableSemantic) =>
    onChange({ ...model, tables: { ...model.tables, [name]: table } });

  const schemaTableMap = useMemo(() => buildSchemaTableMap(schemaTables), [schemaTables]);

  return (
    <div className="space-y-3">
      {Object.entries(model.tables).map(([tableName, table]) => {
        const schemaTable =
          schemaTableMap.get(tableName) ??
          schemaTableMap.get(tableName.split('.').pop() ?? tableName);
        return (
          <TableEditor
            key={tableName}
            tableName={tableName}
            table={table}
            schemaTable={schemaTable}
            onUpdate={(updated) => updateTable(tableName, updated)}
          />
        );
      })}
    </div>
  );
}
