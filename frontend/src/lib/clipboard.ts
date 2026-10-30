// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/** Copies `text` to the clipboard and says whether it worked. Never rejects.
 *  Uses the async Clipboard API where it exists; on a non-secure origin (self-hosted over
 *  plain HTTP, where `navigator.clipboard` is undefined) falls back to `execCommand`. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const el = document.createElement('textarea');
    el.value = text;
    el.style.position = 'fixed';
    el.style.opacity = '0';
    document.body.appendChild(el);
    try {
      el.focus();
      el.select();
      return document.execCommand('copy'); // legacy fallback for HTTP contexts
    } finally {
      document.body.removeChild(el);
    }
  } catch {
    return false;
  }
}
