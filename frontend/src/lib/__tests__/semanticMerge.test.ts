import { describe, it, expect } from 'vitest';
import { deepEqual, mergeSemanticModel, relationshipKey } from '../semanticMerge';
import type { SemanticModel, TableSemantic } from '../../types';

const table = (over: Partial<TableSemantic> = {}): TableSemantic => ({
  display_name: 'Orders',
  description: 'Orders placed',
  default_filters: [],
  columns: {
    status: { display_name: 'Status', description: null, value_mappings: [], is_sensitive: false },
    total: { display_name: 'Total', description: 'Amount', value_mappings: [], is_sensitive: false },
  },
  ...over,
});

const metric = (name: string, description = 'd') => ({
  metric_type: 'simple' as const,
  name,
  definition: 'sum(total)',
  description,
  filters: [],
  related_tables: ['public.orders'],
});

const model = (over: Partial<SemanticModel> = {}): SemanticModel => ({
  tables: { 'public.orders': table() },
  business_metrics: [metric('revenue')],
  common_joins: [],
  generated_at: '2026-01-01T00:00:00Z',
  is_user_reviewed: false,
  generation_model: 'm',
  relationships: [
    {
      from_table: 'public.orders',
      from_column: 'customer_id',
      to_table: 'public.customers',
      to_column: 'id',
      relationship_type: 'many_to_one',
      join_sql: '',
      is_required: true,
      description: null,
    },
  ],
  derived_columns: [],
  time_expressions: {},
  db_timezone: null,
  schema_hash: null,
  source_dialect: 'postgres',
  generation_warnings: [],
  segments: [],
  notes: ['first'],
  version: 3,
  ...over,
});

/** A structurally independent copy with one table field changed. */
const withTableField = (m: SemanticModel, key: string, field: keyof TableSemantic, value: unknown) => ({
  ...m,
  tables: { ...m.tables, [key]: { ...m.tables[key], [field]: value } },
});
const withColumnField = (m: SemanticModel, key: string, col: string, field: string, value: unknown) => ({
  ...m,
  tables: {
    ...m.tables,
    [key]: {
      ...m.tables[key],
      columns: { ...m.tables[key].columns, [col]: { ...m.tables[key].columns[col], [field]: value } },
    },
  },
});

describe('deepEqual', () => {
  it('treats undefined and a missing key alike', () => {
    expect(deepEqual({ a: 1, b: undefined }, { a: 1 })).toBe(true);
    expect(deepEqual({ a: 1 }, { a: 2 })).toBe(false);
    expect(deepEqual([1, 2], [1, 2])).toBe(true);
    expect(deepEqual([1, 2], [2, 1])).toBe(false);
    expect(deepEqual(null, undefined)).toBe(true);
  });
});

