# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Semantic model — business context layered on top of schema.

All classes are Pydantic v2 ``BaseModel`` subclasses so the model can be
constructed, validated, serialised, and deserialised through one canonical
code path. The previous hand-rolled ``_semantic_model_from_dict`` deserializer
in ``chat_service`` has been replaced by ``SemanticModel.model_validate``.
"""

from __future__ import annotations

from enum import StrEnum
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    """Shared Pydantic config for every semantic-model class.

    - ``extra="ignore"`` lets stale fields be dropped silently when reading
      old JSONB rows (post-migration safeguard).
    - ``use_enum_values=False`` keeps ``StrEnum`` instances live in memory so
      isinstance / equality checks work; serialisation still emits strings.
    - ``populate_by_name=True`` allows constructing by the canonical field
      name even when an alias is later introduced.
    """

    model_config = ConfigDict(
        extra="ignore",
        use_enum_values=False,
        populate_by_name=True,
    )


# ── Enums ──────────────────────────────────────────────────────────────────────


class SemanticType(StrEnum):
    IDENTIFIER = "identifier"  # primary/foreign key
    STATUS_FLAG = "status_flag"  # coded status/type column
    MONETARY = "monetary"  # currency amount
    PERCENTAGE = "percentage"  # 0-1 or 0-100 ratio
    TIMESTAMP = "timestamp"  # datetime
    DATE = "date"  # date only
    FREE_TEXT = "free_text"  # long text, not categorical
    CATEGORICAL = "categorical"  # low cardinality, filterable
    BOOLEAN_FLAG = "boolean_flag"  # true/false
    FOREIGN_KEY = "foreign_key"  # references another table
    URL = "url"  # web URL
    EMAIL = "email"  # email address
    PHONE = "phone"  # phone number
    MEASUREMENT = "measurement"  # physical quantity with unit
    COUNT = "count"  # integer count/quantity
    UNKNOWN = "unknown"


class CardinalityClass(StrEnum):
    UNIQUE = "unique"  # n_distinct = -1 (all distinct)
    HIGH = "high"  # n_distinct > 100 or fraction > 0.5
    MEDIUM = "medium"  # n_distinct 10-100
    LOW = "low"  # n_distinct < 10 (good for filtering/grouping)


class RelationshipType(StrEnum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"


class FormatHint(StrEnum):
    CURRENCY_EUR = "currency_eur"
    CURRENCY_USD = "currency_usd"
    CURRENCY_GBP = "currency_gbp"
    PERCENTAGE = "percentage"
    INTEGER = "integer"


class AggregationType(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    COUNT_DISTINCT_APPROX = "count_distinct_approx"  # HyperLogLog approx distinct
    AVERAGE = "average"
    MAX = "max"
    MIN = "min"
    MEDIAN = "median"


class MetricType(StrEnum):
    SIMPLE = "simple"  # single aggregation over one column
    DERIVED = "derived"  # formula combining other metrics
    RATIO = "ratio"  # numerator_expr / denominator_expr
    CUMULATIVE = "cumulative"  # running total / window function
    CONVERSION = "conversion"  # funnel / event-based conversion rate


class EntityType(StrEnum):
    PRIMARY = "primary"  # PK / dimension side
    UNIQUE = "unique"  # alternate key (non-PK unique column)
    FOREIGN = "foreign"  # FK / fact side
    NATURAL = "natural"  # business key, may not be unique


class TimeGranularity(StrEnum):
    """Coarsest meaningful time grain for a DATE or TIMESTAMP column."""

    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class GenerationStatus(StrEnum):
    IDLE = "idle"
    TABLES_PARTIAL = "tables_partial"
    COMPLETE = "complete"


# ── Leaf models ────────────────────────────────────────────────────────────────


class ValueMapping(_Base):
    """Maps a raw coded value to a human-readable display value."""

    raw_value: str
    display_value: str
    description: str | None = None

    @field_validator("raw_value", mode="before")
    @classmethod
    def coerce_to_str(cls, v: object) -> str:
        return str(v) if not isinstance(v, str) else v


class ColumnSemantic(_Base):
    """Business context for a single column."""

    display_name: str
    description: str | None = None
    value_mappings: list[ValueMapping] = Field(default_factory=list)
    is_sensitive: bool = False
    semantic_type: SemanticType = SemanticType.UNKNOWN
    cardinality: CardinalityClass | None = None
    currency: str | None = None  # 'EUR', 'USD' — for MONETARY columns
    unit: str | None = None  # 'kg', 'km', 'seconds' — for MEASUREMENT
    default_aggregation: AggregationType | None = None  # prescribed agg for this column
    is_non_additive: bool = False  # never SUM this column across dimensions
    time_granularity: TimeGranularity | None = None  # coarsest grain for DATE/TIMESTAMP columns

    @field_validator("value_mappings", mode="before")
    @classmethod
    def _coerce_value_mappings(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        coerced = []
        for item in v:
            if isinstance(item, str):
                coerced.append({"raw_value": item, "display_value": item})
            else:
                coerced.append(item)
        return coerced

    @field_validator("semantic_type", mode="before")
    @classmethod
    def _coerce_semantic_type(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in SemanticType._value2member_map_:
            return SemanticType.UNKNOWN
        return v

    @field_validator("default_aggregation", mode="before")
    @classmethod
    def _coerce_aggregation(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and v not in AggregationType._value2member_map_:
            return None
        return v

    @field_validator("time_granularity", mode="before")
    @classmethod
    def _coerce_time_granularity(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and v not in TimeGranularity._value2member_map_:
            return None
        return v


class Hierarchy(_Base):
    """An ordered drill-down path within a table (from Cube.js).

    Communicates valid GROUP BY progressions so the LLM doesn't mix grain
    levels in a single aggregation.
    """

    name: str  # "Date Hierarchy"
    levels: list[str]  # ["year", "quarter", "month", "week", "day"]


class TableSemantic(_Base):
    """Business context for a single table."""

    display_name: str
    description: str | None = None
    default_filters: list[str] = Field(default_factory=list)
    columns: dict[str, ColumnSemantic] = Field(default_factory=dict)
    primary_timestamp_column: str | None = None  # 'created_at', 'ordered_at'
    primary_date_column: str | None = None  # 'order_date', 'invoice_date'
    grain: str | None = None  # e.g. "one row per order"
    hierarchies: list[Hierarchy] = Field(default_factory=list)
    # Partition/cluster columns — LLM is told to always filter on these to avoid full-scan costs.
    partition_columns: list[str] = Field(default_factory=list)
    cluster_columns: list[str] = Field(default_factory=list)
    # For views: the underlying SQL expression. LLM uses this as a CTE/subquery reference.
    base_sql: str | None = None
    # Optional business domain tag (e.g. "finance", "marketing", "ops"). When set,
    # schema pruning applies a score boost to co-domain tables to keep related
    # tables together in the LLM context window.
    domain: str | None = None

    @field_validator("columns", mode="before")
    @classmethod
    def _filter_columns(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {k: col for k, col in v.items() if isinstance(col, (dict, BaseModel))}

    @field_validator("default_filters", mode="before")
    @classmethod
    def _coerce_filters(cls, v: Any) -> list:
        if not isinstance(v, list):
            return []
        return [f if isinstance(f, str) else json.dumps(f) for f in v]

    @field_validator("hierarchies", mode="before")
    @classmethod
    def _filter_hierarchies(cls, v: Any) -> list:
        if not isinstance(v, list):
            return []
        return [h for h in v if isinstance(h, BaseModel) or (isinstance(h, dict) and h.get("name"))]


class CommonJoin(_Base):
    """A frequently-used join pattern between two or more tables."""

    description: str
    tables: list[str] = Field(default_factory=list)
    join_pattern: str  # e.g. "customers.id = orders.customer_id"


class Segment(_Base):
    """A named, reusable WHERE-clause fragment (from Cube.js).

    The LLM references these by name instead of regenerating complex filter
    logic, preventing inconsistent predicate spelling across queries.
    """

    name: str  # "active_customers"
    sql_expression: str  # "status = 'active' AND deleted_at IS NULL"
    description: str = ""
    applicable_tables: list[str] = Field(default_factory=list)


class DerivedColumn(_Base):
    """A calculated field defined in business terms.

    The LLM is instructed to use the exact ``sql_expression`` when asked
    about this concept, guaranteeing consistent calculation.
    """

    name: str  # 'Gross Margin %'
    sql_expression: str  # '(base_price - cost_price) / base_price * 100'
    base_tables: list[str] = Field(default_factory=list)  # ['ecommerce.products']
    description: str = ""
    format_hint: FormatHint | None = None
    available_on: list[str] = Field(default_factory=list)

    @field_validator("format_hint", mode="before")
    @classmethod
    def _coerce_format_hint(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and v not in FormatHint._value2member_map_:
            return None
        return v


class RelationshipEdge(_Base):
    """A formal FK-backed relationship between two tables.

    Sourced exclusively from ``information_schema`` — never inferred from data.
    """

    from_table: str  # 'ecommerce.orders'
    from_column: str  # 'customer_id'
    to_table: str  # 'ecommerce.customers'
    to_column: str  # 'id'
    join_sql: str  # 'ecommerce.orders.customer_id = ecommerce.customers.id'
    relationship_type: RelationshipType = RelationshipType.MANY_TO_ONE
    # ``True`` ⇒ FK column is NOT NULL, so INNER JOIN is safe in either
    # direction. ``False`` ⇒ FK is nullable, must LEFT JOIN to preserve rows.
    is_required: bool = False
    description: str | None = None
    entity_type: EntityType | None = None  # PRIMARY = PK/dim side, FOREIGN = FK/fact side


# ── Discriminated union: BusinessMetric ────────────────────────────────────────


class _MetricCommon(_Base):
    """Fields shared by every metric variant."""

    name: str
    description: str = ""
    filters: list[str] = Field(default_factory=list)
    related_tables: list[str] = Field(default_factory=list)
    format_hint: FormatHint | None = None
    is_non_additive: bool = False
    non_additive_dimension: str | None = None

    @field_validator("filters", mode="before")
    @classmethod
    def _coerce_filters(cls, v: Any) -> list:
        if not isinstance(v, list):
            return []
        return [f if isinstance(f, str) else json.dumps(f) for f in v]

    @field_validator("format_hint", mode="before")
    @classmethod
    def _coerce_format_hint(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and v not in FormatHint._value2member_map_:
            return None
        return v


class SimpleMetric(_MetricCommon):
    """Single aggregation over one expression: e.g. ``SUM(orders.total)``."""

    metric_type: Literal[MetricType.SIMPLE] = MetricType.SIMPLE
    definition: str
    aggregation: AggregationType
    measure_filters: list[str] = Field(default_factory=list)  # pre-agg FILTER (WHERE ...)


class RatioMetric(_MetricCommon):
    """Numerator / denominator pair, rendered as ``(num) / NULLIF(den, 0)``."""

    metric_type: Literal[MetricType.RATIO] = MetricType.RATIO
    numerator_expr: str
    denominator_expr: str
    # Convenience: a precomputed string form of the ratio. Optional — the
    # formatter renders the numerator/denominator pair directly.
    definition: str = ""


class DerivedMetric(_MetricCommon):
    """A formula combining other metrics; aggregation lives inside ``definition``."""

    metric_type: Literal[MetricType.DERIVED] = MetricType.DERIVED
    definition: str


class CumulativeMetric(_MetricCommon):
    """A running total / window function; ``window`` describes the OVER clause."""

    metric_type: Literal[MetricType.CUMULATIVE] = MetricType.CUMULATIVE
    definition: str
    aggregation: AggregationType
    window: str | None = None
    measure_filters: list[str] = Field(default_factory=list)  # pre-agg FILTER (WHERE ...)


class ConversionMetric(_MetricCommon):
    """Event-funnel metric: fraction of a base event that leads to a conversion event.

    Renders as: ``conversion_measure / base_measure [per entity] [within window]``.
    The LLM is expected to write the funnel SQL using a self-join or window function
    depending on the dialect.
    """

    metric_type: Literal[MetricType.CONVERSION] = MetricType.CONVERSION
    base_measure: str  # e.g. "sessions"
    conversion_measure: str  # e.g. "signups"
    entity: str | None = None  # join column linking the two events, e.g. "user_id"
    window: str | None = None  # time window, e.g. "7 days"
    calculation: Literal["conversion_rate", "conversions"] = "conversion_rate"


BusinessMetric = Annotated[
    SimpleMetric | RatioMetric | DerivedMetric | CumulativeMetric | ConversionMetric,
    Field(discriminator="metric_type"),
]


# ── Top-level container ────────────────────────────────────────────────────────


class GenerationProgress(_Base):
    """Tracks long-running phased generation across multiple LLM calls."""

    tables_done: int = 0
    tables_total: int = 0
    batch_size: int = 0


class SemanticModel(_Base):
    """Complete semantic overlay for a database connection.

    ``tables`` keys use ``"schema.table"`` notation (e.g. ``"public.orders"``).
    ``generated_at`` is an ISO-8601 string set by the generator.
    ``generation_model`` records which LLM produced this model.
    """

    tables: dict[str, TableSemantic] = Field(default_factory=dict)
    business_metrics: list[BusinessMetric] = Field(default_factory=list)
    common_joins: list[CommonJoin] = Field(default_factory=list)
    derived_columns: list[DerivedColumn] = Field(default_factory=list)
    relationships: list[RelationshipEdge] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    # Free-form cross-table domain notes injected verbatim at the top of every
    # prompt. Captures facts that cannot be expressed as table/column metadata
    # — e.g. "finance.budgets is quarterly; for monthly revenue use
    # finance.transactions". Partially auto-populated by the generator;
    # extended by users via PUT /semantic.
    notes: list[str] = Field(default_factory=list)
    time_expressions: dict[str, str] = Field(default_factory=dict)
    db_timezone: str | None = None
    schema_hash: str | None = None
    source_dialect: str = "sql"
    generation_warnings: list[str] = Field(default_factory=list)
    generation_status: GenerationStatus = GenerationStatus.IDLE
    generation_progress: GenerationProgress | None = None
    generated_at: str | None = None
    is_user_reviewed: bool = False
    generation_model: str | None = None


# ── Structured-output response envelopes (used by generate_structured) ─────────


class TablesBatchResponse(_Base):
    """LLM response envelope for one batch of table annotations."""

    tables: dict[str, TableSemantic] = Field(default_factory=dict)

    @field_validator("tables", mode="before")
    @classmethod
    def _filter_tables(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {k: tbl for k, tbl in v.items() if isinstance(tbl, dict)}


_VALID_METRIC_TYPES: frozenset[str] = frozenset(
    {"simple", "derived", "ratio", "cumulative", "conversion"}
)
_VALID_AGGREGATIONS: frozenset[str] = frozenset(
    {"sum", "count", "count_distinct", "count_distinct_approx", "average", "max", "min", "median"}
)


class GlobalSectionsResponse(_Base):
    """LLM response envelope for cross-table business context sections."""

    business_metrics: list[BusinessMetric] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    derived_columns: list[DerivedColumn] = Field(default_factory=list)
    common_joins: list[CommonJoin] = Field(default_factory=list)

    @field_validator("business_metrics", mode="before")
    @classmethod
    def _filter_metrics(cls, raw: Any) -> list:
        """Pre-filter LLM-emitted metric dicts to only those that will pass union validation.

        Mirrors the silent-skip logic from the old _parse_metrics helper.
        """
        if not isinstance(raw, list):
            return []
        result: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if not (item.get("name") and item.get("description")):
                continue
            mtype = str(item.get("metric_type", "simple"))
            if mtype not in _VALID_METRIC_TYPES:
                item = {**item, "metric_type": "simple"}
                mtype = "simple"
            if mtype in ("simple", "cumulative"):
                if not item.get("definition"):
                    continue
                agg = str(item.get("aggregation", ""))
                if agg not in _VALID_AGGREGATIONS:
                    item = {**item, "aggregation": "count"}
            elif mtype == "ratio":
                if not (item.get("numerator_expr") and item.get("denominator_expr")):
                    continue
            elif mtype == "derived":
                if not item.get("definition"):
                    continue
            elif mtype == "conversion":
                if not (item.get("base_measure") and item.get("conversion_measure")):
                    continue
            result.append(item)
        return result

    @field_validator("segments", mode="before")
    @classmethod
    def _filter_segments(cls, raw: Any) -> list:
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, dict) and i.get("name") and i.get("sql_expression")]

    @field_validator("derived_columns", mode="before")
    @classmethod
    def _filter_derived(cls, raw: Any) -> list:
        if not isinstance(raw, list):
            return []
        return [i for i in raw if isinstance(i, dict) and i.get("name") and i.get("sql_expression")]

    @field_validator("common_joins", mode="before")
    @classmethod
    def _filter_joins(cls, raw: Any) -> list:
        if not isinstance(raw, list):
            return []
        return [
            i for i in raw if isinstance(i, dict) and i.get("description") and i.get("join_pattern")
        ]
