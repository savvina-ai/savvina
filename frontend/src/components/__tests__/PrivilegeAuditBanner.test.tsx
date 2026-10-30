// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PrivilegeAuditBanner from '../PrivilegeAuditBanner';
import type { AuditFinding, PrivilegeAudit } from '../../types';

const RLS_TITLE = "Row-level security in the database does not apply to this connection's role.";
const LEAST_TITLE = 'The connection role has more privileges than Savvina needs.';

function finding(overrides: Partial<AuditFinding> & { code: AuditFinding['code'] }): AuditFinding {
  return { tables: [], functions: [], more_tables: 0, remediation: '', ...overrides };
}

function audit(overrides?: Partial<PrivilegeAudit>): PrivilegeAudit {
  return {
    checked_at: '2026-10-29T10:00:00+00:00',
    role: 'savvina',
    status: 'ok',
    error: null,
    findings: [],
    rls_in_use: false,
    rls_effective: {},
    ...overrides,
  };
}

const BYPASSRLS = finding({
  code: 'bypassrls',
  remediation: 'ALTER ROLE "savvina" NOBYPASSRLS;',
});
const SUPERUSER = finding({ code: 'superuser', remediation: 'ALTER ROLE "savvina" NOSUPERUSER;' });
const WRITES = finding({
  code: 'write_privileges',
  tables: ['public.orders', 'public.items'],
  remediation: 'REVOKE INSERT, UPDATE, DELETE ON "public"."orders" FROM "savvina";',
});

// The unit tests run in Node; the app's tsconfig has no node types.
const nodeProcess = (
  globalThis as unknown as {
    process: {
      on: (event: string, fn: () => void) => void;
      off: (event: string, fn: () => void) => void;
    };
  }
).process;

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
const hadExecCommand = 'execCommand' in document;

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true });
}