describe('mergeSemanticModel', () => {
  it('yields latest with no conflicts when the draft equals base', () => {
    const base = model();
    const latest = withTableField(model({ version: 4 }), 'public.orders', 'description', 'Changed');
    const { merged, conflicts, serverChanges } = mergeSemanticModel(base, model(), latest);
    expect(merged).toEqual(latest);
    expect(conflicts).toEqual([]);
    expect(serverChanges).toBe(1);
  });

  it('is deep-equal to the draft when latest equals base', () => {
    const base = model();
    const draft = withColumnField(model(), 'public.orders', 'status', 'description', 'Order state');
    const { merged, conflicts, serverChanges } = mergeSemanticModel(base, draft, model());
    expect(merged).toEqual(draft);
    expect(conflicts).toEqual([]);
    expect(serverChanges).toBe(0);
  });

  it('keeps a user change and takes an untouched unit from the server', () => {
    const base = model();
    const draft = withColumnField(model(), 'public.orders', 'status', 'description', 'Order state');
    const latest = withColumnField(model({ version: 4 }), 'public.orders', 'total', 'description', 'Gross');
    const { merged, conflicts, serverChanges } = mergeSemanticModel(base, draft, latest);
    expect(merged.tables['public.orders'].columns.status.description).toBe('Order state');
    expect(merged.tables['public.orders'].columns.total.description).toBe('Gross');
    expect(merged.version).toBe(4);
    expect(conflicts).toEqual([]);
    expect(serverChanges).toBe(1);
  });

  it('keeps the draft and lists a conflict when both sides changed one field', () => {
    const base = model();
    const draft = withTableField(model(), 'public.orders', 'description', 'Mine');
    const latest = withTableField(model(), 'public.orders', 'description', 'Theirs');
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.tables['public.orders'].description).toBe('Mine');
    expect(conflicts).toEqual(['public.orders: description']);
  });

  it('reports no conflict when both sides made the same change', () => {
    const base = model();
    const draft = withTableField(model(), 'public.orders', 'description', 'Same');
    const latest = withTableField(model(), 'public.orders', 'description', 'Same');
    const { conflicts, serverChanges } = mergeSemanticModel(base, draft, latest);
    expect(conflicts).toEqual([]);
    expect(serverChanges).toBe(0);
  });

  it('drops a table or column the server no longer has, listing it when the draft edited it', () => {
    const base = model();
    const draft = withColumnField(model(), 'public.orders', 'total', 'description', 'Edited');
    const latest = model();
    delete latest.tables['public.orders'].columns.total;
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.tables['public.orders'].columns.total).toBeUndefined();
    expect(conflicts).toEqual(['public.orders.total: column dropped on the server']);

    const gone = model({ tables: {} });
    const edited = withTableField(model(), 'public.orders', 'description', 'Edited');
    const r = mergeSemanticModel(base, edited, gone);
    expect(r.merged.tables).toEqual({});
    expect(r.conflicts).toEqual(['public.orders: table dropped on the server']);
    expect(mergeSemanticModel(base, model(), gone).conflicts).toEqual([]);
  });

  it('takes a table and a column the server added', () => {
    const base = model();
    const latest = model({ tables: { ...model().tables, 'public.items': table({ display_name: 'Items' }) } });
    latest.tables['public.orders'].columns.qty = {
      display_name: 'Qty', description: null, value_mappings: [], is_sensitive: false,
    };
    const { merged, serverChanges } = mergeSemanticModel(base, model(), latest);
    expect(merged.tables['public.items'].display_name).toBe('Items');
    expect(merged.tables['public.orders'].columns.qty.display_name).toBe('Qty');
    expect(serverChanges).toBe(2);
  });

  it('merges list sections by identity', () => {
    const base = model({ business_metrics: [metric('revenue'), metric('orders'), metric('aov')] });
    // User: edits revenue, deletes orders, adds margin.
    const draft = model({
      business_metrics: [metric('revenue', 'mine'), metric('aov'), metric('margin')],
    });
    // Server: changes aov, deletes nothing, adds refunds, reorders.
    const latest = model({
      business_metrics: [metric('aov', 'theirs'), metric('refunds'), metric('orders'), metric('revenue')],
    });
    const { merged, conflicts, serverChanges } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics.map((m) => m.name)).toEqual(['aov', 'refunds', 'revenue', 'margin']);
    expect(merged.business_metrics[0].description).toBe('theirs');
    expect(merged.business_metrics[2].description).toBe('mine');
    expect(conflicts).toEqual([]);
    expect(serverChanges).toBe(2);
  });

  it('keeps a user deletion over a server edit and lists it', () => {
    const base = model({ business_metrics: [metric('revenue')] });
    const draft = model({ business_metrics: [] });
    const latest = model({ business_metrics: [metric('revenue', 'theirs')] });
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics).toEqual([]);
    expect(conflicts).toEqual(['metric revenue (deleted by you, changed on the server)']);
  });

  it('keeps a user edit over a server deletion and lists it', () => {
    const base = model({ business_metrics: [metric('revenue')] });
    const draft = model({ business_metrics: [metric('revenue', 'mine')] });
    const latest = model({ business_metrics: [] });
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics.map((m) => m.name)).toEqual(['revenue']);
    expect(conflicts).toEqual(['metric revenue (deleted on the server, changed by you)']);
  });

  it('lists both-sides edits of a list item as a conflict and keeps the draft', () => {
    const base = model({ business_metrics: [metric('revenue')] });
    const draft = model({ business_metrics: [metric('revenue', 'mine')] });
    const latest = model({ business_metrics: [metric('revenue', 'theirs')] });
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics[0].description).toBe('mine');
    expect(conflicts).toEqual(['metric revenue']);
  });

  it('keys relationships by their endpoints, joins by pattern, segments and derived columns by name, notes by text', () => {
    const base = model();
    const edge = { ...base.relationships[0], description: 'FK' };
    const draft = model({
      relationships: [edge],
      common_joins: [{ description: 'j', tables: ['a', 'b'], join_pattern: 'a JOIN b' }],
      segments: [{ name: 'active', sql_expression: 'x', description: '', applicable_tables: [] }],
      derived_columns: [{ name: 'net', sql_expression: 'a-b', base_tables: [], description: '', format_hint: null, available_on: [] }],
      notes: ['first', 'second'],
    });
    const latest = model({ relationships: [], notes: ['first', 'third'] });
    const { merged, conflicts, serverChanges } = mergeSemanticModel(base, draft, latest);
    expect(relationshipKey(edge)).toBe('public.orders.customer_id->public.customers.id');
    expect(merged.relationships).toEqual([edge]);
    expect(merged.common_joins.map((j) => j.join_pattern)).toEqual(['a JOIN b']);
    expect(merged.segments?.map((s) => s.name)).toEqual(['active']);
    expect(merged.derived_columns.map((d) => d.name)).toEqual(['net']);
    expect(merged.notes).toEqual(['first', 'third', 'second']);
    expect(conflicts).toEqual([
      'relationship public.orders.customer_id->public.customers.id (deleted on the server, changed by you)',
    ]);
    expect(serverChanges).toBe(1);
  });

  it('applies the rule to the reviewed flag', () => {
    const base = model({ is_user_reviewed: false });
    expect(mergeSemanticModel(base, model({ is_user_reviewed: true }), model()).merged.is_user_reviewed).toBe(true);
    const r = mergeSemanticModel(base, model(), model({ is_user_reviewed: true }));
    expect(r.merged.is_user_reviewed).toBe(true);
    expect(r.serverChanges).toBe(1);
  });
  it('keeps the user version of an item added on both sides, listing it only when they differ', () => {
    const base = model({ business_metrics: [] });
    const same = mergeSemanticModel(base, model({ business_metrics: [metric('aov')] }), model({ business_metrics: [metric('aov')] }));
    expect(same.merged.business_metrics.map((m) => m.description)).toEqual(['d']);
    expect(same.conflicts).toEqual([]);
    const differ = mergeSemanticModel(
      base,
      model({ business_metrics: [metric('aov', 'mine')] }),
      model({ business_metrics: [metric('aov', 'theirs')] }),
    );
    expect(differ.merged.business_metrics.map((m) => m.description)).toEqual(['mine']);
    expect(differ.conflicts).toEqual(['metric aov']);
  });

  it('keeps two equal notes as two', () => {
    const base = model({ notes: ['', ''] });
    const { merged } = mergeSemanticModel(base, model({ notes: ['', '', 'x'] }), model({ notes: ['', ''] }));
    expect(merged.notes).toEqual(['', '', 'x']);
  });

  it('treats a table or column missing from the draft as unchanged', () => {
    const base = model();
    const draft = model({ tables: {} });
    const latest = withTableField(model(), 'public.orders', 'description', 'Theirs');
    const { merged, conflicts } = mergeSemanticModel(base, draft, latest);
    expect(merged.tables['public.orders'].description).toBe('Theirs');
    expect(merged.tables['public.orders'].columns.status.display_name).toBe('Status');
    expect(conflicts).toEqual([]);
  });
  it('keeps the user order when they reordered, with server additions after', () => {
    const base = model({ business_metrics: [metric('a'), metric('b'), metric('c')] });
    const draft = model({ business_metrics: [metric('c'), metric('a'), metric('b')] });
    const latest = model({ business_metrics: [metric('a'), metric('b'), metric('c'), metric('d')] });
    const { merged } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics.map((m) => m.name)).toEqual(['c', 'a', 'b', 'd']);
  });

  it('follows the server order when the user did not reorder', () => {
    const base = model({ business_metrics: [metric('a'), metric('b')] });
    const draft = model({ business_metrics: [metric('a'), metric('b'), metric('x')] });
    const latest = model({ business_metrics: [metric('b'), metric('a')] });
    const { merged } = mergeSemanticModel(base, draft, latest);
    expect(merged.business_metrics.map((m) => m.name)).toEqual(['b', 'a', 'x']);
  });
});

