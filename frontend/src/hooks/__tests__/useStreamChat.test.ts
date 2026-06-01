// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

import { useAppStore } from '../../store/appStore'
import { useStreamChat } from '../useStreamChat'
import type { StreamCallbacks, ChatStreamPayload, SseDoneEvent } from '../../api/chatStream'

// ── Mock streamChat ───────────────────────────────────────────────────────────

vi.mock('../../api/chatStream', () => ({
  streamChat: vi.fn(),
}))

vi.mock('../../store/authStore', () => ({
  useAuthStore: {
    getState: () => ({ accessToken: 'test-token' }),
  },
}))

import { streamChat } from '../../api/chatStream'
const mockStreamChat = vi.mocked(streamChat)

const PAYLOAD: ChatStreamPayload = {
  connection_id: 'conn-1',
  session_id: null,
  message: 'how many orders?',
  provider: 'claude',
  options: { show_query: true, max_rows: 100, explain_results: true },
}

function makeDoneEvent(overrides?: Partial<SseDoneEvent>): SseDoneEvent {
  return {
    type: 'done',
    session_id: 'sess-1',
    message_id: 'msg-1',
    status: 'executed',
    execution_time_ms: 42,
    token_count: 100,
    input_tokens: 80,
    output_tokens: 20,
    cache_hit: false,
    ...overrides,
  }
}

const resetStore = () =>
  useAppStore.setState({
    activeConnectionId: null,
    activeSessionId: null,
    selectedProvider: '',
    schema: null,
    messages: [],
  })

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('useStreamChat', () => {
  beforeEach(() => {
    resetStore()
    mockStreamChat.mockReset()
  })

  it('accumulates rows across multiple row_batch events', async () => {
    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onEvent({ type: 'row_batch', columns: ['id'], column_types: ['integer'], rows: [[1], [2]], batch_index: 0, truncated: false })
      callbacks.onEvent({ type: 'row_batch', columns: ['id'], column_types: ['integer'], rows: [[3]], batch_index: 1, truncated: false })
      callbacks.onDone(makeDoneEvent())
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    // After done, streamingState is reset; check the promoted message
    const messages = useAppStore.getState().messages
    expect(messages).toHaveLength(1)
    const msg = messages[0]
    expect(msg.results_json?.rows).toEqual([[1], [2], [3]])
    expect(msg.results_json?.row_count).toBe(3)
  })

  it('promotes assistant message to store on done event', async () => {
    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onEvent({ type: 'sql', query: 'SELECT count(*) FROM orders', dialect: 'postgresql' })
      callbacks.onEvent({ type: 'explanation', text: 'There are 42 orders.' })
      callbacks.onDone(makeDoneEvent({ message_id: 'msg-abc', status: 'executed' }))
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    const messages = useAppStore.getState().messages
    expect(messages).toHaveLength(1)
    expect(messages[0].id).toBe('msg-abc')
    expect(messages[0].query_generated).toBe('SELECT count(*) FROM orders')
    expect(messages[0].content).toBe('There are 42 orders.')
    expect(messages[0].status).toBe('executed')
  })

  it('sets session_id in store when first message arrives', async () => {
    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onDone(makeDoneEvent({ session_id: 'new-session-42' }))
    })

    const { result } = renderHook(() => useStreamChat())
    expect(useAppStore.getState().activeSessionId).toBeNull()

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    expect(useAppStore.getState().activeSessionId).toBe('new-session-42')
  })

  it('preserves pre-existing messages (user bubble) when promoting a new session_id', async () => {
    const userMsg = { id: 'user-1', role: 'user' as const, content: 'hello', query_generated: null, query_dialect: null, results_json: null, execution_time_ms: null, token_count: null, input_tokens: null, output_tokens: null, status: 'executed' as const, cache_hit: false, feedback: null, error: null, created_at: new Date().toISOString() }
    useAppStore.setState({ messages: [userMsg] })

    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onDone(makeDoneEvent({ session_id: 'new-session-42', message_id: 'msg-2' }))
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    const messages = useAppStore.getState().messages
    expect(messages).toHaveLength(2)
    expect(messages[0].id).toBe('user-1')
    expect(messages[1].id).toBe('msg-2')
  })

  it('does not overwrite existing session_id in store', async () => {
    useAppStore.setState({ activeSessionId: 'existing-session' })

    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onDone(makeDoneEvent({ session_id: 'other-session' }))
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    expect(useAppStore.getState().activeSessionId).toBe('existing-session')
  })

  it('handles zero-row results — still produces a results_json object', async () => {
    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onDone(makeDoneEvent({ status: 'executed' }))
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    const msg = useAppStore.getState().messages[0]
    expect(msg.results_json).not.toBeNull()
    expect(msg.results_json?.rows).toEqual([])
    expect(msg.results_json?.row_count).toBe(0)
  })

  it('surfaces error event message in the promoted message', async () => {
    mockStreamChat.mockImplementation(async (_payload, callbacks: StreamCallbacks) => {
      callbacks.onEvent({ type: 'error', message: 'Query execution failed' })
      callbacks.onDone(makeDoneEvent({ status: 'error' }))
    })

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    const msg = useAppStore.getState().messages[0]
    expect(msg.error).toBe('Query execution failed')
  })

  it('cleans up silently when stop() aborts the stream', async () => {
    mockStreamChat.mockImplementation(async (_payload, _callbacks, signal) => {
      // Simulate a long-running stream that never resolves during the test
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      })
    })

    const { result } = renderHook(() => useStreamChat())

    // Start sending (do not await — it's in-flight)
    act(() => { result.current.send(PAYLOAD) })

    // Wait until isStreaming is true
    await waitFor(() => expect(result.current.streamingState.isStreaming).toBe(true))

    await act(async () => {
      result.current.stop()
    })

    await waitFor(() => expect(result.current.streamingState.isStreaming).toBe(false))

    // Stop should not have added an error message to the store
    expect(useAppStore.getState().messages).toHaveLength(0)
  })

  it('surfaces network errors as an error message in the store', async () => {
    mockStreamChat.mockRejectedValue(new Error('Failed to fetch'))

    const { result } = renderHook(() => useStreamChat())

    await act(async () => {
      await result.current.send(PAYLOAD)
    })

    const messages = useAppStore.getState().messages
    expect(messages).toHaveLength(1)
    expect(messages[0].status).toBe('error')
    expect(messages[0].error).toBe('Connection to server lost — please try again.')
  })
})
