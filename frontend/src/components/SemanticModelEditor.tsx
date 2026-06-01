// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import ValueMappingEditor from './ValueMappingEditor';
import type { SemanticModel, TableSemantic, ColumnSemantic } from '../types';

interface Props {
  model: SemanticModel;
  onChange: (model: SemanticModel) => void;
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
                className="w-full mt-1 px-2 py-1 text-xs bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
            <div className="flex-1">
              <label className="text-xs text-muted-foreground">Description</label>
              <input
                value={col.description ?? ''}
                onChange={(e) =>
                  onUpdate({ ...col, description: e.target.value || null })
                }
                className="w-full mt-1 px-2 py-1 text-xs bg-background text-foreground border border-border rounded focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>
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

function TableEditor({
  tableName,
  table,
  onUpdate,
}: {
  tableName: string;
  table: TableSemantic;
  onUpdate: (t: TableSemantic) => void;
}) {
  const [open, setOpen] = useState(false);

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

export default function SemanticModelEditor({ model, onChange }: Props) {
  const updateTable = (name: string, table: TableSemantic) =>
    onChange({ ...model, tables: { ...model.tables, [name]: table } });

  return (
    <div className="space-y-3">
      {Object.entries(model.tables).map(([tableName, table]) => (
        <TableEditor
          key={tableName}
          tableName={tableName}
          table={table}
          onUpdate={(updated) => updateTable(tableName, updated)}
        />
      ))}
    </div>
  );
}
