// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { cn } from '@/lib/utils'
import { getProgressPercent, PHASE_LABELS, getPhaseIndex } from '@/lib/streamingPhase'

interface Props {
  statusText: string | null
  hasSql: boolean
  rowCount: number
  className?: string
}

export default function StreamingProgress({ statusText, hasSql, rowCount, className }: Props) {
  const percent = getProgressPercent(statusText, hasSql, rowCount)
  const phaseIdx = getPhaseIndex(statusText)
  const indeterminate = percent === 0

  const label = statusText ?? 'Starting…'
  const subtitle =
    rowCount > 0
      ? `${rowCount} row${rowCount !== 1 ? 's' : ''} received`
      : phaseIdx >= 0
        ? PHASE_LABELS[phaseIdx]
        : null

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {/* Progress track */}
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted">
        {indeterminate ? (
          <div className="absolute inset-y-0 left-0 w-1/4 rounded-full bg-primary animate-indeterminate" />
        ) : (
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-500 ease-out"
            style={{ width: `${percent}%` }}
          />
        )}
      </div>

      {/* Labels */}
      <div className="flex items-center gap-2 text-xs">
        <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary" />
        <span className="text-foreground">{label}</span>
        {subtitle && (
          <span className="text-muted-foreground">· {subtitle}</span>
        )}
      </div>
    </div>
  )
}
