// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { streamChat } from '../chatStream'
import type { SseDoneEvent, SseEvent, StreamCallbacks, ChatStreamPayload } from '../chatStream'

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeStream(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })
}

function makeFetchOk(body: ReadableStream<Uint8Array>) {
  return Promise.resolve(
    new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
  )
}

function sseFrame(payload: object): string {
  return `data: ${JSON.stringify(payload)}\n\n`
}

const DONE_EVENT: SseDoneEvent = {
  type: 'done',
  session_id: 'sess-1',
  message_id: 'msg-1',
  execution_time_ms: 10,
  cache_hit: false,
  status: 'executed',
  token_count: 10,
  input_tokens: 8,
  output_tokens: 2,
}

const PAYLOAD: ChatStreamPayload = {
  connection_id: 'conn-1',
  session_id: null,
  message: 'count orders',
  provider: 'claude',
  options: { show_query: true, max_rows: 100, explain_results: true },
}

function makeCallbacks() {
  const events: SseEvent[] = []
  let doneEvent: SseDoneEvent | null = null
  const callbacks: StreamCallbacks = {
    onEvent: vi.fn((e) => events.push(e)),
    onDone: vi.fn((e) => { doneEvent = e }),
  }
  return { callbacks, events, getDone: () => doneEvent }
}

afterEach(() => vi.restoreAllMocks())

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('streamChat', () => {
  it('fires onEvent and onDone for a complete SSE sequence', async () => {
    const statusFrame = sseFrame({ type: 'status', message: 'Generating…' })
    const doneFrame = sseFrame(DONE_EVENT)
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream(statusFrame + doneFrame)))

    const { callbacks, events, getDone } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    expect(events).toHaveLength(2)
    expect(events[0].type).toBe('status')
    expect(getDone()).not.toBeNull()
    expect(getDone()?.session_id).toBe('sess-1')
  })

  it('fires onDone immediately and returns after done event', async () => {
    const sqlFrame = sseFrame({ type: 'sql', query: 'SELECT 1', dialect: 'postgresql' })
    const doneFrame = sseFrame(DONE_EVENT)
    // Extra frame after done that should never be processed
    const extraFrame = sseFrame({ type: 'status', message: 'SHOULD NOT APPEAR' })
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream(sqlFrame + doneFrame + extraFrame)))

    const { callbacks, events } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    // done fires once, extra frame is ignored
    expect(callbacks.onDone).toHaveBeenCalledOnce()
    const types = events.map((e) => e.type)
    expect(types).not.toContain('SHOULD NOT APPEAR')
  })

  it('skips SSE comment/heartbeat lines starting with ":"', async () => {
    const heartbeat = ': heartbeat\n\n'
    const doneFrame = sseFrame(DONE_EVENT)
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream(heartbeat + doneFrame)))

    const { callbacks, events } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    expect(events).toHaveLength(1)
    expect(events[0].type).toBe('done')
  })

  it('skips empty lines between frames', async () => {
    const doneFrame = sseFrame(DONE_EVENT)
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream('\n\n\n\n' + doneFrame)))

    const { callbacks, events } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    expect(events).toHaveLength(1)
    expect(events[0].type).toBe('done')
  })

  it('handles frames split across multiple stream chunks', async () => {
    const fullFrame = sseFrame(DONE_EVENT)
    const mid = Math.floor(fullFrame.length / 2)
    vi.stubGlobal('fetch', () =>
      makeFetchOk(makeStream(fullFrame.slice(0, mid), fullFrame.slice(mid))),
    )

    const { callbacks } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    expect(callbacks.onDone).toHaveBeenCalledOnce()
  })

  it('drains incomplete buffer on EOF — done frame without trailing \\n\\n', async () => {
    const rawFrame = `data: ${JSON.stringify(DONE_EVENT)}` // no trailing \n\n
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream(rawFrame)))

    const { callbacks } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    expect(callbacks.onDone).toHaveBeenCalledOnce()
  })

  it('throws on non-2xx HTTP status', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve(new Response('Unauthorized', { status: 401, statusText: 'Unauthorized' })),
    )

    const { callbacks } = makeCallbacks()
    await expect(
      streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok'),
    ).rejects.toThrow('Chat stream request failed: 401')
  })

  it('throws when response body is null', async () => {
    // Response.body is a read-only getter — use a plain object mock instead
    vi.stubGlobal('fetch', () =>
      Promise.resolve({ ok: true, body: null } as unknown as Response),
    )

    const { callbacks } = makeCallbacks()
    await expect(
      streamChat(PAYLOAD, callbacks, new AbortController().signal, () => null),
    ).rejects.toThrow('Response body is null')
  })

  it('injects Bearer token in Authorization header', async () => {
    const fetchMock = vi.fn<typeof fetch>(() => makeFetchOk(makeStream(sseFrame(DONE_EVENT))))
    vi.stubGlobal('fetch', fetchMock)

    const { callbacks } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'my-secret-token')

    const [, init] = fetchMock.mock.calls[0]
    expect((init as RequestInit).headers).toMatchObject({
      Authorization: 'Bearer my-secret-token',
    })
  })

  it('omits Authorization header when token is null', async () => {
    const fetchMock = vi.fn<typeof fetch>(() => makeFetchOk(makeStream(sseFrame(DONE_EVENT))))
    vi.stubGlobal('fetch', fetchMock)

    const { callbacks } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => null)

    const [, init] = fetchMock.mock.calls[0]
    expect((init as RequestInit).headers).not.toHaveProperty('Authorization')
  })

  it('skips malformed JSON frames without crashing', async () => {
    const bad = 'data: {not valid json}\n\n'
    const doneFrame = sseFrame(DONE_EVENT)
    vi.stubGlobal('fetch', () => makeFetchOk(makeStream(bad + doneFrame)))

    const { callbacks, events } = makeCallbacks()
    await streamChat(PAYLOAD, callbacks, new AbortController().signal, () => 'tok')

    // bad frame skipped, done still fires
    expect(events.some((e) => e.type === 'done')).toBe(true)
    expect(callbacks.onDone).toHaveBeenCalledOnce()
  })
})
