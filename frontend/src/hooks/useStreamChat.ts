// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useCallback, useRef, useState } from 'react'

import { useAuthStore } from '../store/authStore'
import { useAppStore } from '../store/appStore'
import {
  streamChat,
  type ChatStreamPayload,
  type SseDoneEvent,
  type SseEvent,
} from '../api/chatStream'
import type { MessageStatus, QueryResults, RowValue } from '../types'

export interface StreamingMessageState {
  /** Latest status event message, e.g. "Generating query…" */
  statusText: string | null
  /** SQL query from the `sql` event */
  sql: string | null
  /** SQL dialect from the `sql` event */
  dialect: string | null
  /** LLM explanation from the `explanation` event */
  explanation: string | null
  /** Column names accumulated from row_batch events */
  columns: string[]
  /** Column type names accumulated from row_batch events */
  columnTypes: string[]
  /** All rows accumulated from row_batch events */
  rows: RowValue[][]
  /** Running row count */
  rowCount: number
  /** True if the backend hit max_rows before the cursor was exhausted */
  truncated: boolean
  /** True while the stream is open */
  isStreaming: boolean
  /** Error message from the `error` event */
  error: string | null
}

const INITIAL_STATE: StreamingMessageState = {
  statusText: null,
  sql: null,
  dialect: null,
  explanation: null,
  columns: [],
  columnTypes: [],
  rows: [],
  rowCount: 0,
  truncated: false,
  isStreaming: false,
  error: null,
}

/**
 * Hook that manages local streaming state for a single in-flight SSE chat request.
 *
 * The in-progress partial message is stored in local state (not Zustand) so that
 * partial data is never persisted. When the `done` event arrives, the finalized
 * message is promoted to the app store via addMessage().
 */
