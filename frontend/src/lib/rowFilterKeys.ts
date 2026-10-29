// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// Per dialect (lower-cased `query_dialect`): the identifier quote, and the parts the server
// stores unchanged when typed bare (PostgreSQL folds a bare part to lowercase, MySQL keeps
// its case; both refuse other characters unquoted).
const KEY_QUOTE: Record<string, string> = { postgresql: '"', mysql: '`' };
const BARE_PART: Record<string, RegExp> = {
  postgresql: /^[a-z_][a-z0-9_$]*$/,
  mysql: /^[A-Za-z_][A-Za-z0-9_$]*$/,
};

/** A stored row-filter key (`schema.table`, engine-normalised names) as the admin must type
 *  it for the next save to store the same key: a part that is not bare for the dialect is
 *  quoted. A part already quoted is left alone, so the form's own output round-trips. */
export function displayRowFilterKey(key: string, dialect?: string): string {
  const quote = dialect ? KEY_QUOTE[dialect.toLowerCase()] : undefined;
  const bare = dialect ? BARE_PART[dialect.toLowerCase()] : undefined;
  const dot = key.indexOf('.');
  if (!quote || !bare || dot < 0) return key;
  const shown = (part: string) => {
    const quoted = part.length >= 2 && part.startsWith(quote) && part.endsWith(quote);
    return quoted || bare.test(part) ? part : `${quote}${part}${quote}`;
  };
  return `${shown(key.slice(0, dot))}.${shown(key.slice(dot + 1))}`;
}

/** `map` with every key in its displayed form, for comparing with what the form rows emit. */
export function displayRowFilterMap(
  map: Record<string, string>,
  dialect?: string,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(map).map(([table, condition]) => [displayRowFilterKey(table, dialect), condition]),
  );
}
