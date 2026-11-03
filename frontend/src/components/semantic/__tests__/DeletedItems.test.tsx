// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../../../api/semantic', () => ({
  semanticApi: { getTombstones: vi.fn(), restoreTombstone: vi.fn() },
}));

import { semanticApi } from '../../../api/semantic';
import DeletedItems from '../DeletedItems';

const ENTRIES = [
  { kind: 'metric', key: 'Revenue', created_at: '2026-11-03T12:00:00Z' },
  {
    kind: 'relationship',
    key: 'public.orders.customer_id->public.customers.id',
    created_at: '2026-11-02T12:00:00Z',
  },
];

function renderList(props: { enabled?: boolean; disabled?: boolean } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DeletedItems connectionId="conn-1" enabled={props.enabled ?? true} disabled={props.disabled} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DeletedItems', () => {
  it('lists every deleted item with its kind', async () => {
    vi.mocked(semanticApi.getTombstones).mockResolvedValue(ENTRIES as never);
    renderList();
    expect(await screen.findByText('Deleted items (2)')).toBeInTheDocument();
    expect(screen.getByText('Revenue')).toBeInTheDocument();
    expect(screen.getByText('Relationship')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^Restore / })).toHaveLength(2);
  });

  it('renders nothing while the list is empty', async () => {
    vi.mocked(semanticApi.getTombstones).mockResolvedValue([] as never);
    const { container } = renderList();
    await waitFor(() => expect(semanticApi.getTombstones).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('does not fetch before a model exists', () => {
    renderList({ enabled: false });
    expect(semanticApi.getTombstones).not.toHaveBeenCalled();
  });

  it('restores an item, refetches and says what happens next', async () => {
    vi.mocked(semanticApi.getTombstones)
      .mockResolvedValueOnce(ENTRIES as never)
      .mockResolvedValue([ENTRIES[1]] as never);
    vi.mocked(semanticApi.restoreTombstone).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole('button', { name: 'Restore metric Revenue' }));

    expect(semanticApi.restoreTombstone).toHaveBeenCalledWith('conn-1', 'metric', 'Revenue');
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Restored metric Revenue: a later Regenerate or Apply Drift may bring it back.',
    );
    await waitFor(() => expect(screen.queryByText('Revenue')).not.toBeInTheDocument());
    expect(screen.getByText('Deleted items (1)')).toBeInTheDocument();
  });

  it('says a restored join does not come back by itself', async () => {
    vi.mocked(semanticApi.getTombstones)
      .mockResolvedValueOnce([
        { kind: 'common_join', key: 'a JOIN b', created_at: '2026-11-03T12:00:00Z' },
      ] as never)
      .mockResolvedValue([] as never);
    vi.mocked(semanticApi.restoreTombstone).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole('button', { name: 'Restore join a JOIN b' }));
    expect(await screen.findByRole('status')).toHaveTextContent('joins are not generated');
  });

  it('keeps the note for a restored relationship once the list is empty', async () => {
    vi.mocked(semanticApi.getTombstones)
      .mockResolvedValueOnce([ENTRIES[1]] as never)
      .mockResolvedValue([] as never);
    vi.mocked(semanticApi.restoreTombstone).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderList();
    await user.click(await screen.findByRole('button', { name: /^Restore relationship / }));
    // The note stays once the list is empty.
    expect(await screen.findByRole('status')).toHaveTextContent('a later Regenerate or Apply Drift');
  });

  it('disables Restore while a run is in flight', async () => {
    vi.mocked(semanticApi.getTombstones).mockResolvedValue(ENTRIES as never);
    renderList({ disabled: true });
    const buttons = await screen.findAllByRole('button', { name: /^Restore / });
    buttons.forEach((b) => expect(b).toBeDisabled());
  });
});
