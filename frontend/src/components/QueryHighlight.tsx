// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

interface Props {
  query: string;
  dialect?: string | null;
  /** Set false when embedding inside a card that already provides its own header. */
  showHeader?: boolean;
}

const KEYWORD_RE =
  /\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ON|AND|OR|NOT|IN|IS|AS|DISTINCT|HAVING|LIMIT|OFFSET|UNION|ALL|INTERSECT|EXCEPT|WITH|CASE|WHEN|THEN|ELSE|END|CREATE|ALTER|DROP|TABLE|INDEX|VIEW|SET|VALUES|INTO|RETURNING|ASC|DESC|BY|GROUP|ORDER|EXISTS|BETWEEN|LIKE|NULL|NULLIF|COALESCE|COUNT|SUM|AVG|MAX|MIN|CAST|OVER|PARTITION|ROWS|RANGE|UNBOUNDED|PRECEDING|FOLLOWING|CURRENT|ROW)\b/gi;

function highlightSQL(sql: string) {
  const parts = sql.split(KEYWORD_RE);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <span key={i} className="sql-keyword">
            {part.toUpperCase()}
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

export default function QueryHighlight({ query, dialect, showHeader = true }: Props) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(query);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const dialectLabel = (dialect ?? 'SQL').toUpperCase();

  const codeBlock = (
    <div style={{ background: 'hsl(var(--sql-bg))' }}>
      <pre
        className="overflow-x-auto p-3 font-mono text-xs leading-relaxed"
        style={{ color: 'hsl(var(--foreground))', margin: 0 }}
      >
        <code>{highlightSQL(query)}</code>
      </pre>
    </div>
  );

  if (!showHeader) {
    return codeBlock;
  }

  return (
    <div
      className="overflow-hidden rounded-lg border"
      style={{ background: 'hsl(var(--sql-bg))', borderColor: 'hsl(var(--border))' }}
    >
      {/* Header bar */}
      <div
        className="flex items-center justify-between border-b px-3 py-2"
        style={{ background: 'hsl(var(--muted))', borderColor: 'hsl(var(--border))' }}
      >
        <div className="flex items-center gap-2">
          {/* Traffic lights */}
          <div className="flex items-center gap-1.5">
            <div className="h-2.5 w-2.5 rounded-full" style={{ background: '#FC5F57' }} />
            <div className="h-2.5 w-2.5 rounded-full" style={{ background: '#FDBC40' }} />
            <div className="h-2.5 w-2.5 rounded-full" style={{ background: '#33C748' }} />
          </div>
          <span
            className="ml-1 font-mono text-[10px] font-semibold uppercase tracking-widest"
            style={{ color: 'hsl(var(--muted-foreground))' }}
          >
            QUERY.SQL
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span
            className="font-mono text-[10px] font-semibold uppercase tracking-wide"
            style={{ color: 'hsl(var(--muted-foreground))' }}
          >
            {dialectLabel}
          </span>
          <button
            onClick={copy}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px] transition-opacity hover:opacity-70"
            style={{ color: 'hsl(var(--muted-foreground))' }}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>

      {codeBlock}
    </div>
  );
}
