// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, Check } from 'lucide-react';
import { useProviders } from '../hooks/useProviders';
import { cn } from '@/lib/utils';

interface Props {
  value: string;
  onChange: (providerId: string) => void;
}

export default function ProviderSelector({ value, onChange }: Props) {
  const { data: providers, isLoading } = useProviders();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const configured = useMemo(
    () => providers?.filter((p) => p.is_configured && (p.is_active || p.id == null)) ?? [],
    [providers],
  );
  const providerKey = (p: { id: string | null; provider_type: string }) =>
    p.id ?? p.provider_type;
  const selected = configured.find((p) => providerKey(p) === value);

  // Auto-select first configured provider when value is stale/blank
  useEffect(() => {
    if (!isLoading && configured.length > 0 && !configured.find((p) => providerKey(p) === value)) {
      onChange(providerKey(configured[0]));
    }
  }, [configured, value, isLoading, onChange]);

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

  if (isLoading) {
    return (
      <div className="flex items-center gap-1.5 rounded-full border border-border bg-surface-elevated px-2.5 py-1 text-xs text-muted-foreground">
        <div className="h-1.5 w-1.5 rounded-full bg-muted animate-pulse" />
        <span className="font-mono">Loading…</span>
      </div>
    );
  }

  if (configured.length === 0) {
    return (
      <div className="flex items-center gap-1.5 rounded-full border border-destructive/30 bg-destructive/10 px-2.5 py-1 text-xs text-destructive">
        No providers configured
      </div>
    );
  }

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 rounded-full border border-border bg-surface-elevated px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted"
      >
        <div
          className={cn(
            'h-1.5 w-1.5 rounded-full',
            selected?.is_healthy ? 'bg-success' : 'bg-destructive',
          )}
        />
        <span
          className="max-w-[14rem] truncate font-mono"
          title={selected ? `${selected.display_name} · ${selected.current_model}` : undefined}
        >
          {selected
            ? `${selected.display_name} · ${selected.current_model}`
            : 'Select provider'}
        </span>
        <ChevronDown className="h-3 w-3" />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-2 w-72 overflow-hidden rounded-xl border border-border bg-surface-elevated shadow-lg animate-fade-in">
          {configured.map((p) => (
            <button
              key={providerKey(p)}
              onClick={() => {
                onChange(providerKey(p));
                setOpen(false);
              }}
              className="flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted"
            >
              <div
                className={cn(
                  'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                  p.is_healthy ? 'bg-success' : 'bg-destructive',
                )}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-foreground">{p.display_name}</span>
                  {value === providerKey(p) && <Check className="ml-auto h-3.5 w-3.5 text-primary" />}
                </div>
                <p className="truncate font-mono text-[10px] text-muted-foreground">
                  {p.current_model}
                </p>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
