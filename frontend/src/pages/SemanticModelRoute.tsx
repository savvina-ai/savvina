// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useParams } from 'react-router';
import SemanticModelPage from './SemanticModelPage';

/**
 * The `semantic/:connectionId` route element. React Router reuses the element when only
 * the param changes, so without a key the page's draft, progress bar and any pending
 * generation would carry over to the next connection — and TanStack runs a pending
 * mutation's onSuccess with the latest options, writing one connection's model into
 * another's cache and draft. Keying on the param gives every connection a fresh page.
 */
export default function SemanticModelRoute() {
  const { connectionId } = useParams<{ connectionId: string }>();
  return <SemanticModelPage key={connectionId} />;
}
