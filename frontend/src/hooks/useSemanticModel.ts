// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useQuery } from '@tanstack/react-query';
import { semanticApi } from '../api/semantic';

/**
 * Fetch the semantic model for a connection. Shares the ['semantic', id] cache
 * key with SemanticModelPage so both consumers hit the same query.
 *
 * `retry: false` because the endpoint 404s when a connection simply has no model
 * yet — that is a normal state, not a transient failure worth retrying.
 */
export function useSemanticModel(connectionId: string | null) {
  return useQuery({
    queryKey: ['semantic', connectionId],
    queryFn: () => semanticApi.get(connectionId!),
    enabled: !!connectionId,
    retry: false,
  });
}
