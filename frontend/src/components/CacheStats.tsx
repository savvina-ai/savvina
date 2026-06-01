// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trash2, ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { chatApi } from '../api/chat';
import { useCacheStats } from '../hooks/useChat';
import { cn } from '../lib/utils';

interface Props {
  connectionId: string;
}

const PAGE_SIZE = 20;

export default function CacheStats({ connectionId }: Props) {
  const { data: stats, isLoading } = useCacheStats();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);

  const entriesQuery = useQuery({
    queryKey: ['cacheEntries', connectionId, offset, search],
    queryFn: () => chatApi.getCacheEntries(connectionId, PAGE_SIZE, offset, search || undefined),
  });

  const clearCache = useMutation({
    mutationFn: () => chatApi.clearCache(connectionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cacheStats'] });
      queryClient.invalidateQueries({ queryKey: ['cacheEntries', connectionId] });
      setOffset(0);
    },
  });

  const deleteEntry = useMutation({
    mutationFn: (entryId: string) => chatApi.deleteCacheEntry(entryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cacheStats'] });
      queryClient.invalidateQueries({ queryKey: ['cacheEntries', connectionId] });
    },
  });

  const handleSearch = (value: string) => {
    setSearch(value);
    setOffset(0);
  };

  if (isLoading) {
    return <div className="h-24 bg-muted rounded-lg animate-pulse" />;
  }

  if (!stats) return null;

  const entries = entriesQuery.data?.items ?? [];
  const total = entriesQuery.data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="rounded-lg border border-border p-4 space-y-4">
      {/* Header + clear all */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">Query Cache</h3>
        <button
          onClick={() => clearCache.mutate()}
          disabled={clearCache.isPending}
          className="text-xs text-destructive hover:text-destructive/80 disabled:opacity-50"
        >
          Clear All
        </button>
      </div>

      {/* Stats summary */}
      <div className="grid grid-cols-3 gap-3">
        <Stat label="Cached queries" value={stats.total_entries} />
        <Stat label="Hit rate" value={`${(stats.hit_rate * 100).toFixed(0)}%`} />
        <Stat label="LLM calls saved" value={stats.hit_count} />
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
        <input
          type="text"
          value={search}
          onChange={e => handleSearch(e.target.value)}
          placeholder="Search cached questions…"
          className="w-full pl-8 pr-3 py-1.5 text-xs rounded-md border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </div>

      {/* Entries list */}
      <div className="space-y-1 min-h-[60px]">
        {entriesQuery.isLoading ? (
          <div className="h-12 bg-muted rounded animate-pulse" />
        ) : entries.length === 0 ? (
          <p className="text-xs text-muted-foreground text-center py-4">
            {search ? 'No matching entries' : 'No cached entries yet'}
          </p>
        ) : (
          entries.map(entry => (
            <div
              key={entry.id}
              className="flex items-center justify-between gap-2 rounded px-2 py-1.5 hover:bg-muted/50 group"
            >
              <div className="flex-1 min-w-0">
                <p className="text-xs text-foreground truncate">{entry.question_raw}</p>
                <p className="text-[10px] text-muted-foreground">
                  {entry.query_dialect} · ×{entry.hit_count}
                </p>
              </div>
              <button
                onClick={() => deleteEntry.mutate(entry.id)}
                disabled={deleteEntry.isPending}
                className={cn(
                  'flex-shrink-0 p-1 rounded text-muted-foreground opacity-0 group-hover:opacity-100',
                  'hover:text-destructive hover:bg-destructive/10 disabled:opacity-30 transition-opacity'
                )}
                title="Delete entry"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))
        )}
      </div>

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between pt-1 border-t border-border">
          <span className="text-[10px] text-muted-foreground">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <div className="flex gap-1">
            <button
              onClick={() => setOffset(o => Math.max(0, o - PAGE_SIZE))}
              disabled={currentPage === 1}
              className="p-0.5 rounded text-muted-foreground hover:text-foreground disabled:opacity-30"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <span className="text-[10px] text-muted-foreground self-center px-1">
              {currentPage}/{totalPages}
            </span>
            <button
              onClick={() => setOffset(o => o + PAGE_SIZE)}
              disabled={currentPage === totalPages}
              className="p-0.5 rounded text-muted-foreground hover:text-foreground disabled:opacity-30"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="text-center">
      <p className="text-lg font-bold text-foreground">{value}</p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}
