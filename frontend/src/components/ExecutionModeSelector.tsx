// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useRef, useEffect, useState } from 'react';
import type { ElementType } from 'react';
import { Zap, Eye, FileCode, Check, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Connection } from '../types';

type Mode = Connection['execution_mode'];

interface ModeConfig {
  value: Mode;
  label: string;
  badge: string;
  description: string;
  icon: ElementType;
  dotColor: string;
  iconColor: string;
}

const MODES: ModeConfig[] = [
  {
    value: 'auto_execute',
    label: 'Auto-Execute',
    badge: 'AUTO',
    description: 'Queries run immediately. Fastest workflow.',
    icon: Zap,
    dotColor: 'bg-warning',
    iconColor: 'text-warning',
  },
  {
    value: 'review_first',
    label: 'Review First',
    badge: 'REVIEW',
    description: 'See the query before it runs. Best for cloud DBs.',
    icon: Eye,
    dotColor: 'bg-primary',
    iconColor: 'text-primary',
  },
  {
    value: 'generate_only',
    label: 'Generate Only',
    badge: 'GEN',
    description: 'Just generate the query. Copy and run it yourself.',
    icon: FileCode,
    dotColor: 'bg-info',
    iconColor: 'text-info',
  },
];

interface Props {
  value: Mode;
  onChange: (mode: Mode) => void;
}

export default function ExecutionModeSelector({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const current = MODES.find((m) => m.value === value) ?? MODES[0];

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 rounded-full border border-border bg-surface-elevated px-2.5 py-1 text-xs transition-colors hover:bg-muted"
      >
        <current.icon className={cn('h-3 w-3', current.iconColor)} />
        <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {current.badge}
        </span>
        <ChevronDown className="h-3 w-3 text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-2 w-64 overflow-hidden rounded-xl border border-border bg-surface-elevated shadow-lg animate-fade-in">
          {MODES.map((mode) => (
            <button
              key={mode.value}
              onClick={() => {
                onChange(mode.value);
                setOpen(false);
              }}
              className="flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted"
            >
              <mode.icon
                className={cn('mt-0.5 h-4 w-4 shrink-0', mode.iconColor)}
              />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-foreground">{mode.label}</span>
                  <span className="rounded bg-badge-bg px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase text-badge-text">
                    {mode.badge}
                  </span>
                  {value === mode.value && (
                    <Check className="ml-auto h-3.5 w-3.5 text-primary" />
                  )}
                </div>
                <p className="mt-0.5 text-[11px] text-muted-foreground">{mode.description}</p>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
