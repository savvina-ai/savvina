// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import SharePage from '../SharePage';
import { shareApi } from '../../api/share';

vi.mock('../../components/ChartView', () => ({ default: () => <div /> }));
vi.mock('../../components/ChartEditor', () => ({ default: () => <div /> }));
vi.mock('html-to-image', () => ({ toPng: vi.fn() }));
vi.mock('../../api/share', () => ({ shareApi: { getSharedResult: vi.fn() } }));

const STALE = 'These results were computed under a different row filter. Re-run the question.';

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/share/tok']}>
      <Routes>
        <Route path="/share/:token" element={<SharePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SharePage errors', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the server text on a 410', async () => {
    vi.mocked(shareApi.getSharedResult).mockRejectedValue({
      response: { status: 410, data: { detail: STALE } },
    });
    renderPage();
    expect(await screen.findByText(STALE)).toBeInTheDocument();
  });

  it('shows the generic text on a 404', async () => {
    vi.mocked(shareApi.getSharedResult).mockRejectedValue({
      response: { status: 404, data: { detail: 'Not found' } },
    });
    renderPage();
    expect(
      await screen.findByText('This shared result is unavailable or has expired.'),
    ).toBeInTheDocument();
  });

  it('keeps the generic text on an expired-link 410', async () => {
    vi.mocked(shareApi.getSharedResult).mockRejectedValue({
      response: { status: 410, data: { detail: 'Share link has expired' } },
    });
    renderPage();
    expect(
      await screen.findByText('This shared result is unavailable or has expired.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Share link has expired')).not.toBeInTheDocument();
  });

  it('keeps the generic text on a 410 whose detail is a validation list', async () => {
    vi.mocked(shareApi.getSharedResult).mockRejectedValue({
      response: { status: 410, data: { detail: [{ loc: ['path', 'token'], msg: 'bad' }] } },
    });
    renderPage();
    expect(
      await screen.findByText('This shared result is unavailable or has expired.'),
    ).toBeInTheDocument();
  });
});
