// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type * as TanstackReactQuery from '@tanstack/react-query';
import ChatMessageBubble from '../ChatMessage';
import { makeChatMessage, makeQueryResults } from '../../test/factories';

// Mock heavy child components that trigger network calls
vi.mock('../QueryReviewPanel', () => ({
  default: () => <div data-testid="query-review-panel" />,
}));
vi.mock('../QueryHighlight', () => ({
  default: () => <div data-testid="query-highlight" />,
}));
vi.mock('../ResultsView', () => ({
  default: () => <div data-testid="results-view" />,
}));

// ChatMessageBubble calls useQueryClient() to invalidate the examples cache after
// feedback submission. Provide a minimal mock so tests don't need a QueryClientProvider.
vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof TanstackReactQuery>();
  return {
    ...actual,
    useQueryClient: vi.fn(() => ({ invalidateQueries: vi.fn() })),
  };
});

// Mock the chat API
vi.mock('../../api/chat', () => ({
  chatApi: {
    submitFeedback: vi.fn().mockResolvedValue({}),
  },
}));

import { chatApi } from '../../api/chat';
const mockSubmitFeedback = vi.mocked(chatApi.submitFeedback);

beforeEach(() => {
  mockSubmitFeedback.mockClear();
});

describe('ChatMessage — user message', () => {
  it('renders message content', () => {
    const msg = makeChatMessage({ role: 'user', content: 'Show me top customers' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByText('Show me top customers')).toBeInTheDocument();
  });

  it('does not render QueryHighlight, ResultsView, or feedback buttons', () => {
    const msg = makeChatMessage({ role: 'user', content: 'Hello' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTestId('query-highlight')).not.toBeInTheDocument();
    expect(screen.queryByTestId('results-view')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Good answer')).not.toBeInTheDocument();
  });
});

describe('ChatMessage — assistant status=executed', () => {
  it('renders QueryHighlight', () => {
    const msg = makeChatMessage({ status: 'executed', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTestId('query-highlight')).toBeInTheDocument();
  });

  it('renders ResultsView when results_json is present', () => {
    const msg = makeChatMessage({
      status: 'executed',
      results_json: makeQueryResults(),
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTestId('results-view')).toBeInTheDocument();
  });

  it('renders explanation text', () => {
    const msg = makeChatMessage({ status: 'executed', content: 'Joining customers with orders' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByText('Joining customers with orders')).toBeInTheDocument();
  });

  it('renders 👍 and 👎 feedback buttons', () => {
    const msg = makeChatMessage({ status: 'executed', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTitle('Good answer')).toBeInTheDocument();
    expect(screen.getByTitle('Bad answer')).toBeInTheDocument();
  });

  it('no ⚡ Cached badge when cache_hit=false', () => {
    const msg = makeChatMessage({ status: 'executed', cache_hit: false });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByText(/Cached/)).not.toBeInTheDocument();
  });
});

describe('ChatMessage — assistant status=cached', () => {
  it('renders ⚡ Cached badge', () => {
    const msg = makeChatMessage({ status: 'cached', cache_hit: true });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByText(/Cached/)).toBeInTheDocument();
  });

  it('renders QueryHighlight and ResultsView', () => {
    const msg = makeChatMessage({
      status: 'cached',
      cache_hit: true,
      query_generated: 'SELECT 1',
      results_json: makeQueryResults(),
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTestId('query-highlight')).toBeInTheDocument();
    expect(screen.getByTestId('results-view')).toBeInTheDocument();
  });

  it('renders feedback buttons', () => {
    const msg = makeChatMessage({ status: 'cached', cache_hit: true, query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTitle('Good answer')).toBeInTheDocument();
    expect(screen.getByTitle('Bad answer')).toBeInTheDocument();
  });
});

describe('ChatMessage — assistant status=pending_approval', () => {
  it('renders QueryReviewPanel', () => {
    const msg = makeChatMessage({ status: 'pending_approval', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTestId('query-review-panel')).toBeInTheDocument();
  });

  it('does NOT render ResultsView', () => {
    const msg = makeChatMessage({
      status: 'pending_approval',
      results_json: makeQueryResults(),
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTestId('results-view')).not.toBeInTheDocument();
  });

  it('no feedback buttons', () => {
    const msg = makeChatMessage({ status: 'pending_approval', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTitle('Good answer')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Bad answer')).not.toBeInTheDocument();
  });
});

describe('ChatMessage — assistant status=query_only', () => {
  it('renders QueryHighlight', () => {
    const msg = makeChatMessage({ status: 'query_only', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByTestId('query-highlight')).toBeInTheDocument();
  });

  it('does NOT render ResultsView when results_json is null', () => {
    const msg = makeChatMessage({ status: 'query_only', results_json: null });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTestId('results-view')).not.toBeInTheDocument();
  });

  it('does NOT render QueryReviewPanel', () => {
    const msg = makeChatMessage({ status: 'query_only', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTestId('query-review-panel')).not.toBeInTheDocument();
  });
});

describe('ChatMessage — assistant status=error', () => {
  it('renders error message in a red box', () => {
    const msg = makeChatMessage({
      status: 'error',
      error: 'Permission denied',
      query_generated: null,
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByText('Permission denied')).toBeInTheDocument();
  });

  it('no feedback buttons when there is no generated query', () => {
    const msg = makeChatMessage({
      status: 'error',
      error: 'Timeout',
      query_generated: null,
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByTitle('Good answer')).not.toBeInTheDocument();
  });
});

describe('ChatMessage — feedback submission', () => {
  it('clicking 👍 calls chatApi.submitFeedback with "positive"', async () => {
    const user = userEvent.setup();
    const msg = makeChatMessage({ id: 'msg-42', status: 'executed', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    await user.click(screen.getByTitle('Good answer'));
    expect(mockSubmitFeedback).toHaveBeenCalledWith('msg-42', 'positive');
  });

  it('clicking 👎 calls chatApi.submitFeedback with "negative"', async () => {
    const user = userEvent.setup();
    const msg = makeChatMessage({ id: 'msg-42', status: 'executed', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    await user.click(screen.getByTitle('Bad answer'));
    expect(mockSubmitFeedback).toHaveBeenCalledWith('msg-42', 'negative');
  });

  it('buttons are disabled after first click to prevent double-submit', async () => {
    const user = userEvent.setup();
    const msg = makeChatMessage({ status: 'executed', query_generated: 'SELECT 1' });
    render(<ChatMessageBubble message={msg} />);
    await user.click(screen.getByTitle('Good answer'));
    await waitFor(() => {
      expect(screen.getByTitle('Good answer')).toBeDisabled();
      expect(screen.getByTitle('Bad answer')).toBeDisabled();
    });
  });
});

describe('ChatMessage — MetaLine', () => {
  it('shows "N rows · Nms" when both row_count and execution_time_ms are present', () => {
    const msg = makeChatMessage({
      status: 'executed',
      results_json: makeQueryResults({ row_count: 5 }),
      execution_time_ms: 45,
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.getByText(/5 rows/)).toBeInTheDocument();
    expect(screen.getByText(/45ms/)).toBeInTheDocument();
  });

  it('MetaLine is hidden when no results and no execution_time_ms', () => {
    const msg = makeChatMessage({
      status: 'query_only',
      results_json: null,
      execution_time_ms: null,
    });
    render(<ChatMessageBubble message={msg} />);
    expect(screen.queryByText(/rows/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ms/)).not.toBeInTheDocument();
  });
});
