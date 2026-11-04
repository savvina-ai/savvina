// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import type {
  BusinessMetric,
  ColumnSemantic,
  CommonJoin,
  DerivedColumn,
  RelationshipEdge,
  Segment,
  SemanticModel,
  TableSemantic,
} from '../types';

/**
 * Three-way merge of a semantic model draft with the stored model, for a save refused
 * with 409 (the model changed since the draft was loaded).
 *
 * `base` is the server model the draft was seeded from, `draft` the user's edits, `latest`
 * the server's current model. Per unit (a table field, a column field, a list item, a
 * scalar): a value the user changed wins; one they did not touch takes `latest`; where
 * both sides changed it to different values the draft wins and the unit is listed in
 * `conflicts`. Tables and columns absent from `latest` are dropped (the schema no longer
 * has them), and ones `latest` added are taken as they are. List items are matched by
 * identity (name, join pattern, relationship endpoints, the note's text), ordered as
 * `latest` orders them with the user's additions appended. Everything the server owns
 * (generation stamps, warnings, status, `version`) comes from `latest`, so the merged draft
 * saves against the latest version. `serverChanges` counts the units taken from `latest`
 * that differ from `base`, for the banner.
 */
export interface MergeResult {
  merged: SemanticModel;
  conflicts: string[];
  serverChanges: number;
}

/**
 * The opening of the backend's 409 for a stale draft (`update_semantic_model`): the only
 * 409 the page answers with a merge. Any other 409 (the store's own conflict) is shown as
 * an error, since nothing says the stored model moved.
 */
export const STALE_DRAFT = 'The semantic model changed since this draft was loaded';

type Plain = Record<string, unknown>;

/** Structural equality; `undefined` and a missing key are the same thing. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null || b == null) return a == null && b == null;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a !== 'object' || typeof b !== 'object') return false;
  const keys = new Set([...Object.keys(a as Plain), ...Object.keys(b as Plain)]);
  for (const k of keys) {
    if (!deepEqual((a as Plain)[k], (b as Plain)[k])) return false;
  }
  return true;
}

interface Tally {
  conflicts: string[];
  serverChanges: number;
}

/** The three-way rule for one unit. Returns the value to keep. */
function pick<T>(base: T, draft: T, latest: T, label: string, tally: Tally): T {
  const userChanged = !deepEqual(draft, base);
  const serverChanged = !deepEqual(latest, base);
  if (!userChanged) {
    if (serverChanged) tally.serverChanges += 1;
    return latest;
  }
  if (serverChanged && !deepEqual(draft, latest)) tally.conflicts.push(label);
  return draft;
}

function mergeFields<T extends object>(
  base: T | undefined,
  draft: T | undefined,
  latest: T,
  label: string,
  tally: Tally,
  skip: ReadonlySet<string>,
): T {
  const b = (base ?? {}) as Plain;
  const d = (draft ?? {}) as Plain;
  const l = latest as unknown as Plain;
  const keys = new Set([...Object.keys(b), ...Object.keys(d), ...Object.keys(l)]);
  const out: Plain = {};
  for (const key of keys) {
    if (skip.has(key)) continue;
    const value = pick(b[key], d[key], l[key], `${label}: ${key}`, tally);
    if (value !== undefined) out[key] = value;
  }
  return out as T;
}

const NO_SKIP: ReadonlySet<string> = new Set();
const SKIP_COLUMNS: ReadonlySet<string> = new Set(['columns']);

