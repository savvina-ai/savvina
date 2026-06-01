// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useRef, useState } from 'react';
import { Database, History as HistoryIcon, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import SchemaExplorer from './SchemaExplorer';
import HistoryPanel from './HistoryPanel';

type Tab = 'schema' | 'history';

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function WorkspacePanel({ open, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('schema');
  const [width, setWidth] = useState(280);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  useEffect(() => {
    const onMouseMove = (e: globalThis.MouseEvent) => {
      if (!isResizing.current) return;
      const delta = startX.current - e.clientX;
      setWidth(Math.min(500, Math.max(200, startWidth.current + delta)));
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
  }, []);

  const triggerClass = (t: Tab) =>
    cn(
      'flex flex-1 items-center justify-center gap-1.5 rounded-sm px-3 py-1.5 text-xs font-medium transition-colors',
      tab === t
        ? 'bg-primary/15 text-primary font-semibold'
        : 'text-muted-foreground hover:text-foreground',
    );

  return (
    <aside
      className={cn(
        'relative flex shrink-0 flex-col border-l border-border bg-background transition-[width] duration-200 overflow-hidden',
        !open && 'w-0 border-l-0',
      )}
      style={open ? { width } : undefined}
    >
      {/* Tab bar + close button */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex flex-1 rounded-md bg-secondary p-1">
          <button className={triggerClass('schema')} onClick={() => setTab('schema')}>
            <Database className="h-3.5 w-3.5" />
            Schema
          </button>
          <button className={triggerClass('history')} onClick={() => setTab('history')}>
            <HistoryIcon className="h-3.5 w-3.5" />
            History
          </button>
        </div>
        <button
          onClick={onClose}
          aria-label="Close workspace panel"
          className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Active tab content — only one rendered at a time */}
      {tab === 'schema' && <SchemaExplorer embedded />}
      {tab === 'history' && <HistoryPanel embedded />}

      {/* Resize handle on left edge */}
      <div
        onMouseDown={(e) => {
          isResizing.current = true;
          startX.current = e.clientX;
          startWidth.current = width;
          document.body.style.userSelect = 'none';
          document.body.style.cursor = 'col-resize';
          e.preventDefault();
        }}
        className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50"
      />
    </aside>
  );
}
