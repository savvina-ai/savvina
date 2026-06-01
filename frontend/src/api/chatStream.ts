// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * SSE client for POST /api/v1/chat.
 *
 * Uses Fetch + ReadableStream — NOT EventSource (which only supports GET)
 * and NOT axios (which buffers the full response before resolving).
 */

export interface SseStatusEvent {
  type: 'status'
  message: string
}

export interface SseSqlEvent {
  type: 'sql'
  query: string
  dialect: string
}

export interface SseExplanationEvent {
  type: 'explanation'
  text: string
}

export interface SseRowBatchEvent {
  type: 'row_batch'
  rows: unknown[][]
  columns: string[]
  column_types: string[]
  batch_index: number
  truncated: boolean
}

export interface SseErrorEvent {
  type: 'error'
  message: string
}

export interface SseDoneEvent {
  type: 'done'
  session_id: string
  message_id: string
  execution_time_ms: number | null
  cache_hit: boolean
  status: string
  token_count: number | null
  input_tokens: number | null
  output_tokens: number | null
  warning?: string | null
}

export type SseEvent =
  | SseStatusEvent
  | SseSqlEvent
  | SseExplanationEvent
  | SseRowBatchEvent
  | SseErrorEvent
  | SseDoneEvent

export interface ChatStreamPayload {
  connection_id: string
  session_id: string | null
  message: string
  provider: string
  options: {
    show_query: boolean
    max_rows: number
    explain_results: boolean
    bypass_cache?: boolean
    force_refresh?: boolean
  }
}

export interface StreamCallbacks {
  onEvent: (event: SseEvent) => void
  onDone: (event: SseDoneEvent) => void
}

const API_BASE_URL =
  import.meta.env.VITE_API_URL ||
  (typeof window !== 'undefined' ? window.location.origin : '')

/**
 * Open a streaming POST to /api/v1/chat and fire callbacks for each SSE event.
 * Resolves when the `done` event is received or the signal is aborted.
 * Rejects on network error or non-2xx response.
 */
export async function streamChat(
  payload: ChatStreamPayload,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
  getAccessToken: () => string | null,
): Promise<void> {
  const token = getAccessToken()

  const response = await fetch(`${API_BASE_URL}/api/v1/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  })

  if (!response.ok) {
    throw new Error(`Chat stream request failed: ${response.status} ${response.statusText}`)
  }

  if (!response.body) {
    throw new Error('Response body is null — SSE stream unavailable')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // SSE messages are separated by '\n\n'
      const parts = buffer.split('\n\n')
      // Last element may be an incomplete chunk — keep it in the buffer
      buffer = parts.pop() ?? ''

      for (const part of parts) {
        const line = part.trim()
        if (!line || line.startsWith(':')) {
          // Empty line or SSE comment (heartbeat) — skip
          continue
        }
        if (line.startsWith('data: ')) {
          const jsonStr = line.slice(6)
          let event: SseEvent
          try {
            event = JSON.parse(jsonStr) as SseEvent
          } catch {
            // Malformed JSON — ignore this chunk
            continue
          }
          callbacks.onEvent(event)
          if (event.type === 'done') {
            callbacks.onDone(event)
            return
          }
        }
      }
    }

    // Drain any remaining data left in the buffer when the stream closes
    // (e.g. final frame not terminated by \n\n before EOF).
    const remaining = buffer.trim()
    if (remaining && remaining.startsWith('data: ')) {
      const jsonStr = remaining.slice(6)
      try {
        const event = JSON.parse(jsonStr) as SseEvent
        callbacks.onEvent(event)
        if (event.type === 'done') {
          callbacks.onDone(event)
          return
        }
      } catch { console.debug('SSE: malformed final frame, ignoring', jsonStr) }
    }
  } finally {
    reader.cancel().catch(() => undefined)
  }
}