function mergeColumns(
  tableKey: string,
  base: Record<string, ColumnSemantic> | undefined,
  draft: Record<string, ColumnSemantic> | undefined,
  latest: Record<string, ColumnSemantic>,
  tally: Tally,
): Record<string, ColumnSemantic> {
  const out: Record<string, ColumnSemantic> = {};
  for (const [name, latestCol] of Object.entries(latest)) {
    const baseCol = base?.[name];
    const draftCol = draft?.[name];
    if (baseCol === undefined) {
      // Added on the server; the user cannot add columns.
      tally.serverChanges += 1;
      out[name] = latestCol;
      continue;
    }
    // The page cannot remove a column: a draft without it is treated as unchanged.
    out[name] = mergeFields(
      baseCol,
      draftCol ?? baseCol,
      latestCol,
      `${tableKey}.${name}`,
      tally,
      NO_SKIP,
    );
  }
  // Dropped on the server: gone, whatever the draft did to it.
  for (const name of Object.keys(base ?? {})) {
    if (name in latest) continue;
    tally.serverChanges += 1;
    if (draft?.[name] !== undefined && !deepEqual(draft[name], base?.[name])) {
      tally.conflicts.push(`${tableKey}.${name}: column dropped on the server`);
    }
  }
  return out;
}

function mergeTables(
  base: Record<string, TableSemantic>,
  draft: Record<string, TableSemantic>,
  latest: Record<string, TableSemantic>,
  tally: Tally,
): Record<string, TableSemantic> {
  const out: Record<string, TableSemantic> = {};
  for (const [key, latestTable] of Object.entries(latest)) {
    const baseTable = base[key];
    const draftTable = draft[key];
    if (baseTable === undefined) {
      tally.serverChanges += 1;
      out[key] = latestTable;
      continue;
    }
    // The page cannot remove a table: a draft without it is treated as unchanged.
    const draftSide = draftTable ?? baseTable;
    const fields = mergeFields(baseTable, draftSide, latestTable, key, tally, SKIP_COLUMNS);
    out[key] = {
      ...fields,
      columns: mergeColumns(key, baseTable.columns, draftSide.columns, latestTable.columns, tally),
    };
  }
  for (const key of Object.keys(base)) {
    if (key in latest) continue;
    tally.serverChanges += 1;
    if (draft[key] !== undefined && !deepEqual(draft[key], base[key])) {
      tally.conflicts.push(`${key}: table dropped on the server`);
    }
  }
  return out;
}

/**
 * A list keyed by identity. Items are compared whole: an edited item is the user's
 * version, a deleted one stays deleted, an added one is appended. Order: the user's when
 * they reordered the items they kept (their relative order differs from `base`), with the
 * server's additions after them; otherwise `latest`'s, with the user's additions after.
 */
function mergeList<T>(
  base: readonly T[],
  draft: readonly T[],
  latest: readonly T[],
  keyOf: (item: T) => string,
  labelOf: (key: string) => string,
  tally: Tally,
): T[] {
  const byKey = (items: readonly T[]) => new Map(items.map((item) => [keyOf(item), item]));
  const b = byKey(base);
  const d = byKey(draft);
  const l = byKey(latest);
  const out: T[] = [];
  for (const [key, latestItem] of l) {
    const baseItem = b.get(key);
    const draftItem = d.get(key);
    if (baseItem === undefined) {
      if (draftItem === undefined) {
        tally.serverChanges += 1; // added on the server
        out.push(latestItem);
      } else {
        // Added on both sides under the same key: the user's version.
        if (!deepEqual(draftItem, latestItem)) tally.conflicts.push(labelOf(key));
        out.push(draftItem);
      }
      continue;
    }
    if (draftItem === undefined) {
      // Deleted by the user: stays deleted, even if the server changed it.
      if (!deepEqual(latestItem, baseItem)) tally.conflicts.push(`${labelOf(key)} (deleted by you, changed on the server)`);
      continue;
    }
    out.push(pick(baseItem, draftItem, latestItem, labelOf(key), tally));
  }
  for (const [key, draftItem] of d) {
    if (l.has(key)) continue;
    const baseItem = b.get(key);
    if (baseItem === undefined) {
      out.push(draftItem); // added by the user
    } else if (!deepEqual(draftItem, baseItem)) {
      // Deleted on the server, edited by the user: the user's version, as a conflict.
      tally.conflicts.push(`${labelOf(key)} (deleted on the server, changed by you)`);
      out.push(draftItem);
    } else {
      tally.serverChanges += 1; // deleted on the server, untouched here: stays deleted
    }
  }
  return userReordered(base, draft, keyOf) ? inDraftOrder(out, draft, keyOf) : out;
}

