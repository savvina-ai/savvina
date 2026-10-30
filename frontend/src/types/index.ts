// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// ── Pagination ──
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ── Data Sources ──
export interface DataSourceInfo {
  source_type: string;
  display_name: string;
  icon: string;
  query_dialect: string;
  config_schema: ConfigSchema;
}

export interface ConfigSchema {
  fields: ConfigField[];
}

export interface ConfigField {
  name: string;
  type: 'string' | 'integer' | 'password' | 'select' | 'boolean' | 'textarea';
  label: string;
  required?: boolean;
  default?: unknown;
  placeholder?: string;
  options?: string[];
  required_if?: Record<string, string>;
  disabled_if?: Record<string, unknown>;
}

// ── Privacy Settings ──
export interface PrivacySettings {
  include_sample_values: boolean;
  include_column_comments: boolean;
  include_row_counts: boolean;
  sensitive_column_patterns: string[];
  excluded_schemas: string[];
  excluded_tables: string[];
  excluded_columns: string[];
  /** Per-table row filters: {"schema.table": "<condition without WHERE>"}. Tables not
   *  listed are unfiltered. On PUT, an absent field leaves the map unchanged and {} clears
   *  it (refused while a legacy row_filter_sql is stored, unless clear_legacy_row_filter). */
  row_filters: Record<string, string>;
  /** Legacy whole-query filter (before 2.3). Read only, shown in the migration banner;
   *  the server ignores it on PUT. */
  row_filter_sql?: string | null;
}

/** Payload of PUT /connections/{id}/privacy: any subset of the settings plus the command
 *  that removes the legacy whole-query filter. */
export type PrivacySettingsUpdate = Partial<PrivacySettings> & {
  clear_legacy_row_filter?: boolean;
};

// ── Connections ──
export interface Connection {
  id: string;
  name: string;
  source_type: string;
  execution_mode: 'auto_execute' | 'review_first' | 'generate_only';
  is_active: boolean;
  schema_cached_at: string | null;
  created_at: string;
}

// ── Privilege audit ──
export type PrivilegeFindingCode =
  | 'superuser'
  | 'super_privilege'
  | 'bypassrls'
  | 'owner_without_force'
  | 'write_privileges'
  | 'reads_excluded_tables'
  | 'sql_from_string';

export interface AuditFinding {
  code: PrivilegeFindingCode;
  /** `schema.table` keys; empty for a role-level finding. */
  tables: string[];
  /** `sql_from_string` only: `schema.name(argtypes)`. */
  functions: string[];
  /** Tables beyond the 50 listed in `tables`. */
  more_tables: number;
  /** The SQL the admin runs; one statement per line. */
  remediation: string;
}

/** The last privilege audit of the connection role (stored on the connection; written on
 *  create, Test, a privacy save that changes the filtered or excluded tables, and a config
 *  save). `unavailable`: the catalog could not be read, `error` says why. */
export interface PrivilegeAudit {
  checked_at: string;
  role: string;
  status: 'ok' | 'unavailable';
  error: string | null;
  findings: AuditFinding[];
  /** Some row-filtered table has native row-level security enabled: the superuser,
   *  bypassrls and owner findings are then about RLS, otherwise about least privilege. */
  rls_in_use: boolean;
  rls_effective: Record<string, boolean>;
}

export interface ConnectionDetail extends Connection {
  privacy_settings: PrivacySettings | null;
  row_filter_needs_migration: boolean;
  privilege_audit: PrivilegeAudit | null;
  schema_cache: Record<string, unknown> | null;
}

// ── Chat ──
export type MessageStatus = 'pending_approval' | 'executed' | 'query_only' | 'error' | 'cached';

export interface ChatRequest {
  connection_id: string;
  session_id: string | null;
  message: string;
  provider: string;
  options: {
    show_query: boolean;
    max_rows: number;
    explain_results: boolean;
    bypass_cache?: boolean;
    force_refresh?: boolean;
  };
}

export interface ChatResponse {
  session_id: string;
  message_id: string;
  query: string | null;
  query_dialect: string | null;
  explanation: string;
  results: QueryResults | null;
  execution_time_ms: number | null;
  token_count: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  status: MessageStatus;
  cache_hit: boolean;
  error: string | null;
}

export type RowValue = string | number | boolean | null | undefined;

export interface QueryResults {
  columns: string[];
  column_types: string[];
  rows: RowValue[][];
  row_count: number;
  truncated: boolean;
  bytes_scanned: number | null;
  /** The row-filter map the rows were computed under; "" for none. */
  row_filter_fingerprint?: string | null;
  /** Set by the server on a stored result computed under a different row filter: rows are empty. */
  stale_row_filter?: boolean;
}

export interface ChatSession {
  id: string;
  connection_id: string;
  title: string;
  provider: string;
  created_at: string;
  updated_at: string;
  cache_hit_count: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  query_generated: string | null;
  query_dialect: string | null;
  results_json: QueryResults | null;
  execution_time_ms: number | null;
  token_count: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  status: MessageStatus;
  cache_hit: boolean;
  feedback: 'thumbs_up' | 'thumbs_down' | null;
  error: string | null;
  warning?: string | null;
  created_at: string;
}

