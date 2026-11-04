// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AxiosError, AxiosHeaders } from 'axios';
import ResultsView from '../ResultsView';
import { makeQueryResults } from '../../test/factories';
import { chatApi } from '../../api/chat';
import { useConnection } from '../../hooks/useConnections';
import { useAppStore } from '../../store/appStore';

vi.mock('../ChartView', () => ({ default: () => <div data-testid="chart-view" /> }));
vi.mock('../ChartEditor', () => ({ default: () => <div data-testid="chart-editor" /> }));
vi.mock('../ColumnStatsView', () => ({ default: () => <div data-testid="column-stats" /> }));
vi.mock('html-to-image', () => ({ toPng: vi.fn() }));
vi.mock('../../api/chat', () => ({ chatApi: { sortResults: vi.fn() } }));
vi.mock('../../api/export', () => ({ exportApi: { downloadXlsx: vi.fn() } }));
vi.mock('../../api/share', () => ({ shareApi: { shareMessage: vi.fn() } }));
vi.mock('../../hooks/useConnections', () => ({ useConnection: vi.fn() }));

const STRICT_TOOLTIP = 'Sorting is off for a masked column under Strict sensitive columns.';

function setStrict(strict: boolean | undefined) {
  vi.mocked(useConnection).mockReturnValue({
    data:
      strict === undefined
        ? undefined
        : { privacy_settings: { strict_sensitive_columns: strict } },
  } as unknown as ReturnType<typeof useConnection>);
}

// `email` is masked: every value, NULL included, comes back as [REDACTED].
const masked = makeQueryResults({
  columns: ['id', 'email'],
  column_types: ['integer', 'text'],
  rows: [
    [2, '[REDACTED]'],
    [1, '[REDACTED]'],
  ],
  row_count: 2,
  truncated: true,
});

function header(name: string) {
  return screen.getByRole('columnheader', { name: new RegExp(`^${name}`) });
}

function sortButton(name: string) {
  return within(header(name)).getByRole('button');
}

function firstColumn() {
  return screen
    .getAllByRole('row')
    .slice(1)
    .map((r) => within(r).getAllByRole('cell')[0].textContent);
}

/** Tab until `el` has focus (bounded, so a missing button fails instead of hanging). */
async function tabTo(user: ReturnType<typeof userEvent.setup>, el: HTMLElement) {
  for (let i = 0; i < 30 && document.activeElement !== el; i++) await user.tab();
  expect(document.activeElement).toBe(el);
}

beforeEach(() => {
  useAppStore.setState({ activeConnectionId: 'conn-9' });
  vi.mocked(useConnection).mockReset();
  vi.mocked(chatApi.sortResults).mockReset();
  vi.mocked(chatApi.sortResults).mockResolvedValue(masked);
});