/** The user moved items: the keys both lists hold appear in a different relative order. */
function userReordered<T>(base: readonly T[], draft: readonly T[], keyOf: (item: T) => string) {
  const inDraft = new Set(draft.map(keyOf));
  const inBase = new Set(base.map(keyOf));
  const a = base.map(keyOf).filter((k) => inDraft.has(k));
  const b = draft.map(keyOf).filter((k) => inBase.has(k));
  return a.some((k, i) => k !== b[i]);
}

/** *items* in the draft's order; items the draft does not hold (server additions) last. */
function inDraftOrder<T>(items: T[], draft: readonly T[], keyOf: (item: T) => string): T[] {
  const rank = new Map(draft.map((item, i) => [keyOf(item), i]));
  return items
    .map((item, i) => ({ item, i, r: rank.get(keyOf(item)) }))
    .sort((x, y) =>
      x.r !== undefined && y.r !== undefined
        ? x.r - y.r
        : x.r !== undefined
          ? -1
          : y.r !== undefined
            ? 1
            : x.i - y.i,
    )
    .map((x) => x.item);
}

/** The backend's `relationship_key`: the four endpoints, joined. */
export function relationshipKey(edge: RelationshipEdge): string {
  return `${edge.from_table}.${edge.from_column}->${edge.to_table}.${edge.to_column}`;
}

const shortNote = (note: string) => (note.length > 40 ? `${note.slice(0, 37)}…` : note);

interface KeyedNote {
  key: string;
  text: string;
}

function keyNotes(notes: string[]): KeyedNote[] {
  const seen = new Map<string, number>();
  return notes.map((text) => {
    const n = (seen.get(text) ?? 0) + 1;
    seen.set(text, n);
    return { key: `${text}#${n}`, text };
  });
}

const unkeyNotes = (notes: KeyedNote[]) => notes.map((n) => n.text);

export function mergeSemanticModel(
  base: SemanticModel,
  draft: SemanticModel,
  latest: SemanticModel,
): MergeResult {
  const tally: Tally = { conflicts: [], serverChanges: 0 };
  const merged: SemanticModel = {
    // Server-owned fields (stamps, warnings, status, version) come from `latest`.
    ...latest,
    tables: mergeTables(base.tables, draft.tables, latest.tables, tally),
    business_metrics: mergeList<BusinessMetric>(
      base.business_metrics,
      draft.business_metrics,
      latest.business_metrics,
      (m) => m.name,
      (k) => `metric ${k}`,
      tally,
    ),
    segments: mergeList<Segment>(
      base.segments ?? [],
      draft.segments ?? [],
      latest.segments ?? [],
      (s) => s.name,
      (k) => `segment ${k}`,
      tally,
    ),
    derived_columns: mergeList<DerivedColumn>(
      base.derived_columns,
      draft.derived_columns,
      latest.derived_columns,
      (d) => d.name,
      (k) => `derived column ${k}`,
      tally,
    ),
    common_joins: mergeList<CommonJoin>(
      base.common_joins,
      draft.common_joins,
      latest.common_joins,
      (j) => j.join_pattern,
      (k) => `join ${k}`,
      tally,
    ),
    relationships: mergeList<RelationshipEdge>(
      base.relationships,
      draft.relationships,
      latest.relationships,
      relationshipKey,
      (k) => `relationship ${k}`,
      tally,
    ),
    // Keyed by text and occurrence, so two equal notes (two blank ones, say) stay two.
    notes: unkeyNotes(
      mergeList<KeyedNote>(
        keyNotes(base.notes ?? []),
        keyNotes(draft.notes ?? []),
        keyNotes(latest.notes ?? []),
        (n) => n.key,
        (k) => `note "${shortNote(k.slice(0, k.lastIndexOf('#')))}"`,
        tally,
      ),
    ),
    is_user_reviewed: pick(
      base.is_user_reviewed,
      draft.is_user_reviewed,
      latest.is_user_reviewed,
      'Marked as reviewed',
      tally,
    ),
  };
  return { merged, conflicts: tally.conflicts, serverChanges: tally.serverChanges };
}
