// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Recharts renderer — driven by a UserChart config and pre-transformed data. */

import { type RefObject } from 'react';
import {
  AreaChart, Area,
  BarChart, Bar,
  LineChart, Line,
  PieChart, Pie,
  ScatterChart, Scatter,
  ComposedChart,
  RadialBarChart, RadialBar, Cell,
  LabelList,
  XAxis, YAxis,
  CartesianGrid, Tooltip, Legend,
  ResponsiveContainer,
} from 'recharts';
import { ArrowUp, ArrowDown, Minus } from 'lucide-react';

import type { QueryResults } from '../types';
import type { UserChart, LegendPosition, NumberFormat } from '../types/charts';
import { transformChartData, computeTrendLine } from '../lib/chartDataTransform';

const CHART_COLORS = [
  'hsl(var(--chart-1))',
  'hsl(var(--chart-2))',
  'hsl(var(--chart-3))',
  'hsl(var(--chart-4))',
  'hsl(var(--chart-5))',
];

function getColor(i: number): string {
  return CHART_COLORS[i % CHART_COLORS.length];
}

function legendProps(pos: LegendPosition) {
  if (pos === 'top')
    return {
      verticalAlign: 'top' as const,
      align: 'center' as const,
      layout: 'horizontal' as const,
    };
  if (pos === 'left')
    return {
      verticalAlign: 'middle' as const,
      align: 'left' as const,
      layout: 'vertical' as const,
    };
  if (pos === 'right')
    return {
      verticalAlign: 'middle' as const,
      align: 'right' as const,
      layout: 'vertical' as const,
    };
  return {
    verticalAlign: 'bottom' as const,
    align: 'center' as const,
    layout: 'horizontal' as const,
  };
}

function legendWrapperStyle(pos: LegendPosition) {
  if (pos === 'left' || pos === 'right') {
    return { fontSize: 12, maxWidth: 160, lineHeight: '18px' } as const;
  }
  return { fontSize: 12, paddingTop: 8, paddingBottom: 0, lineHeight: '18px' } as const;
}

const tooltipStyle = {
  backgroundColor: 'hsl(var(--popover))',
  border: '1px solid hsl(var(--border))',
  borderRadius: '6px',
  color: 'hsl(var(--popover-foreground))',
  fontSize: 12,
};

const axisProps = {
  tick: { fontSize: 11, fill: 'hsl(var(--muted-foreground))' },
  axisLine: { stroke: 'hsl(var(--border))' },
  tickLine: { stroke: 'hsl(var(--border))' },
};