describe('PrivilegeAuditBanner', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor);
    else delete (navigator as unknown as Record<string, unknown>).clipboard;
    if (!hadExecCommand) delete (document as unknown as Record<string, unknown>).execCommand;
  });

  it('renders nothing for null without a recheck handler', () => {
    const { container } = render(<PrivilegeAuditBanner audit={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('null with a recheck handler: the not-checked line and Check now', async () => {
    const onRecheck = vi.fn();
    render(<PrivilegeAuditBanner audit={null} onRecheck={onRecheck} />);
    expect(screen.getByText('Privileges have not been checked yet.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });

  it('super_privilege and the MySQL superuser copy', () => {
    render(
      <PrivilegeAuditBanner
        audit={audit({
          findings: [
            finding({ code: 'superuser', remediation: 'CREATE USER ...' }),
            finding({ code: 'super_privilege', remediation: 'REVOKE SUPER ON *.* FROM `u`@`%`;' }),
          ],
        })}
        dialect="MySQL"
      />,
    );
    expect(screen.getByText(/The account has ALL PRIVILEGES at the global level\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Create a dedicated read-only role and point the connection at it:/),
    ).toBeInTheDocument();
    expect(screen.getByText(/The account has SUPER\. It grants no table access/)).toBeInTheDocument();
  });

  it('write_privileges says and N more', () => {
    render(
      <PrivilegeAuditBanner
        audit={audit({
          findings: [finding({ code: 'write_privileges', tables: ['a.b'], more_tables: 3 })],
        })}
      />,
    );
    expect(screen.getByText(/The role can write to a\.b and 3 more tables\./)).toBeInTheDocument();
  });

  it('plural forms follow the count', () => {
    render(
      <PrivilegeAuditBanner
        audit={audit({
          findings: [
            finding({ code: 'owner_without_force', tables: ['a.b', 'c.d'] }),
            finding({ code: 'sql_from_string', functions: ['public.dblink(text)'] }),
          ],
          rls_in_use: true,
        })}
      />,
    );
    expect(screen.getByText(/owns a\.b, c\.d and the tables do not force/)).toBeInTheDocument();
    expect(screen.getByText(/revoking it closes the path/)).toBeInTheDocument();
  });

  it('renders nothing for ok without findings', () => {
    const { container } = render(<PrivilegeAuditBanner audit={audit()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('tier 1 only: the RLS title, the reassurance and the finding', () => {
    render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER], rls_in_use: true })} />);
    expect(screen.getByText(RLS_TITLE)).toBeInTheDocument();
    expect(screen.getByText("Savvina's own row filters still apply.")).toBeInTheDocument();
    expect(
      screen.getByText(
        'The role is a superuser. PostgreSQL applies no privilege or row policy to it. Create a dedicated read-only role and point the connection at it:',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('ALTER ROLE "savvina" NOSUPERUSER;')).toBeInTheDocument();
    expect(screen.queryByText(LEAST_TITLE)).not.toBeInTheDocument();
    expect(screen.queryByText('Also:')).not.toBeInTheDocument();
  });

  it('tier 2 only: the least-privilege title and the tables named', () => {
    render(<PrivilegeAuditBanner audit={audit({ findings: [WRITES] })} />);
    expect(screen.getByText(LEAST_TITLE)).toBeInTheDocument();
    expect(screen.getByText(/The role can write to public\.orders, public\.items\./)).toBeInTheDocument();
    expect(screen.queryByText(RLS_TITLE)).not.toBeInTheDocument();
  });

  it('both tiers: the RLS title, then Also:, then the tier 2 blocks', () => {
    render(
      <PrivilegeAuditBanner audit={audit({ findings: [WRITES, BYPASSRLS], rls_in_use: true })} />,
    );
    const title = screen.getByText(RLS_TITLE);
    const also = screen.getByText('Also:');
    const bypass = screen.getByText(/The role has BYPASSRLS/);
    const writes = screen.getByText(/The role can write to/);
    expect(title.compareDocumentPosition(bypass) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bypass.compareDocumentPosition(also) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(also.compareDocumentPosition(writes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('a superuser without RLS in use is a least-privilege finding', () => {
    render(<PrivilegeAuditBanner audit={audit({ findings: [WRITES, SUPERUSER], rls_in_use: false })} />);
    expect(screen.getByText(LEAST_TITLE)).toBeInTheDocument();
    expect(screen.queryByText(RLS_TITLE)).not.toBeInTheDocument();
    expect(screen.queryByText('Also:')).not.toBeInTheDocument();
    // TIER_1 codes come first.
    const superuser = screen.getByText(/The role is a superuser/);
    const writes = screen.getByText(/The role can write to/);
    expect(superuser.compareDocumentPosition(writes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('mysql superuser copy', () => {
    render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} dialect="MySQL" />);
    expect(
      screen.getByText(
        'The account has ALL PRIVILEGES at the global level. Create a dedicated read-only role and point the connection at it:',
      ),
    ).toBeInTheDocument();
  });

  it('the other sentences', () => {
    render(
      <PrivilegeAuditBanner
        audit={audit({
          findings: [
            finding({ code: 'bypassrls', remediation: 'x' }),
            finding({ code: 'owner_without_force', tables: ['public.orders'], remediation: 'x' }),
            finding({ code: 'reads_excluded_tables', tables: ['public.audit_log'], remediation: 'x' }),
            finding({ code: 'sql_from_string', functions: ['public.dblink(text)'], remediation: 'x' }),
          ],
        })}
      />,
    );
    expect(screen.getByText('The role has BYPASSRLS. No row policy applies to it.')).toBeInTheDocument();
    expect(
      screen.getByText(
        'The role owns public.orders and the table does not force row-level security, so no policy applies to the owner.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'The role can read public.audit_log, which this connection excludes. The LLM never sees them; the edit-query path can still read them.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'The role can execute public.dblink(text), which runs SQL passed as a string. Savvina rejects it by name; revoking it closes the path for good.',
      ),
    ).toBeInTheDocument();
  });

  it('the fence is never mentioned', () => {
    const { container } = render(
      <PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER, WRITES], rls_in_use: true })} />,
    );
    expect(container.textContent).not.toMatch(/fence/i);
  });

  it('write_privileges without tables names the database', () => {
    render(
      <PrivilegeAuditBanner
        audit={audit({ findings: [finding({ code: 'write_privileges', remediation: 'x' })] })}
      />,
    );
    expect(screen.getByText(/The role can write to the database\./)).toBeInTheDocument();
  });

  it('Copy writes the remediation to the clipboard', async () => {
    // fireEvent, not userEvent: user-event v14 installs its own clipboard stub for the
    // duration of an interaction, which would hide this mock.
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy the SQL for superuser' }));
    expect(writeText).toHaveBeenCalledWith('ALTER ROLE "savvina" NOSUPERUSER;');
    expect(await screen.findByRole('button', { name: 'Copied the SQL for superuser' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Copied/ })).toHaveTextContent('Copied');
  });

  it('a multi-line remediation is one code block and Copy writes every line', async () => {
    const remediation = 'REVOKE INSERT ON "public"."a" FROM "savvina";\nREVOKE INSERT ON "public"."b" FROM "savvina";';
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });
    const { container } = render(
      <PrivilegeAuditBanner audit={audit({ findings: [{ ...WRITES, remediation }] })} />,
    );
    const code = container.querySelectorAll('code');
    expect(code).toHaveLength(1);
    expect(code[0].textContent).toBe(remediation);
    fireEvent.click(screen.getByRole('button', { name: /^Copy/ }));
    expect(writeText).toHaveBeenCalledWith(remediation);
    await screen.findByRole('button', { name: /^Copied/ });
  });

  it('without navigator.clipboard Copy falls back to execCommand and does not throw', async () => {
    setClipboard(undefined);
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });
    render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} />);
    fireEvent.click(screen.getByRole('button', { name: /^Copy/ }));
    expect(await screen.findByRole('button', { name: /^Copied/ })).toBeInTheDocument();
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('a failed fallback copy does not show Copied', async () => {
    setClipboard(undefined);
    Object.defineProperty(document, 'execCommand', {
      value: vi.fn().mockReturnValue(false),
      configurable: true,
    });
    render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} />);
    fireEvent.click(screen.getByRole('button', { name: /^Copy/ }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByRole('button', { name: /^Copy/ })).toHaveTextContent('Copy');
    expect(screen.queryByText('Copied')).not.toBeInTheDocument();
  });

  it('a rejecting writeText is not an unhandled rejection and shows no Copied', async () => {
    const unhandled = vi.fn();
    nodeProcess.on('unhandledRejection', unhandled);
    try {
      setClipboard({ writeText: vi.fn().mockRejectedValue(new Error('denied')) });
      render(<PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} />);
      fireEvent.click(screen.getByRole('button', { name: /^Copy/ }));
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(unhandled).not.toHaveBeenCalled();
      expect(screen.queryByText('Copied')).not.toBeInTheDocument();
    } finally {
      nodeProcess.off('unhandledRejection', unhandled);
    }
  });

  it('unavailable: muted, the error, and Check again', async () => {
    const onRecheck = vi.fn();
    const { container } = render(
      <PrivilegeAuditBanner
        audit={audit({ status: 'unavailable', error: 'connection refused' })}
        onRecheck={onRecheck}
      />,
    );
    expect(screen.getByText('Privileges could not be checked: connection refused.')).toBeInTheDocument();
    expect(container.firstChild).toHaveClass('bg-muted/40');
    await userEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(onRecheck).toHaveBeenCalledOnce();
  });

  it('unavailable without an error text has no colon', () => {
    render(<PrivilegeAuditBanner audit={audit({ status: 'unavailable', error: null })} />);
    expect(screen.getByText('Privileges could not be checked.')).toBeInTheDocument();
  });

  it('unavailable does not double the full stop of the error', () => {
    render(
      <PrivilegeAuditBanner audit={audit({ status: 'unavailable', error: 'connection refused. ' })} />,
    );
    expect(screen.getByText('Privileges could not be checked: connection refused.')).toBeInTheDocument();
  });

  it('Check again is offered with findings too and disabled while checking', () => {
    render(
      <PrivilegeAuditBanner audit={audit({ findings: [SUPERUSER] })} onRecheck={vi.fn()} rechecking />,
    );
    expect(screen.getByRole('button', { name: 'Checking…' })).toBeDisabled();
  });

  it('no Check again without a handler', () => {
    render(<PrivilegeAuditBanner audit={audit({ status: 'unavailable', error: 'x' })} />);
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument();
  });
});
