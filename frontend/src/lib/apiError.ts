// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/** Turn an axios/FastAPI error into a plain string safe to render. */

interface ValidationItem {
  loc?: unknown[];
  msg?: string;
}

function formatValidationItem(item: ValidationItem): string {
  const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
  const msg = item.msg ?? JSON.stringify(item);
  return loc ? `${loc}: ${msg}` : msg;
}

/**
 * FastAPI returns `detail` as a string for HTTPException but as an array of
 * validation objects for 422s. Rendering the array directly crashes React
 * (error #31), so always collapse it to a string here.
 *
 * Without a `detail`, a 429 says a rate limit was hit (naming it when slowapi's `error`
 * text is there) and any other string `error` is shown as is. `fallback` is used when the error carries neither
 * (network error, non-FastAPI response). Without it the caller gets `String(err)`, which reads
 * as "AxiosError: Request failed with status code 500".
 */
export function apiErrorMessage(err: unknown, fallback?: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const response = (
      err as { response?: { status?: number; data?: { detail?: unknown; error?: unknown } } }
    ).response;
    const detail = response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) return detail.map((d) => formatValidationItem(d)).join('; ');
    if (detail && typeof detail === 'object') return JSON.stringify(detail);
    // slowapi's rate-limit handler answers `{"error": "Rate limit exceeded: 5 per 1
    // minute"}` with no `detail`. Most limits are per minute, but the auth routes use the
    // operator's AUTH_RATE_LIMIT, which may be per hour — so name the limit rather than
    // guess the wait. A 429 with a detail (a provider throttle the backend maps to 429)
    // kept its own reason above.
    const error = response?.data?.error;
    const errorText = typeof error === 'string' && error ? error : null;
    if (response?.status === 429) {
      return errorText
        ? `Too many requests (${errorText}) — try again later`
        : 'Too many requests — try again later';
    }
    if (errorText) return errorText;
  }
  return fallback ?? String(err);
}