function formatYTick(v: unknown): string {
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${+(n / 1_000_000_000).toPrecision(3)}B`;
  if (abs >= 1_000_000) return `${+(n / 1_000_000).toPrecision(3)}M`;
  if (abs >= 1_000) return `${+(n / 1_000).toPrecision(3)}K`;
  return String(n);
}

type DomainBound = number | string | ((v: number) => number);

function yAxisDomain(chart: UserChart): [DomainBound, DomainBound] {
  const max: DomainBound = chart.yAxisMax ?? 'auto';
  if (chart.yAxisMin !== null && chart.yAxisMin !== undefined) {
    return [chart.yAxisMin, max];
  }
  // Bar charts must include 0 as their baseline.
  if (chart.type === 'bar') return [0, max];
  // For all other series types, pad below the data minimum so values aren't
  // flush against the axis edge. Padding = 10 % of |dataMin|, at least 1 unit,
  // and the result is floored at 0 when all data is positive.
  return [
    (dataMin: number) => {
      if (dataMin <= 0) return dataMin;
      const padding = Math.max(1, Math.abs(dataMin) * 0.1);
      return Math.max(0, dataMin - padding);
    },
    max,
  ];
}

function formatNumberValue(value: number, format: NumberFormat, currencySymbol: string): string {
  switch (format) {
    case 'integer':
      return Math.round(value).toLocaleString();
    case 'currency':
      return `${currencySymbol}${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    case 'percent':
      return `${value.toFixed(1)}%`;
    default:
      return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
}

const trendLineProps = {
  stroke: 'hsl(var(--chart-trend))',
  strokeWidth: 2,
  strokeDasharray: '4 2',
  dot: false as const,
  activeDot: false as const,
  name: 'Trend',
  legendType: 'none' as const,
} as const;

interface Props {
  results: QueryResults;
  chart: UserChart;
  containerRef: RefObject<HTMLDivElement>;
  animate?: boolean;
}

export default function ChartView({ results, chart, containerRef, animate = true }: Props) {
  const { data: rawData, yKeys } = transformChartData(results, chart);

  const showTrend =
    chart.showTrendLine &&
    yKeys.length > 0 &&
    (chart.type === 'bar' || chart.type === 'line' || chart.type === 'area' || chart.type === 'combo');

  const data = showTrend
    ? computeTrendLine(rawData, yKeys[0], chart.trendLineType ?? 'linear', chart.movingAvgWindow ?? 3)
    : rawData;

  const legendConfig = chart.showLegend ? legendProps(chart.legendPosition) : undefined;
  const legendStyle = chart.showLegend ? legendWrapperStyle(chart.legendPosition) : undefined;

  // ── Number / KPI card ──────────────────────────────────────────────────────
  if (chart.type === 'number') {
    const raw = data[0]?.[yKeys[0]];
    const value = typeof raw === 'number' ? raw : parseFloat(String(raw ?? '0'));
    const formatted = isNaN(value)
      ? '—'
      : formatNumberValue(value, chart.numberFormat ?? 'decimal', chart.currencySymbol ?? '$');

    const comparison = chart.comparisonValue;
    const delta = comparison !== null ? value - comparison : null;
    const trendDir = delta !== null ? (delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat') : null;

    return (
      <div ref={containerRef} className="flex flex-col items-center justify-center gap-3 py-12 px-4">
        {chart.title && (
          <p className="text-sm font-medium text-muted-foreground">{chart.title}</p>
        )}
        <p className="text-6xl font-bold tabular-nums tracking-tight text-foreground">
          {formatted}
        </p>
        {comparison !== null && delta !== null && (
          <div className="flex items-center gap-1.5 text-sm">
            {trendDir === 'up' && (
              <ArrowUp className="h-4 w-4" style={{ color: 'hsl(var(--number-positive))' }} />
            )}
            {trendDir === 'down' && (
              <ArrowDown className="h-4 w-4" style={{ color: 'hsl(var(--number-negative))' }} />
            )}
            {trendDir === 'flat' && (
              <Minus className="h-4 w-4 text-muted-foreground" />
            )}
            <span
              style={{
                color:
                  trendDir === 'up'
                    ? 'hsl(var(--number-positive))'
                    : trendDir === 'down'
                      ? 'hsl(var(--number-negative))'
                      : undefined,
              }}
              className={trendDir === 'flat' ? 'text-muted-foreground' : ''}
            >
              {formatNumberValue(
                Math.abs(delta),
                chart.numberFormat ?? 'decimal',
                chart.currencySymbol ?? '$',
              )}
            </span>
            {chart.comparisonLabel && (
              <span className="text-muted-foreground">{chart.comparisonLabel}</span>
            )}
          </div>
        )}
      </div>
    );
  }

  // ── Gauge / radial progress ────────────────────────────────────────────────
  if (chart.type === 'gauge') {
    const raw = data[0]?.[yKeys[0]];
    const value = typeof raw === 'number' ? raw : parseFloat(String(raw ?? '0'));
    const gaugeMin = chart.gaugeMin ?? 0;
    const gaugeMax = chart.gaugeMax ?? 100;
    const clampedPct = isNaN(value)
      ? 0
      : Math.max(0, Math.min(100, ((value - gaugeMin) / (gaugeMax - gaugeMin)) * 100));

    // Determine arc colour from thresholds (highest threshold exceeded wins)
    let fillColor = getColor(0);
    if (chart.gaugeThresholds.length > 0) {
      const sorted = [...chart.gaugeThresholds].sort((a, b) => a.value - b.value);
      for (const t of sorted) {
        if (value >= t.value) fillColor = t.color;
      }
    }

    const formatted = isNaN(value)
      ? '—'
      : formatNumberValue(value, chart.numberFormat ?? 'decimal', chart.currencySymbol ?? '$');

    return (
      <div ref={containerRef} className="w-full px-4 py-4">
        {chart.title && (
          <p className="mb-2 text-center text-sm font-medium text-foreground">{chart.title}</p>
        )}
        <div className="relative mx-auto" style={{ maxWidth: 320 }}>
          <ResponsiveContainer width="100%" height={180}>
            <RadialBarChart
              cx="50%"
              cy="90%"
              innerRadius="65%"
              outerRadius="110%"
              startAngle={180}
              endAngle={0}
              data={[{ value: clampedPct }]}
            >
              <RadialBar
                dataKey="value"
                cornerRadius={4}
                background={{ fill: 'hsl(var(--muted))' }}
                isAnimationActive={animate}
              >
                <Cell fill={fillColor} />
              </RadialBar>
            </RadialBarChart>
          </ResponsiveContainer>
          {/* Centred value overlay — positioned above the flat edge of the semicircle */}
          <div className="absolute inset-x-0 bottom-0 flex flex-col items-center pb-2">
            <p className="text-3xl font-bold tabular-nums text-foreground">{formatted}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {gaugeMin} – {gaugeMax}
            </p>
          </div>
        </div>
      </div>
    );
  }

  // ── All other chart types — require xKey + at least one yKey ──────────────
  if (!chart.xKey || yKeys.length === 0) {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
        Select X-axis and at least one Y-axis column to display the chart.
      </div>
    );
  }

  const isPie = chart.type === 'pie';
  const sideLegend =
    chart.showLegend && (chart.legendPosition === 'left' || chart.legendPosition === 'right');
  const chartHeight = isPie ? (sideLegend ? 380 : 440) : 320;

  const pieOuterRadius = sideLegend ? '65%' : '55%';
  const pieCx =
    chart.legendPosition === 'left' ? '62%' : chart.legendPosition === 'right' ? '38%' : '50%';
  const pieMargin = sideLegend
    ? { top: 12, right: 12, bottom: 12, left: 12 }
    : { top: 20, right: 40, bottom: 24, left: 40 };

  const xAxisAngle = chart.xAxisAngle ?? 0;
  const xAxisHeight = xAxisAngle === -90 ? 90 : xAxisAngle !== 0 ? 65 : undefined;
  const xAxisTickProps = xAxisAngle !== 0
    ? { ...axisProps.tick, angle: xAxisAngle, textAnchor: 'end' as const, dy: 4 }
    : axisProps.tick;
  const xAxisInterval = xAxisAngle !== 0 ? 0 : undefined;

  const commonYAxisProps = {
    ...axisProps,
    width: 60,
    scale: chart.yAxisScale ?? 'linear',
    domain: yAxisDomain(chart),
    tickFormatter: formatYTick,
  } as const;

  const commonXAxisProps = {
    dataKey: chart.xKey,
    ...axisProps,
    tick: xAxisTickProps,
    height: xAxisHeight,
    interval: xAxisInterval,
  } as const;

  const trendLine = showTrend ? (
    <Line key="__trend__" dataKey="__trend__" {...trendLineProps} isAnimationActive={animate} />
  ) : null;

  return (
    <div ref={containerRef} className="w-full px-4 py-4">
      {chart.title && (
        <p className="mb-2 text-center text-sm font-medium text-foreground">{chart.title}</p>
      )}
      <ResponsiveContainer width="100%" height={chartHeight}>
        {chart.type === 'scatter' ? (
          <ScatterChart>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis type="number" dataKey={chart.xKey} name={chart.xKey} {...axisProps} />
            <YAxis type="number" dataKey={yKeys[0]} name={yKeys[0]} {...axisProps} width={60} />
            <Tooltip contentStyle={tooltipStyle} cursor={{ strokeDasharray: '3 3' }} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            <Scatter name={chart.title || yKeys[0]} data={data} fill={getColor(0)} isAnimationActive={animate} />
          </ScatterChart>
        ) : isPie ? (
          <PieChart margin={pieMargin}>
            <Pie
              data={data.map((d, i) => ({ ...d, fill: getColor(i) }))}
              dataKey={yKeys[0]}
              nameKey={chart.xKey}
              cx={pieCx}
              cy="50%"
              outerRadius={pieOuterRadius}
              label={({ percent }) => {
                const pct = ((percent as number | undefined) ?? 0) * 100;
                return pct >= 4 ? `${pct.toFixed(0)}%` : '';
              }}
              labelLine={{ stroke: 'hsl(var(--muted-foreground))', strokeWidth: 1 }}
              isAnimationActive={animate}
            />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
          </PieChart>
        ) : chart.type === 'bar' && showTrend ? (
          // Bar + trend line requires ComposedChart to mix bar and line series
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis {...commonXAxisProps} />
            <YAxis {...commonYAxisProps} />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            {yKeys.map((key, i) => (
              <Bar
                key={key}
                dataKey={key}
                fill={getColor(i)}
                radius={chart.stacked ? undefined : [3, 3, 0, 0]}
                stackId={chart.stacked ? 'stack' : undefined}
                isAnimationActive={animate}
              >
                {chart.showDataLabels && (
                  <LabelList
                    dataKey={key}
                    position={chart.stacked ? 'center' : 'top'}
                    style={{ fontSize: 10, fill: chart.stacked ? 'hsl(var(--background))' : 'hsl(var(--foreground))' }}
                    formatter={formatYTick}
                  />
                )}
              </Bar>
            ))}
            {trendLine}
          </ComposedChart>
        ) : chart.type === 'bar' ? (
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis {...commonXAxisProps} />
            <YAxis {...commonYAxisProps} />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            {yKeys.map((key, i) => (
              <Bar
                key={key}
                dataKey={key}
                fill={getColor(i)}
                radius={chart.stacked ? undefined : [3, 3, 0, 0]}
                stackId={chart.stacked ? 'stack' : undefined}
                isAnimationActive={animate}
              >
                {chart.showDataLabels && (
                  <LabelList
                    dataKey={key}
                    position={chart.stacked ? 'center' : 'top'}
                    style={{ fontSize: 10, fill: chart.stacked ? 'hsl(var(--background))' : 'hsl(var(--foreground))' }}
                    formatter={formatYTick}
                  />
                )}
              </Bar>
            ))}
          </BarChart>
        ) : chart.type === 'area' ? (
          <AreaChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis {...commonXAxisProps} />
            <YAxis {...commonYAxisProps} />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            {yKeys.map((key, i) => (
              <Area
                key={key}
                type="monotone"
                dataKey={key}
                stroke={getColor(i)}
                fill={getColor(i)}
                fillOpacity={0.15}
                strokeWidth={2}
                stackId={chart.stacked ? 'stack' : undefined}
                connectNulls={chart.connectNulls}
                isAnimationActive={animate}
              />
            ))}
            {trendLine}
          </AreaChart>
        ) : chart.type === 'combo' ? (
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis {...commonXAxisProps} />
            <YAxis {...commonYAxisProps} />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            {yKeys.map((key, i) => {
              const seriesType = (chart.comboSeriesTypes ?? {})[key] ?? (i === 0 ? 'bar' : 'line');
              return seriesType === 'bar' ? (
                <Bar
                  key={key}
                  dataKey={key}
                  fill={getColor(i)}
                  radius={chart.stacked ? undefined : [3, 3, 0, 0]}
                  stackId={chart.stacked ? 'stack' : undefined}
                  isAnimationActive={animate}
                >
                  {chart.showDataLabels && (
                    <LabelList
                      dataKey={key}
                      position={chart.stacked ? 'center' : 'top'}
                      style={{ fontSize: 10, fill: chart.stacked ? 'hsl(var(--background))' : 'hsl(var(--foreground))' }}
                      formatter={formatYTick}
                    />
                  )}
                </Bar>
              ) : (
                <Line
                  key={key}
                  type="monotone"
                  dataKey={key}
                  stroke={getColor(i)}
                  strokeWidth={2}
                  dot={data.length <= 60}
                  activeDot={{ r: 4 }}
                  connectNulls={chart.connectNulls}
                  isAnimationActive={animate}
                />
              );
            })}
            {trendLine}
          </ComposedChart>
        ) : (
          /* line */
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis {...commonXAxisProps} />
            <YAxis {...commonYAxisProps} />
            <Tooltip contentStyle={tooltipStyle} />
            {legendConfig && <Legend wrapperStyle={legendStyle} {...legendConfig} />}
            {yKeys.map((key, i) => (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                stroke={getColor(i)}
                strokeWidth={2}
                dot={data.length <= 60}
                activeDot={{ r: 4 }}
                connectNulls={chart.connectNulls}
                isAnimationActive={animate}
              />
            ))}
            {trendLine}
          </LineChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
