// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { connectionsApi } from '../api/connections';
import type { SchemaResponse } from '../types';

/** `GET /connections/{id}/schema`, with a 404 (no schema cached yet, as after a semantic
 *  save until the next refresh) read as `null` rather than an error. */
export async function fetchCachedSchema(id: string): Promise<SchemaResponse | null> {
  try {
    return (await connectionsApi.getSchema(id)) as SchemaResponse;
  } catch (e: unknown) {
    const err = e as { response?: { status?: number } };
    if (err?.response?.status === 404) return null;
    throw e;
  }
}

/** The one definition of the `['schema', id]` query. Every reader of that key uses it, so
 *  whichever runs first caches the same shape (a 404 is `null` for all of them). */
export function schemaQueryOptions(id: string | null | undefined) {
  return {
    queryKey: ['schema', id] as const,
    queryFn: () => fetchCachedSchema(id!),
    enabled: !!id,
    staleTime: 5 * 60_000,
  };
}