// ── Providers ──
export interface ProviderStatus {
  id: string | null;
  provider_type: string;
  display_name: string;
  provider_display_name: string;
  is_active: boolean;
  is_configured: boolean;
  is_healthy: boolean;
  current_model: string;
  available_models: string[];
  base_url: string | null;
  updated_at: string | null;
}

// ── Semantic Model ──
export interface RelationshipEdge {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  relationship_type: string;
  join_sql: string;
  is_required: boolean;
  description: string | null;
  /** PRIMARY = PK/dimension side, FOREIGN = FK/fact side. */
  entity_type?: string | null;
}

/** A named reusable filter expression, applicable to one or more tables. */
export interface Segment {
  name: string;
  sql_expression: string;
  description: string;
  applicable_tables: string[];
}

/** An ordered drill-down path within a table. Generator-only — read-only in the UI. */
export interface Hierarchy {
  name: string;
  levels: string[];
}

export interface DerivedColumn {
  name: string;
  sql_expression: string;
  base_tables: string[];
  description: string;
  format_hint: string | null;
  available_on: string[];
}

export interface SemanticModel {
  tables: Record<string, TableSemantic>;
  business_metrics: BusinessMetric[];
  common_joins: CommonJoin[];
  generated_at: string | null;
  is_user_reviewed: boolean;
  generation_model: string | null;
  // v2 fields
  relationships: RelationshipEdge[];
  derived_columns: DerivedColumn[];
  time_expressions: Record<string, string>;
  db_timezone: string | null;
  schema_hash: string | null;
  source_dialect: string;
  generation_warnings: string[];
  // Optional: the backend always sends these, but older cached payloads and
  // test fixtures may omit them.
  segments?: Segment[];
  notes?: string[];
  generation_status?: 'idle' | 'tables_partial' | 'complete';
  generation_progress?: {
    tables_done: number;
    tables_total: number;
    batch_size: number;
  } | null;
}

export interface TableSemantic {
  display_name: string;
  description: string | null;
  default_filters: string[];
  columns: Record<string, ColumnSemantic>;
  primary_timestamp_column?: string | null;
  primary_date_column?: string | null;
  grain?: string | null;
  domain?: string | null;
  // Generator-only — `TableSemanticUpdate` does not accept these, so the editor
  // shows them read-only.
  hierarchies?: Hierarchy[];
  partition_columns?: string[];
  cluster_columns?: string[];
  base_sql?: string | null;
}

export interface ColumnSemantic {
  display_name: string;
  description: string | null;
  value_mappings: ValueMapping[];
  is_sensitive: boolean;
  semantic_type?: string;
  cardinality?: string;
  currency?: string | null;
  unit?: string | null;
  default_aggregation?: AggregationType | null;
  is_non_additive?: boolean;
  time_granularity?: TimeGranularity | null;
}

export interface ValueMapping {
  raw_value: string;
  display_value: string;
  description: string | null;
}

export type MetricType = 'simple' | 'ratio' | 'derived' | 'cumulative' | 'conversion';

export type AggregationType =
  | 'sum'
  | 'count'
  | 'count_distinct'
  | 'count_distinct_approx'
  | 'average'
  | 'max'
  | 'min'
  | 'median';

/** Coarsest meaningful time grain for a DATE/TIMESTAMP column. */
export type TimeGranularity =
  | 'second'
  | 'minute'
  | 'hour'
  | 'day'
  | 'week'
  | 'month'
  | 'quarter'
  | 'year';

/**
 * Mirrors the backend `BusinessMetric` discriminated union (models.py), flattened:
 * `metric_type` is the discriminator and is required by the API — variant-specific
 * fields are optional here and validated server-side.
 */
export interface BusinessMetric {
  metric_type: MetricType;
  name: string;
  // Optional: `conversion` metrics carry no `definition` on the backend.
  definition?: string;
  description: string;
  filters: string[];
  related_tables: string[];
  format_hint?: string | null;
  is_non_additive?: boolean;
  non_additive_dimension?: string | null;
  // simple | cumulative
  aggregation?: AggregationType;
  measure_filters?: string[];
  // cumulative | conversion
  window?: string | null;
  // ratio
  numerator_expr?: string;
  denominator_expr?: string;
  // conversion
  base_measure?: string;
  conversion_measure?: string;
  entity?: string | null;
  calculation?: 'conversion_rate' | 'conversions';
}

export interface CommonJoin {
  description: string;
  tables: string[];
  join_pattern: string;
}

/** Mirrors `SemanticCorrection` in backend/app/schemas/chat.py — sent with thumbs-down. */
export interface SemanticCorrectionPayload {
  table_key: string;
  field: string;
  correction_type: 'add_value_mapping' | 'update_filter' | 'update_description';
  value: Record<string, unknown>;
}

