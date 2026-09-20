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
 * `fallback` is used when the error carries no `detail` at all (network error,
 * non-FastAPI response). Without it the caller gets `String(err)`, which reads
 * as "AxiosError: Request failed with status code 500".
 */
export function apiErrorMessage(err: unknown, fallback?: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) return detail.map((d) => formatValidationItem(d)).join('; ');
    if (detail && typeof detail === 'object') return JSON.stringify(detail);
  }
  return fallback ?? String(err);
}
