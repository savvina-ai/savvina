# Security Policy

## Reporting a vulnerability

Please do not report security vulnerabilities through public GitHub
Issues.

Email security@savvina.ai with:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fix if you have one

You will receive a response within 48 hours. We will keep you informed
of progress toward a fix and public disclosure.

We ask that you give us reasonable time to address the issue before
any public disclosure.

---

## Known security exceptions

Some security constraints are intentionally not enforced due to framework or architectural
limitations. Each exception is reviewed, documented with mitigating controls, and tracked in
the [Security Runbook](docs/administration/security-runbook.md).

| ID | Summary |
|----|---------|
| SEC-8 | `style-src 'unsafe-inline'` in CSP — required by Radix UI portal components; nonce-based hardening is infeasible without SSR. Low severity; mitigated by `script-src 'self'` and `X-Frame-Options: DENY`. See [security-runbook.md](docs/administration/security-runbook.md#csp-style-src-unsafe-inline) for full details. |
