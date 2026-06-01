// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { suggestChart } from '../chartDetection';
import { makeQueryResults } from '../../test/factories';

describe('suggestChart', () => {
  describe('Number card detection', () => {
    it('suggests number for a single row with a single numeric column', () => {
      const results = makeQueryResults({
        columns: ['revenue'],
        column_types: ['numeric'],
        rows: [[42000]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('number');
      expect(suggestChart(results).yKeys).toEqual(['revenue']);
    });

    it('suggests number when there is also a label column alongside the numeric', () => {
      const results = makeQueryResults({
        columns: ['label', 'total'],
        column_types: ['text', 'integer'],
        rows: [['Q1', 500]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('number');
    });
  });

  describe('Gauge detection', () => {
    it('suggests gauge for a rate-named column with a single row', () => {
      const results = makeQueryResults({
        columns: ['success_rate'],
        column_types: ['float'],
        rows: [[0.87]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('gauge');
    });

    it('suggests gauge for a percent-named column', () => {
      const results = makeQueryResults({
        columns: ['completion_percent'],
        column_types: ['float'],
        rows: [[72]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('gauge');
    });

    it('suggests gauge for a score-named column', () => {
      const results = makeQueryResults({
        columns: ['nps_score'],
        column_types: ['integer'],
        rows: [[45]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('gauge');
    });

    it('suggests gauge for a utilisation-named column', () => {
      const results = makeQueryResults({
        columns: ['cpu_utilisation'],
        column_types: ['float'],
        rows: [[0.62]],
        row_count: 1,
      });
      expect(suggestChart(results).type).toBe('gauge');
    });
  });

  describe('Number / gauge defaults', () => {
    it('gauge defaults include gaugeMin=0 and gaugeMax=100', () => {
      const results = makeQueryResults({
        columns: ['error_rate'],
        column_types: ['float'],
        rows: [[0.03]],
        row_count: 1,
      });
      const suggestion = suggestChart(results);
      expect(suggestion.gaugeMin).toBe(0);
      expect(suggestion.gaugeMax).toBe(100);
    });

    it('number default includes numberFormat=decimal', () => {
      const results = makeQueryResults({
        columns: ['revenue'],
        column_types: ['numeric'],
        rows: [[1234]],
        row_count: 1,
      });
      const suggestion = suggestChart(results);
      expect(suggestion.numberFormat).toBe('decimal');
    });

    it('returns xKey as empty string for scalar types', () => {
      const results = makeQueryResults({
        columns: ['total'],
        column_types: ['integer'],
        rows: [[99]],
        row_count: 1,
      });
      expect(suggestChart(results).xKey).toBe('');
    });
  });

  describe('Time series and bar fallbacks', () => {
    it('suggests line for a date + numeric pair with multiple rows', () => {
      const results = makeQueryResults({
        columns: ['created_at', 'revenue'],
        column_types: ['timestamp', 'numeric'],
        rows: [['2024-01-01', 100], ['2024-02-01', 200]],
        row_count: 2,
      });
      expect(suggestChart(results).type).toBe('line');
    });

    it('suggests bar for a label + numeric pair with multiple rows', () => {
      const results = makeQueryResults({
        columns: ['region', 'revenue'],
        column_types: ['text', 'numeric'],
        rows: [['North', 100], ['South', 200]],
        row_count: 2,
      });
      expect(suggestChart(results).type).toBe('bar');
    });

    it('all returned charts include trend line defaults', () => {
      const results = makeQueryResults({
        columns: ['region', 'revenue'],
        column_types: ['text', 'numeric'],
        rows: [['A', 1], ['B', 2]],
        row_count: 2,
      });
      const s = suggestChart(results);
      expect(s.showTrendLine).toBe(false);
      expect(s.trendLineType).toBe('linear');
      expect(s.movingAvgWindow).toBe(3);
    });
  });
});
