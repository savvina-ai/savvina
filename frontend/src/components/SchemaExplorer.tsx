// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, Table2, Key, Copy, X, RefreshCw, Loader2, Search } from 'lucide-react';
import { useAppStore } from '../store/appStore';
import { useConnectionSchema } from '../hooks/useConnections';
import { connectionsApi } from '../api/connections';
import { semanticApi } from '../api/semantic';
import { cn } from '@/lib/utils';
import type { SchemaTable, SchemaColumn } from '../types';

interface SchemaData {
  tables?: SchemaTable[];
}

interface Props {
  onClose?: () => void;
  embedded?: boolean;
}

type TypeInfo = { abbr: string; color: string };

function getTypeInfo(dataType: string): TypeInfo {
  const t = dataType.toLowerCase();
  if (/^(int|integer|bigint|smallint|tinyint|serial|bigserial)/.test(t))
    return { abbr: 'int', color: 'hsl(211 62% 50%)' };
  if (/^(uuid)/.test(t))
    return { abbr: 'uuid', color: 'hsl(211 62% 50%)' };
  if (/^(varchar|text|char|string|nvarchar|nchar|clob)/.test(t))
    return { abbr: 'txt', color: 'hsl(160 72% 40%)' };
  if (/^(numeric|decimal|float|double|real|money|number)/.test(t))
    return { abbr: 'num', color: 'hsl(32 90% 48%)' };
  if (/^(timestamp|datetime|date|time)/.test(t))
    return { abbr: 'date', color: 'hsl(260 60% 58%)' };
  if (/^(bool|boolean)/.test(t))
    return { abbr: 'bool', color: 'hsl(340 70% 55%)' };
  if (/^(json|jsonb)/.test(t))
    return { abbr: 'json', color: 'hsl(32 90% 48%)' };
  return { abbr: t.slice(0, 4), color: 'hsl(215 15% 50%)' };
}

function ColumnRow({ col }: { col: SchemaColumn }) {
  const copy = (e: MouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
    navigator.clipboard.writeText(col.name);
  };
  const { abbr, color } = getTypeInfo(col.data_type);

  return (
    <div
      onClick={copy}
      title={`Click to copy: ${col.name}`}
      className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 transition-colors hover:bg-muted"
    >
      {col.is_primary_key ? (
        <Key className="h-3 w-3 shrink-0 text-schema-icon" />
      ) : (
        <div className="h-3 w-3 shrink-0" />
      )}
      <span className="flex-1 truncate font-mono text-xs text-foreground">
        <span style={{ color }} className="mr-1 font-semibold">{abbr}</span>
        {col.name}
      </span>
    </div>
  );
}

