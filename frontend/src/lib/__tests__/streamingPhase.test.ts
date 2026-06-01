// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest'
import { getPhaseIndex, getProgressPercent, PHASE_COUNT } from '../streamingPhase'

describe('getPhaseIndex', () => {
  it('returns -1 for null', () => {
    expect(getPhaseIndex(null)).toBe(-1)
  })

  it('maps each backend status string to the correct phase', () => {
    expect(getPhaseIndex('Loading connection…')).toBe(0)
    expect(getPhaseIndex('Resolving schema…')).toBe(1)
    expect(getPhaseIndex('Generating query…')).toBe(2)
    expect(getPhaseIndex('Validating query…')).toBe(3)
    expect(getPhaseIndex('Executing query…')).toBe(4)
  })

  it('maps correction messages into the execution band (no backward jump)', () => {
    expect(getPhaseIndex('Correcting query…')).toBe(4)
    expect(getPhaseIndex('Re-executing corrected query…')).toBe(4)
  })

  it('returns -1 for unknown strings', () => {
    expect(getPhaseIndex('Something unexpected')).toBe(-1)
  })
})

describe('getProgressPercent', () => {
  it('returns 0 when nothing is known', () => {
    expect(getProgressPercent(null, false, 0)).toBe(0)
  })

  it('returns 60 if SQL arrived before first status', () => {
    expect(getProgressPercent(null, true, 0)).toBe(60)
  })

  it('produces monotonically increasing values across the main pipeline', () => {
    const statuses = [
      'Loading connection…',
      'Resolving schema…',
      'Generating query…',
      'Validating query…',
      'Executing query…',
    ]
    let prev = -1
    for (const s of statuses) {
      const pct = getProgressPercent(s, false, 0)
      expect(pct).toBeGreaterThan(prev)
      prev = pct
    }
  })

  it('never exceeds 100', () => {
    expect(getProgressPercent('Executing query…', true, 9999)).toBeLessThanOrEqual(100)
  })

  it('nudges above base percent when rows are flowing in execution phase', () => {
    const base = getProgressPercent('Executing query…', true, 0)
    const withRows = getProgressPercent('Executing query…', true, 5)
    expect(withRows).toBeGreaterThan(base)
  })

  it('all values fall within [0, 100]', () => {
    const cases: [string | null, boolean, number][] = [
      [null, false, 0],
      [null, true, 0],
      ['Loading connection…', false, 0],
      ['Executing query…', true, 200],
      ['Correcting query…', false, 0],
    ]
    for (const [s, sql, rows] of cases) {
      const pct = getProgressPercent(s, sql, rows)
      expect(pct).toBeGreaterThanOrEqual(0)
      expect(pct).toBeLessThanOrEqual(100)
    }
  })
})

describe('PHASE_COUNT', () => {
  it('equals 5 (connect, schema, generate, validate, execute)', () => {
    expect(PHASE_COUNT).toBe(5)
  })
})
