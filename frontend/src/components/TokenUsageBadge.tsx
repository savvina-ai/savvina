// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { memo } from 'react';
import { ArrowDown, ArrowUp, Cpu } from 'lucide-react';
import { cn } from '@/lib/utils';

interface Props {
  total: number | null;
  input: number | null;
  output: number | null;
  /** Compact variant used in tight message footers (no icon, smaller text). */
  compact?: boolean;
  className?: string;
}

/**
 * Inline token usage badge.
 *
 * When input/output are available, renders a two-segment bar showing their relative
 * share plus separate counts. Otherwise falls back to the single total.
 */
function TokenUsageBadge({ total, input, output, compact = false, className }: Props) {
  const hasSplit = input != null && output != null && input + output > 0;
  const grandTotal = hasSplit ? input! + output! : total;

  if (grandTotal == null || grandTotal <= 0) return null;

  const inputPct = hasSplit ? Math.round((input! / grandTotal) * 100) : 0;
  const outputPct = hasSplit ? 100 - inputPct : 0;

  const title = hasSplit
    ? `Prompt: ${input!.toLocaleString()} tokens (${inputPct}%) · Completion: ${output!.toLocaleString()} tokens (${outputPct}%)`
    : `${grandTotal.toLocaleString()} tokens`;

  if (compact) {
    return (
      <span
        title={title}
        className={cn(
          'inline-flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground',
          className,
        )}
      >
        {hasSplit ? (
          <>
            <span className="inline-flex items-center gap-0.5">
              <ArrowUp className="h-2.5 w-2.5 text-muted-foreground/70" aria-hidden />
              {input!.toLocaleString()}
            </span>
            <span className="inline-flex items-center gap-0.5">
              <ArrowDown className="h-2.5 w-2.5 text-muted-foreground/70" aria-hidden />
              {output!.toLocaleString()}
            </span>
            <span
              aria-hidden
              className="inline-flex h-1.5 w-10 overflow-hidden rounded-full bg-muted"
            >
              <span
                className="h-full bg-primary/70"
                style={{ width: `${inputPct}%` }}
              />
              <span
                className="h-full bg-primary"
                style={{ width: `${outputPct}%` }}
              />
            </span>
          </>
        ) : (
          <span>{grandTotal.toLocaleString()} tokens</span>
        )}
      </span>
    );
  }

  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1.5 font-mono text-xs text-muted-foreground',
        className,
      )}
    >
      <Cpu className="h-3.5 w-3.5" aria-hidden />
      {hasSplit ? (
        <>
          <span>{grandTotal.toLocaleString()} tokens</span>
          <span
            aria-hidden
            className="inline-flex h-2 w-16 overflow-hidden rounded-full bg-muted"
          >
            <span className="h-full bg-primary/70" style={{ width: `${inputPct}%` }} />
            <span className="h-full bg-primary" style={{ width: `${outputPct}%` }} />
          </span>
          <span className="inline-flex items-center gap-0.5">
            <ArrowUp className="h-3 w-3 text-muted-foreground/70" aria-hidden />
            {input!.toLocaleString()}
          </span>
          <span className="inline-flex items-center gap-0.5">
            <ArrowDown className="h-3 w-3 text-muted-foreground/70" aria-hidden />
            {output!.toLocaleString()}
          </span>
        </>
      ) : (
        <span>{grandTotal.toLocaleString()} tokens</span>
      )}
    </span>
  );
}

export default memo(TokenUsageBadge);
