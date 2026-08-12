// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { server } from './server';

// Several routes are behind React.lazy + Suspense, so a findBy*/waitFor query is
// racing a dynamic import, not just a re-render. Testing Library's 1s default
// loses that race on a loaded machine (CI runners included) and fails tests that
// are perfectly correct. Raise it; testTimeout in vitest.config.ts is set above
// this so a genuinely stuck query still reports as a query failure rather than
// an opaque test timeout.
configure({ asyncUtilTimeout: 5000 });

beforeAll(() => server.listen({ onUnhandledRequest: 'warn' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