/** Mirrors `SemanticSuggestionResponse` in backend/app/schemas/semantic.py. */
export interface SemanticSuggestionResponse {
  id: string;
  connection_id: string;
  table_key: string;
  field: string;
  correction_type: string;
  value: Record<string, unknown>;
  is_applied: boolean;
  source_message_id: string | null;
  created_at: string;
}

/** Mirrors `DriftReport` in backend/app/schemas/semantic.py. */
export interface DriftReport {
  connection_id: string;
  warnings: string[];
  warning_count: number;
  checked_at: string;
  /** "live": compared against a fresh introspection; "cache": the database was unreachable. */
  schema_source: 'live' | 'cache';
  schema_cached_at: string | null;
  refresh_error: string | null;
  /** True when a table or column was added or removed, or a column renamed — what Apply Drift acts on. */
  has_actionable_changes: boolean;
}

/** Mirrors `DriftApplySummary` in backend/app/semantic/drift.py. Every list is sorted. */
export interface DriftApplySummary {
  /**
   * A preview: nothing was written. `tables_added` are the tables, and `columns_added`
   * the new columns, the apply would send to the LLM, and `columns_renamed` the keys it
   * would rename. `relationships_added` includes the catalog edges, missing from the
   * model, on the new columns the apply would send, between tables the model already
   * holds. A relationship to a new table, and the LLM's items over the new tables, appear
   * only on apply.
   */
  dry_run: boolean;
  changed: boolean;
  tables_added: string[];
  tables_removed: string[];
  /**
   * Surviving table key → the columns added to it. On an apply, the ones the LLM
   * annotated and the model now stores; on a preview, the new columns the apply would
   * send to the LLM.
   */
  columns_added: Record<string, string[]>;
  /** Surviving table key → removed column names. */
  columns_removed: Record<string, string[]>;
  /**
   * Surviving table key → stored column key → the live column name it was renamed to: a
   * near-miss spelling (quoted, or in another letter case) of a column the model had
   * seen, renamed with its annotation, hand edits and references, with no LLM call. A
   * renamed column is in neither `columns_added` nor `columns_removed`.
   */
  columns_renamed: Record<string, Record<string, string>>;
  metrics_added: string[];
  metrics_removed: string[];
  joins_added: string[];
  joins_removed: string[];
  derived_columns_added: string[];
  derived_columns_removed: string[];
  segments_added: string[];
  segments_removed: string[];
  /** `from_table.from_column → to_table.to_column`. */
  relationships_added: string[];
  relationships_removed: string[];
  /** New tables the LLM returned nothing for; still missing from the model. */
  tables_not_annotated: string[];
  /**
   * Tables whose new columns this apply did not get: their batch failed or returned
   * nothing for them, or an earlier apply's mark was kept because this one did not
   * reach them. They still lack those columns and the next apply sends them last.
   */
  column_tables_failed: string[];
  warnings: string[];
}

/** Mirrors `ApplyDriftResponse` in backend/app/schemas/semantic.py. */
export interface ApplyDriftResponse {
  semantic_model: SemanticModel;
  summary: DriftApplySummary;
}

/** Mirrors `DriftApplyHistoryEntry` in backend/app/schemas/semantic.py. */
export interface DriftApplyHistoryEntry {
  applied_at: string;
  applied_by: string | null;
  summary: DriftApplySummary;
}

// ── Schema ──
export interface SchemaRelationship {
  from_schema: string;
  from_table: string;
  from_column: string;
  to_schema: string;
  to_table: string;
  to_column: string;
}

export interface SchemaResponse {
  source_type: string;
  tables: SchemaTable[];
  relationships: SchemaRelationship[];
  metadata: Record<string, unknown>;
}

export interface SchemaTable {
  catalog: string | null;
  schema_name: string;
  name: string;
  table_type: string;
  columns: SchemaColumn[];
  row_count_approx: number | null;
  description: string | null;
}

export interface SchemaColumn {
  name: string;
  data_type: string;
  native_type: string;
  nullable: boolean;
  is_primary_key: boolean;
  description: string | null;
  sample_values: string[] | null;
}

// ── Cache ──
export interface CacheStats {
  total_entries: number;
  hit_count: number;
  miss_count: number;
  hit_rate: number;
  top_cached_queries: { question: string; hit_count: number }[];
}

export interface CacheEntry {
  id: string;
  question_raw: string;
  query_dialect: string;
  hit_count: number;
  created_at: string;
  last_hit_at: string | null;
}

export interface VerifiedExample {
  id: string;
  question: string;
  query: string;
  query_dialect: string;
  created_at: string;
}

// ── App Settings ──
export interface AppSettings {
  app_name: string;
  debug: boolean;
  log_level: string;
  ollama_base_url: string;
  default_query_timeout: number;
  default_row_limit: number;
  cache_enabled: boolean;
  cache_max_age_days: number;
  semantic_similarity_threshold: number;
  embedding_model: string;
  db_pool_size: number;
  db_max_overflow: number;
  schema_pruning_enabled: boolean;
  schema_pruning_top_k: number;
  bcrypt_rounds: number;
}
