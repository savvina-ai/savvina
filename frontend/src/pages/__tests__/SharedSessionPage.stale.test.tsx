// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import SharedSessionPage from '../SharedSessionPage';
import { shareApi } from '../../api/share';
import type { PublicMessageSummary } from '../../api/share';
import { makeQueryResults } from '../../test/factories';

vi.mock('../../components/QueryHighlight', () => ({ default: () => <div /> }));
vi.mock('../../api/share', () => ({ shareApi: { getSharedSession: vi.fn() } }));

const STALE = 'These results were computed under a different row filter. Re-run the question.';

function message(results: PublicMessageSummary['results_json']): PublicMessageSummary {
  return {
    role: 'assistant',
    content: '',
    query_generated: 'SELECT 1',
    query_dialect: 'postgresql',
    results_json: results,
    execution_time_ms: 5,
    status: 'executed',
    created_at: '2026-01-01T00:00:00Z',
  };
}

function renderPage(msg: PublicMessageSummary) {
  vi.mocked(shareApi.getSharedSession).mockResolvedValue({
    data: { title: 'Session', messages: [msg] },
  } as never);
  return render(
    <MemoryRouter initialEntries={['/share/session/tok']}>
      <Routes>
        <Route path="/share/session/:token" element={<SharedSessionPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SharedSessionPage under a stale row filter', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the notice, no CSV button and no row count', async () => {
    renderPage(message(makeQueryResults({ rows: [], row_count: 0, stale_row_filter: true })));
    expect(await screen.findByText(STALE)).toBeInTheDocument();
    expect(screen.queryByText('Download CSV')).not.toBeInTheDocument();
    expect(screen.queryByText(/^\d+ rows?$/)).not.toBeInTheDocument();
  });

  it('shows the table, row count and CSV button for a fresh result', async () => {
    renderPage(message(makeQueryResults()));
    expect(await screen.findByText('Download CSV')).toBeInTheDocument();
    expect(screen.getAllByText(/3 rows/).length).toBeGreaterThan(0);
  });
});
