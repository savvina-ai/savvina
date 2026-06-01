// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useQuery } from '@tanstack/react-query';
import { datasourcesApi } from '../api/datasources';
import { getDatasourceIcon } from '@/lib/datasourceIcons';
import type { DataSourceInfo } from '../types';

interface Props {
  onSelect: (source: DataSourceInfo) => void;
}

export default function DataSourceSelector({ onSelect }: Props) {
  const { data: sources, isLoading, error } = useQuery({
    queryKey: ['datasources'],
    queryFn: datasourcesApi.getAvailable,
  });

  if (isLoading) {
    return (
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-24 bg-muted rounded-xl animate-pulse" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-destructive text-sm">
        Could not load available databases. Please check your connection and try refreshing the page.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
      {sources?.map((source) => (
        <button
          key={source.source_type}
          onClick={() => onSelect(source)}
          className="flex flex-col items-center gap-2 p-6 border-2 border-border hover:border-ring hover:bg-accent rounded-xl transition-all text-center"
        >
          {getDatasourceIcon(source.source_type) ? (
            <img
              src={getDatasourceIcon(source.source_type)!}
              alt={source.display_name}
              className="h-10 w-10 object-contain"
            />
          ) : (
            <span className="text-3xl">{source.icon}</span>
          )}
          <span className="text-sm font-medium text-foreground">{source.display_name}</span>
          <span className="text-xs text-muted-foreground">{source.query_dialect.toUpperCase()}</span>
        </button>
      ))}
    </div>
  );
}