describe('ResultsView sort on a masked column', () => {
  it('reads the privacy settings of the active connection', () => {
    setStrict(true);
    render(<ResultsView results={masked} messageId="m1" />);
    expect(useConnection).toHaveBeenCalledWith('conn-9');
  });

  it('disables the sort on a masked column under strict mode, with the reason', async () => {
    setStrict(true);
    const user = userEvent.setup();
    render(<ResultsView results={masked} messageId="m1" />);
    const button = sortButton('email');
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(STRICT_TOOLTIP);
    expect(header('email')).toHaveAttribute('title', STRICT_TOOLTIP);
    await user.click(button);
    expect(chatApi.sortResults).not.toHaveBeenCalled();
    expect(header('email')).not.toHaveAttribute('aria-sort');
  });

  it('keeps the sort on an unmasked column under strict mode', async () => {
    setStrict(true);
    const user = userEvent.setup();
    render(<ResultsView results={masked} messageId="m1" />);
    const button = sortButton('id');
    expect(button).toBeEnabled();
    expect(header('id')).not.toHaveAttribute('title');
    await user.click(button);
    expect(chatApi.sortResults).toHaveBeenCalledWith('m1', 'id', 'ASC');
  });

  it('keeps the sort on a masked column when strict mode is off', async () => {
    setStrict(false);
    const user = userEvent.setup();
    render(<ResultsView results={masked} messageId="m1" />);
    await user.click(sortButton('email'));
    expect(chatApi.sortResults).toHaveBeenCalledWith('m1', 'email', 'ASC');
  });

  it('keeps the sort while the connection settings are not loaded', () => {
    setStrict(undefined);
    render(<ResultsView results={masked} messageId="m1" />);
    expect(sortButton('email')).toBeEnabled();
  });

  it('does not treat a column with some real values as masked', async () => {
    setStrict(true);
    const user = userEvent.setup();
    const partly = makeQueryResults({
      columns: ['id', 'note'],
      rows: [
        [1, '[REDACTED]'],
        [2, 'hello'],
      ],
      row_count: 2,
      truncated: true,
    });
    render(<ResultsView results={partly} messageId="m1" />);
    await user.click(sortButton('note'));
    expect(chatApi.sortResults).toHaveBeenCalledWith('m1', 'note', 'ASC');
  });

  it('disables the client-side sort on a masked column too: a click leaves the rows as they were', async () => {
    setStrict(true);
    const user = userEvent.setup();
    render(<ResultsView results={{ ...masked, truncated: false }} messageId="m1" />);
    expect(sortButton('email')).toBeDisabled();
    await user.click(sortButton('email'));
    expect(firstColumn()).toEqual(['2', '1']);
    expect(header('email')).not.toHaveAttribute('aria-sort');
  });
});

describe('ResultsView header keyboard sort', () => {
  it('sorts with Enter and Space from the keyboard and reports aria-sort', async () => {
    setStrict(false);
    const user = userEvent.setup();
    render(<ResultsView results={{ ...masked, truncated: false }} messageId="m1" />);
    await tabTo(user, sortButton('id'));

    await user.keyboard('{Enter}');
    expect(firstColumn()).toEqual(['1', '2']);
    expect(header('id')).toHaveAttribute('aria-sort', 'ascending');
    expect(header('email')).not.toHaveAttribute('aria-sort');

    await user.keyboard(' ');
    expect(firstColumn()).toEqual(['2', '1']);
    expect(header('id')).toHaveAttribute('aria-sort', 'descending');
  });

  it('skips a blocked header in the tab order', async () => {
    setStrict(true);
    const user = userEvent.setup();
    render(<ResultsView results={{ ...masked, truncated: false }} messageId="m1" />);
    await tabTo(user, sortButton('id'));
    await user.tab();
    expect(document.activeElement).not.toBe(sortButton('email'));
  });
});

describe('ResultsView sort failure', () => {
  const DETAIL = 'Strict sensitive columns: the query sorts by a sensitive column, so it was not run.';

  it('shows the server refusal when the database sort is rejected', async () => {
    setStrict(false);
    const user = userEvent.setup();
    vi.mocked(chatApi.sortResults).mockRejectedValue(
      new AxiosError('Bad Request', '400', undefined, undefined, {
        status: 400,
        statusText: 'Bad Request',
        data: { detail: DETAIL },
        headers: {},
        config: { headers: new AxiosHeaders() },
      }),
    );
    render(<ResultsView results={masked} messageId="m1" />);
    await user.click(sortButton('email'));
    expect(await screen.findByText(DETAIL)).toBeInTheDocument();
  });

  it('falls back to the generic message without a server detail', async () => {
    setStrict(false);
    const user = userEvent.setup();
    vi.mocked(chatApi.sortResults).mockRejectedValue(new Error('Network Error'));
    render(<ResultsView results={masked} messageId="m1" />);
    await user.click(sortButton('id'));
    expect(await screen.findByText('Sort failed — results may be unordered')).toBeInTheDocument();
  });
});
