// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ReportBuilderPage from '../ReportBuilderPage';
import { chatApi } from '../../api/chat';
import { exportApi } from '../../api/export';
import { makeQueryResults } from '../../test/factories';

vi.mock('../../store/appStore', () => ({
  useAppStore: (select: (s: { activeConnectionId: string }) => unknown) =>
    select({ activeConnectionId: 'conn-1' }),
}));
vi.mock('../../hooks/useConnections', () => ({
  useConnections: () => ({ data: [{ id: 'conn-1', name: 'DB', source_type: 'postgresql' }] }),
}));
vi.mock('../../hooks/useChat', () => ({
  useSessions: () => ({
    data: [
      {
        id: 'sess-1',
        connection_id: 'conn-1',
        title: 'Orders',
        provider: 'p',
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-01T00:00:00Z',
        cache_hit_count: 0,
      },
    ],
    isLoading: false,
  }),
}));
vi.mock('../../api/chat', () => ({ chatApi: { getHistory: vi.fn() } }));
vi.mock('../../api/export', () => ({ exportApi: { downloadReport: vi.fn() } }));
vi.mock('@/components/ChartView', () => ({ default: () => <div /> }));
vi.mock('html-to-image', () => ({ toPng: vi.fn() }));

const STALE = 'Results outdated (row filter changed); re-run the question';

function message(id: string, results: ReturnType<typeof makeQueryResults>) {
  return {
    id,
    session_id: 'sess-1',
    role: 'assistant' as const,
    content: `Question ${id}`,
    query_generated: 'SELECT 1',
    results_json: results,
    status: 'completed',
    created_at: '2026-10-01T00:00:00Z',
  };
}

describe('ReportBuilderPage stale results', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(chatApi.getHistory).mockResolvedValue([
      message('fresh', makeQueryResults()),
      message('stale', makeQueryResults({ rows: [], row_count: 0, stale_row_filter: true })),
    ] as never);
    vi.mocked(exportApi.downloadReport).mockResolvedValue(undefined as never);
  });

  it('names the stale message instead of counting its rows, and offers no chart', async () => {
    render(<ReportBuilderPage />);
    await userEvent.click(await screen.findByText('Orders'));

    expect(await screen.findByText('3 rows')).toBeInTheDocument();
    expect(screen.getByText(STALE)).toBeInTheDocument();
    expect(screen.queryByText('0 rows')).not.toBeInTheDocument();

    await userEvent.click(screen.getByText('Question fresh'));
    await userEvent.click(screen.getByText('Question stale'));
    expect(screen.getAllByRole('button', { name: /Add chart/ })).toHaveLength(1);
    // The stale section stays selectable and carries the notice.
    expect(screen.getAllByText(STALE)).toHaveLength(2);
  });

  it('a stale section cannot get a chart and sends none', async () => {
    render(<ReportBuilderPage />);
    await userEvent.click(await screen.findByText('Orders'));
    await userEvent.click(await screen.findByText('Question stale'));
    fireEvent.change(screen.getByLabelText('Report Title'), { target: { value: 'R' } });
    await userEvent.click(screen.getByRole('button', { name: /Generate PDF/ }));

    expect(exportApi.downloadReport).toHaveBeenCalledWith('R', [
      { message_id: 'stale', heading: 'Question stale', chart_image: undefined, chart_title: undefined },
    ]);
  });
});
