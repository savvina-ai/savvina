// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useState } from 'react';
import { copyToClipboard } from '@/lib/clipboard';
import type { AuditFinding, PrivilegeAudit, PrivilegeFindingCode } from '../types';

/** Native row-level security does not apply to the role. Shown under the RLS title only
 *  while `rls_in_use`; otherwise they are least-privilege findings like the rest. */
const TIER_1: ReadonlySet<PrivilegeFindingCode> = new Set([
  'superuser',
  'bypassrls',
  'owner_without_force',
]);

const RLS_TITLE = "Row-level security in the database does not apply to this connection's role.";
const LEAST_TITLE = 'The connection role has more privileges than Savvina needs.';

function list(items: string[]): string {
  return items.join(', ');
}

const DEDICATED = 'Create a dedicated read-only role and point the connection at it:';

function tableList(finding: AuditFinding): string {
  const more = finding.more_tables > 0 ? ` and ${finding.more_tables} more tables` : '';
  return `${list(finding.tables)}${more}`;
}

/** One sentence per code, in plain words. The fence is never mentioned. */
function sentence(finding: AuditFinding, mysql: boolean): string {
  switch (finding.code) {
    case 'superuser':
      return mysql
        ? `The account has ALL PRIVILEGES at the global level. ${DEDICATED}`
        : `The role is a superuser. PostgreSQL applies no privilege or row policy to it. ${DEDICATED}`;
    case 'super_privilege':
      return 'The account has SUPER. It grants no table access, but it allows server administration.';
    case 'bypassrls':
      return 'The role has BYPASSRLS. No row policy applies to it.';
    case 'owner_without_force': {
      const plural = finding.tables.length > 1;
      return `The role owns ${list(finding.tables)} and the ${plural ? 'tables do' : 'table does'} not force row-level security, so no policy applies to the owner.`;
    }
    case 'write_privileges':
      return `The role can write to ${finding.tables.length ? tableList(finding) : 'the database'}. Savvina only reads; a read-only role is the third layer after the validator and the read-only transaction.`;
    case 'reads_excluded_tables':
      return `The role can read ${tableList(finding)}, which this connection excludes. The LLM never sees them; the edit-query path can still read them.`;
    case 'sql_from_string': {
      const plural = finding.functions.length > 1;
      return `The role can execute ${list(finding.functions)}, which ${plural ? 'run' : 'runs'} SQL passed as a string. Savvina rejects ${plural ? 'them' : 'it'} by name; revoking ${plural ? 'them' : 'it'} closes the path for good.`;
    }
    default:
      return 'The role has a privilege Savvina does not need.';
  }
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      aria-label={`${copied ? 'Copied' : 'Copy'} the SQL for ${label}`}
      onClick={() => {
        void copyToClipboard(text).then((ok) => {
          if (ok) setCopied(true);
        });
      }}
      className="rounded-md border border-border px-2 py-0.5 text-xs text-foreground hover:bg-muted"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

function FindingBlock({ finding, mysql }: { finding: AuditFinding; mysql: boolean }) {
  return (
    <div className="mt-3">
      <p className="text-foreground">{sentence(finding, mysql)}</p>
      <div className="mt-1 flex items-start gap-2">
        <pre className="min-w-0 flex-1 overflow-x-auto rounded-md bg-background/60 px-2 py-1 text-xs">
          <code>{finding.remediation}</code>
        </pre>
        <CopyButton text={finding.remediation} label={finding.code.replace(/_/g, ' ')} />
      </div>
    </div>
  );
}

interface Props {
  audit: PrivilegeAudit | null;
  /** The connection's SQL dialect (`DataSourceInfo.query_dialect` or `source_type`); picks
   *  the MySQL wording of the superuser sentence. */
  dialect?: string;
  /** Reruns the audit (`POST /connections/{id}/test`). The button is shown only when given. */
  onRecheck?: () => void;
  rechecking?: boolean;
}

/** The connection role's privilege findings, each with the SQL that fixes it. A connection the
 *  audit never ran on gets a Check now button when a handler is given; nothing for a clean
 *  result (Privacy Settings shows the "checked" line for that). */
export default function PrivilegeAuditBanner({ audit, dialect, onRecheck, rechecking }: Props) {
  if (!audit) {
    if (!onRecheck) return null;
    return (
      <div aria-live="polite" className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm">
        <p className="text-muted-foreground">Privileges have not been checked yet.</p>
        <button
          type="button"
          onClick={onRecheck}
          disabled={rechecking}
          className="mt-2 rounded-md border border-border px-3 py-1 text-sm text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
        >
          {rechecking ? 'Checking…' : 'Check now'}
        </button>
      </div>
    );
  }
  const mysql = (dialect ?? '').toLowerCase() === 'mysql';
  const recheck = onRecheck && (
    <button
      type="button"
      onClick={onRecheck}
      disabled={rechecking}
      className="mt-2 rounded-md border border-border px-3 py-1 text-sm text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
    >
      {rechecking ? 'Checking…' : 'Check again'}
    </button>
  );

  if (audit.status === 'unavailable') {
    const error = (audit.error ?? '').replace(/[.\s]+$/, '');
    return (
      <div aria-live="polite" className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm">
        <p className="text-muted-foreground">
          {error ? `Privileges could not be checked: ${error}.` : 'Privileges could not be checked.'}
        </p>
        {recheck}
      </div>
    );
  }
  if (audit.findings.length === 0) return null;

  const tier1 = audit.rls_in_use ? audit.findings.filter((f) => TIER_1.has(f.code)) : [];
  const rest = audit.findings.filter((f) => !tier1.includes(f));
  // Without RLS in use the TIER_1 codes still come first.
  const tier2 = [...rest.filter((f) => TIER_1.has(f.code)), ...rest.filter((f) => !TIER_1.has(f.code))];

  return (
    <div aria-live="polite" className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm">
      {tier1.length > 0 ? (
        <>
          <p className="font-medium text-foreground">{RLS_TITLE}</p>
          <p className="text-muted-foreground">Savvina&apos;s own row filters still apply.</p>
          {tier1.map((f) => (
            <FindingBlock key={f.code} finding={f} mysql={mysql} />
          ))}
          {tier2.length > 0 && <p className="mt-3 text-muted-foreground">Also:</p>}
        </>
      ) : (
        <p className="font-medium text-foreground">{LEAST_TITLE}</p>
      )}
      {tier2.map((f) => (
        <FindingBlock key={f.code} finding={f} mysql={mysql} />
      ))}
      {recheck}
    </div>
  );
}
