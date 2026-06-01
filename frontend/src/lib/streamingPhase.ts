// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * Maps SSE status text from the backend streaming pipeline into a monotonic
 * phase index and progress percentage so the UI can render a determinate bar.
 */

const PHASE_KEYWORDS: [string, number][] = [
  ['loading connection', 0],
  ['resolving schema', 1],
  ['generating query', 2],
  ['validating', 3],
  ['executing', 4],
  ['correcting', 4],
  ['re-executing', 4],
]

export const PHASE_COUNT = 5

export const PHASE_LABELS: readonly string[] = [
  'Connecting',
  'Reading schema',
  'Generating query',
  'Validating',
  'Running query',
]

export function getPhaseIndex(statusText: string | null): number {
  if (statusText == null) return -1
  const lower = statusText.toLowerCase()
  for (let i = PHASE_KEYWORDS.length - 1; i >= 0; i--) {
    if (lower.includes(PHASE_KEYWORDS[i][0])) return PHASE_KEYWORDS[i][1]
  }
  return -1
}

export function getProgressPercent(
  statusText: string | null,
  hasSql: boolean,
  rowCount: number,
): number {
  const idx = getPhaseIndex(statusText)

  if (idx >= 0) {
    // Reserve the last 10% for row-streaming feedback in the execution phase
    const base = idx < PHASE_COUNT - 1
      ? ((idx + 1) / PHASE_COUNT) * 100
      : 90

    if (idx === PHASE_COUNT - 1 && rowCount > 0) {
      return Math.min(base + Math.min(rowCount, 10), 100)
    }
    return Math.min(base, 100)
  }

  // Before the first status event arrives: show a sliver if SQL appeared already
  if (hasSql) return 60
  return 0
}
