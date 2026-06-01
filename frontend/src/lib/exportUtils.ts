// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Client-side export utilities for query results. */

import type { QueryResults } from '../types';

function escapeCsvField(value: unknown): string {
  if (value === null || value === undefined) return '';
  const str = String(value);
  if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function downloadCsv(results: QueryResults, filename = 'query-results.csv'): void {
  const header = results.columns.map(escapeCsvField).join(',');
  const rows = results.rows.map((row) => row.map(escapeCsvField).join(','));
  const csv = [header, ...rows].join('\r\n');
  triggerBlobDownload(new Blob([csv], { type: 'text/csv;charset=utf-8' }), filename);
}

export function downloadJson(results: QueryResults, filename = 'query-results.json'): void {
  const records = results.rows.map((row) =>
    Object.fromEntries(results.columns.map((col, i) => [col, row[i]])),
  );
  const json = JSON.stringify(records, null, 2);
  triggerBlobDownload(new Blob([json], { type: 'application/json' }), filename);
}

/** Trigger a browser download from an API blob response. */
export function downloadBlob(data: Blob, filename: string): void {
  triggerBlobDownload(data, filename);
}

/** Trigger a browser download from a pre-built object URL or data URL. */
export function triggerUrlDownload(url: string, filename: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
}
