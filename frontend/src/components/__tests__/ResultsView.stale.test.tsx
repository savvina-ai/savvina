// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ResultsView from '../ResultsView';
import { makeQueryResults } from '../../test/factories';

vi.mock('../ChartView', () => ({ default: () => <div data-testid="chart-view" /> }));
vi.mock('../ChartEditor', () => ({ default: () => <div data-testid="chart-editor" /> }));
vi.mock('../ColumnStatsView', () => ({ default: () => <div data-testid="column-stats" /> }));
vi.mock('html-to-image', () => ({ toPng: vi.fn() }));
vi.mock('../../api/chat', () => ({ chatApi: { sortResults: vi.fn() } }));
vi.mock('../../api/export', () => ({ exportApi: { downloadXlsx: vi.fn() } }));
vi.mock('../../api/share', () => ({ shareApi: { shareMessage: vi.fn() } }));

const NOTICE = 'These results were computed under a different row filter. Re-run the question.';

describe('ResultsView under a stale row filter', () => {
  it('shows the notice and no table or actions', () => {
    const results = makeQueryResults({ rows: [], row_count: 0, stale_row_filter: true });
    render(<ResultsView results={results} messageId="m1" />);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Download as CSV')).not.toBeInTheDocument();
  });

  it('renders the table when the flag is absent', () => {
    render(<ResultsView results={makeQueryResults()} messageId="m1" />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByTitle('Download as CSV')).toBeInTheDocument();
  });
});
