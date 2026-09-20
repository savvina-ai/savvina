// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useCallback, useMemo, useState } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query';
import { settingsApi } from '../api/settings';
import type { AppSettings } from '../types';

export interface StagedSettings<K extends keyof AppSettings> {
  /** Server values with the user's staged edits laid over them; null until the GET resolves. */
  values: Pick<AppSettings, K> | null;
  set: <P extends K>(key: P, value: AppSettings[P]) => void;
  /** True while at least one staged value differs from what the server has. */
  dirty: boolean;
  save: () => void;
  update: UseMutationResult<AppSettings, Error, Pick<AppSettings, K>>;
  isLoading: boolean;
}

/**
 * Stage a subset of AppSettings locally and write them with one PUT on Save.
 *
 * Only the keys the user has touched are held here (an overrides map), and `values`
 * is derived from the server data on every render. A full local copy seeded once
 * from the server never re-synced after a save or an external change, and made
 * "✓ Saved" show beside edits that had not been saved.
 */
export function useStagedSettings<K extends keyof AppSettings>(
  keys: readonly K[],
): StagedSettings<K> {
  const queryClient = useQueryClient();
  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  });
  const [overrides, setOverrides] = useState<Partial<Pick<AppSettings, K>>>({});

  const update = useMutation({
    mutationFn: (payload: Pick<AppSettings, K>) => settingsApi.update(payload),
    onSuccess: (data) => {
      // Seed the cache from the PUT response before clearing the overrides, otherwise
      // `dirty` flips true again until the invalidation refetch lands.
      queryClient.setQueryData(['settings'], data);
      setOverrides({});
      void queryClient.invalidateQueries({ queryKey: ['settings'] });
    },
  });

  const values = useMemo(() => {
    if (!settings) return null;
    return Object.fromEntries(
      keys.map((k) => [k, k in overrides ? overrides[k] : settings[k]]),
    ) as Pick<AppSettings, K>;
  }, [settings, overrides, keys]);

  const dirty =
    !!settings && keys.some((k) => k in overrides && overrides[k] !== settings[k]);

  const set = useCallback(<P extends K>(key: P, value: AppSettings[P]) => {
    setOverrides((o) => ({ ...o, [key]: value }));
  }, []);

  const save = useCallback(() => {
    if (values) update.mutate(values);
  }, [values, update]);

  return { values, set, dirty, save, update, isLoading };
}