export function useStreamChat() {
  const [streamingState, setStreamingState] =
    useState<StreamingMessageState>(INITIAL_STATE)

  // Mirror of latest state for use inside callbacks (avoids stale closure)
  const stateRef = useRef<StreamingMessageState>(INITIAL_STATE)
  const abortRef = useRef<AbortController | null>(null)
  const isSendingRef = useRef(false)

  const addMessage = useAppStore((s) => s.addMessage)
  const promoteSession = useAppStore((s) => s.promoteSession)

  const updateState = useCallback((updater: (prev: StreamingMessageState) => StreamingMessageState) => {
    // Update the ref synchronously so subsequent callbacks (e.g. handleDone) see the
    // latest state even when multiple SSE events arrive in the same chunk and React
    // hasn't yet flushed its batched setStreamingState calls.
    const next = updater(stateRef.current)
    stateRef.current = next
    setStreamingState(next)
  }, [])

  const handleEvent = useCallback((event: SseEvent) => {
    switch (event.type) {
      case 'status':
        updateState((prev) => ({ ...prev, statusText: event.message }))
        break
      case 'sql':
        updateState((prev) => ({ ...prev, sql: event.query, dialect: event.dialect }))
        break
      case 'explanation':
        updateState((prev) => ({ ...prev, explanation: event.text }))
        break
      case 'row_batch':
        updateState((prev) => ({
          ...prev,
          columns: event.columns,
          columnTypes: event.column_types,
          rows: [...prev.rows, ...(event.rows as RowValue[][])],
          rowCount: prev.rowCount + event.rows.length,
          truncated: event.truncated,
        }))
        break
      case 'error':
        updateState((prev) => ({ ...prev, error: event.message }))
        break
      default:
        break
    }
  }, [updateState])

  const handleDone = useCallback((event: SseDoneEvent) => {
    const final = stateRef.current

    // Promote session_id to the store if this is the first message
    if (!useAppStore.getState().activeSessionId && event.session_id) {
      promoteSession(event.session_id)
    }

    // Assemble the full results object from accumulated row batches.
    // Use event.status to decide — not columns.length — so that a query returning
    // 0 rows still gets a results object (matching process_message behaviour).
    const executedStatuses = ['executed', 'cached']
    const results: QueryResults | null = executedStatuses.includes(event.status)
      ? {
          columns: final.columns,
          column_types: final.columnTypes,
          rows: final.rows,
          row_count: final.rowCount,
          truncated: final.truncated,
          bytes_scanned: null,
        }
      : null

    addMessage({
      id: event.message_id || `stream-${Date.now()}`,
      role: 'assistant',
      content: final.explanation ?? '',
      query_generated: final.sql,
      query_dialect: final.dialect,
      results_json: results,
      execution_time_ms: event.execution_time_ms,
      token_count: event.token_count ?? null,
      input_tokens: event.input_tokens ?? null,
      output_tokens: event.output_tokens ?? null,
      status: event.status as MessageStatus,
      cache_hit: event.cache_hit,
      feedback: null,
      error: final.error,
      warning: event.warning ?? null,
      created_at: new Date().toISOString(),
    })

    stateRef.current = INITIAL_STATE
    setStreamingState(INITIAL_STATE)
    isSendingRef.current = false

    // Abort the stream reader so streamChat resolves promptly even if
    // the HTTP connection stays open (e.g. Nginx proxy buffering delay).
    abortRef.current?.abort()
    abortRef.current = null
  }, [addMessage, promoteSession])

  const send = useCallback(async (payload: ChatStreamPayload) => {
    if (isSendingRef.current) return

    const controller = new AbortController()
    abortRef.current = controller
    isSendingRef.current = true

    const next = { ...INITIAL_STATE, isStreaming: true }
    stateRef.current = next
    setStreamingState(next)

    const getToken = () => useAuthStore.getState().accessToken

    try {
      await streamChat(
        payload,
        { onEvent: handleEvent, onDone: handleDone },
        controller.signal,
        getToken,
      )

      // Safety net: if streamChat resolved but handleDone never fired,
      // the stream closed prematurely — surface any accumulated error or
      // a generic message and reset.
      if (isSendingRef.current) {
        const final = stateRef.current
        addMessage({
          id: `err-${Date.now()}`,
          role: 'assistant',
          content: '',
          query_generated: final.sql,
          query_dialect: final.dialect,
          results_json: null,
          execution_time_ms: null,
          token_count: null,
          input_tokens: null,
          output_tokens: null,
          status: 'error' as MessageStatus,
          cache_hit: false,
          feedback: null,
          error: final.error || 'Connection to server lost — please try again.',
          warning: null,
          created_at: new Date().toISOString(),
        })
        stateRef.current = INITIAL_STATE
        setStreamingState(INITIAL_STATE)
        isSendingRef.current = false
        abortRef.current = null
      }
    } catch (err) {
      if (controller.signal.aborted) {
        // User cancelled — clean up silently
        stateRef.current = INITIAL_STATE
        setStreamingState(INITIAL_STATE)
        isSendingRef.current = false
        abortRef.current = null
        return
      }

      // Network or non-2xx error — surface as error message in the store.
      // Raw fetch errors ("Failed to fetch", "network error", etc.) vary by browser
      // and mean nothing to users — normalize them to a stable message.
      const raw = err instanceof Error ? err.message : String(err)
      const isHttpError = raw.startsWith('Chat stream request failed:')
      const msg = isHttpError ? raw : 'Connection to server lost — please try again.'
      addMessage({
        id: `err-${Date.now()}`,
        role: 'assistant',
        content: '',
        query_generated: null,
        query_dialect: null,
        results_json: null,
        execution_time_ms: null,
        token_count: null,
        input_tokens: null,
        output_tokens: null,
        status: 'error',
        cache_hit: false,
        feedback: null,
        error: msg,
        warning: null,
        created_at: new Date().toISOString(),
      })
      stateRef.current = INITIAL_STATE
      setStreamingState(INITIAL_STATE)
      isSendingRef.current = false
      abortRef.current = null
    }
  }, [handleEvent, handleDone, addMessage])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { streamingState, send, stop }
}
