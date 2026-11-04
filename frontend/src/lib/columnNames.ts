// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**
 * Result column names made unique for use as object keys (charts key each row object by
 * column name, so two columns called `count` would keep only the last value). The first
 * occurrence keeps its name, so a saved chart keyed on it still works; the n-th occurrence
 * becomes `name_n`, skipping any candidate that is itself a column name or already taken.
 * The results table and the CSV export are positional and do not use this.
 */
export function uniqueColumnNames(columns: string[]): string[] {
  const taken = new Set(columns);
  const assigned = new Set<string>();
  const seen = new Map<string, number>();
  return columns.map((col) => {
    const n = (seen.get(col) ?? 0) + 1;
    seen.set(col, n);
    // A first occurrence is in `taken`, and no suffix the loop below hands out ever is,
    // so it can always keep its name.
    if (n === 1) return col;
    let k = n;
    let candidate = `${col}_${k}`;
    while (taken.has(candidate) || assigned.has(candidate)) {
      k += 1;
      candidate = `${col}_${k}`;
    }
    assigned.add(candidate);
    return candidate;
  });
}