function TableRow({
  table,
  displayName,
  isOpen,
  onToggle,
}: {
  table: SchemaTable;
  displayName?: string;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const isView = table.table_type?.toUpperCase() === 'VIEW';

  const copyName = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    navigator.clipboard.writeText(table.name);
  };

  return (
    <div className="animate-fade-in">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted"
      >
        {isOpen ? (
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <Table2 className="h-3.5 w-3.5 shrink-0 text-schema-icon" />
        <span className="flex-1 truncate font-mono text-xs text-foreground">{table.name}</span>
        {displayName && displayName !== table.name && (
          <span className="text-[10px] text-muted-foreground">{displayName}</span>
        )}
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase',
            isView
              ? 'bg-muted text-muted-foreground'
              : 'bg-badge-bg text-badge-text',
          )}
        >
          {isView ? 'VIEW' : 'TABLE'}
        </span>
        {table.row_count_approx != null && (
          <span className="font-mono text-[10px] text-muted-foreground">
            ~{table.row_count_approx.toLocaleString()}
          </span>
        )}
        <button
          onClick={copyName}
          title="Copy table name"
          className="text-muted-foreground transition-colors hover:text-foreground"
        >
          <Copy className="h-3 w-3" />
        </button>
      </button>

      {isOpen && (
        <div className="ml-5 space-y-0.5 py-1 animate-fade-in">
          {table.columns.map((col) => (
            <ColumnRow key={col.name} col={col} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function SchemaExplorer({ onClose, embedded = false }: Props) {
  const [search, setSearch] = useState('');
  const [openTables, setOpenTables] = useState<Set<string>>(new Set());
  const [collapsedSchemas, setCollapsedSchemas] = useState<Set<string>>(new Set());
  const [width, setWidth] = useState(256);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (embedded) return;
    const onMouseMove = (e: globalThis.MouseEvent) => {
      if (!isResizing.current) return;
      const delta = e.clientX - startX.current;
      setWidth(Math.min(500, Math.max(180, startWidth.current + delta)));
    };
    const onMouseUp = () => {
      isResizing.current = false;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, [embedded]);

  const activeConnectionId = useAppStore((s) => s.activeConnectionId);
  const { data: schema } = useConnectionSchema(activeConnectionId);

  const handleRefreshSchema = async () => {
    if (!activeConnectionId || isRefreshing) return;
    setIsRefreshing(true);
    setRefreshError(null);
    try {
      await connectionsApi.refreshSchema(activeConnectionId);
      await queryClient.invalidateQueries({ queryKey: ['schema', activeConnectionId] });
      await queryClient.invalidateQueries({ queryKey: ['semantic', activeConnectionId] });
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      const msg = detail ?? 'Schema refresh failed';
      setRefreshError(msg);
    } finally {
      setIsRefreshing(false);
    }
  };

  const { data: semanticModel } = useQuery({
    queryKey: ['semantic', activeConnectionId],
    queryFn: () => semanticApi.get(activeConnectionId!),
    enabled: !!activeConnectionId,
  });

  const semanticNames = semanticModel?.tables;
  const tables = (schema as SchemaData | undefined)?.tables ?? [];
  const filtered = search
    ? tables.filter(
        (t) =>
          t.name.toLowerCase().includes(search.toLowerCase()) ||
          t.columns.some((c) => c.name.toLowerCase().includes(search.toLowerCase())),
      )
    : tables;

  const schemaGroups = new Map<string, SchemaTable[]>();
  for (const t of filtered) {
    const key = t.schema_name || 'public';
    const existing = schemaGroups.get(key) ?? [];
    existing.push(t);
    schemaGroups.set(key, existing);
  }
  const multiSchema = schemaGroups.size > 1 || (schemaGroups.size === 1 && !schemaGroups.has('public'));

  // For 3-part naming: derive a catalog label from the first table in each group.
  const catalogBySchema = new Map<string, string>();
  for (const t of filtered) {
    if (t.catalog) catalogBySchema.set(t.schema_name || 'public', t.catalog);
  }

  const toggleTable = (key: string) => {
    setOpenTables((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleSchema = (name: string) => {
    setCollapsedSchemas((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const allTableKeys = filtered.map((t) => `${t.catalog ?? ''}.${t.schema_name}.${t.name}`);
  const allSchemaNames = Array.from(schemaGroups.keys());

  const expandAll = () => {
    setCollapsedSchemas(new Set());
    setOpenTables(new Set(allTableKeys));
  };

  const collapseAll = () => {
    setOpenTables(new Set());
    setCollapsedSchemas(new Set(allSchemaNames));
  };

  return (
    <div
      className={cn(
        'relative flex flex-col animate-fade-in',
        embedded ? 'flex-1 min-h-0' : 'h-full shrink-0 border-r border-border bg-background',
      )}
      style={embedded ? undefined : { width }}
    >
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-border px-3 py-2">
        <div className="flex items-center gap-1">
          {filtered.length > 0 && (
            <>
              <button
                onClick={expandAll}
                className="text-[10px] text-muted-foreground transition-colors hover:text-foreground"
              >
                Expand all
              </button>
              <span className="text-[10px] text-muted-foreground">·</span>
              <button
                onClick={collapseAll}
                className="text-[10px] text-muted-foreground transition-colors hover:text-foreground"
              >
                Collapse all
              </button>
            </>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleRefreshSchema}
            disabled={!activeConnectionId || isRefreshing}
            title="Refresh schema"
            className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40"
          >
            {isRefreshing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
          </button>
          {!embedded && (
            <button
              onClick={onClose}
              title="Close panel"
              className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Search */}
      <div className="border-b border-border px-3 py-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tables & columns…"
            className="h-8 w-full rounded-md border border-border bg-secondary pl-8 pr-2 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>

      {/* Table list */}
      <div className="flex-1 overflow-y-auto p-2">
        {filtered.length === 0 ? (
          <div className="py-4 text-center text-xs text-muted-foreground">
            {tables.length === 0 ? (
              <div className="space-y-2">
                <p>{isRefreshing ? 'Loading schema…' : 'No schema loaded'}</p>
                {!isRefreshing && activeConnectionId && (
                  <button
                    onClick={handleRefreshSchema}
                    className="inline-flex items-center gap-1.5 rounded-md bg-brand-gradient px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
                  >
                    <RefreshCw className="h-3 w-3" />
                    Load Schema
                  </button>
                )}
                {refreshError && (
                  <p className="text-destructive">{refreshError}</p>
                )}
              </div>
            ) : (
              'No tables found'
            )}
          </div>
        ) : multiSchema ? (
          Array.from(schemaGroups.entries()).map(([schemaName, schemaTables]) => (
            <div key={schemaName}>
              <button
                onClick={() => toggleSchema(schemaName)}
                className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground transition-colors hover:bg-muted"
              >
                {collapsedSchemas.has(schemaName) ? (
                  <ChevronRight className="h-3 w-3" />
                ) : (
                  <ChevronDown className="h-3 w-3" />
                )}
                {catalogBySchema.has(schemaName) ? (
                  <>
                    <span className="text-muted-foreground/60">{catalogBySchema.get(schemaName)} ·</span>
                    {' '}{schemaName}
                  </>
                ) : schemaName}
              </button>
              {!collapsedSchemas.has(schemaName) && (
                <div className="ml-2">
                  {schemaTables.map((t) => {
                    const key = `${t.catalog ?? ''}.${t.schema_name}.${t.name}`;
                    const displayName =
                      semanticNames?.[`${t.schema_name}.${t.name}`]?.display_name ??
                      semanticNames?.[t.name]?.display_name;
                    return (
                      <TableRow
                        key={key}
                        table={t}
                        displayName={displayName}
                        isOpen={openTables.has(key)}
                        onToggle={() => toggleTable(key)}
                      />
                    );
                  })}
                </div>
              )}
            </div>
          ))
        ) : (
          filtered.map((t) => {
            const key = `${t.catalog ?? ''}.${t.schema_name}.${t.name}`;
            const displayName =
              semanticNames?.[`${t.schema_name}.${t.name}`]?.display_name ??
              semanticNames?.[t.name]?.display_name;
            return (
              <TableRow
                key={key}
                table={t}
                displayName={displayName}
                isOpen={openTables.has(key)}
                onToggle={() => toggleTable(key)}
              />
            );
          })
        )}
      </div>

      {/* Resize handle — standalone mode only */}
      {!embedded && (
        <div
          onMouseDown={(e) => {
            isResizing.current = true;
            startX.current = e.clientX;
            startWidth.current = width;
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'col-resize';
            e.preventDefault();
          }}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50"
        />
      )}
    </div>
  );
}
