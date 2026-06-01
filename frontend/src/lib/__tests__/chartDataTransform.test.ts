// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { computeTrendLine } from '../chartDataTransform';

describe('computeTrendLine', () => {
  describe('linear regression', () => {
    it('fits a perfect line through monotonically increasing data', () => {
      const data = [{ v: 1 }, { v: 2 }, { v: 3 }, { v: 4 }, { v: 5 }];
      const result = computeTrendLine(data, 'v', 'linear', 3);
      expect(result[0].__trend__).toBeCloseTo(1, 4);
      expect(result[2].__trend__).toBeCloseTo(3, 4);
      expect(result[4].__trend__).toBeCloseTo(5, 4);
    });

    it('returns the mean when all values are equal (zero slope)', () => {
      const data = [{ v: 7 }, { v: 7 }, { v: 7 }];
      const result = computeTrendLine(data, 'v', 'linear', 3);
      result.forEach((row) => expect(row.__trend__).toBeCloseTo(7, 4));
    });

    it('handles a descending series', () => {
      const data = [{ v: 10 }, { v: 8 }, { v: 6 }, { v: 4 }];
      const result = computeTrendLine(data, 'v', 'linear', 3);
      // Slope should be -2
      expect((result[1].__trend__ as number) - (result[0].__trend__ as number)).toBeCloseTo(-2, 4);
    });

    it('does not mutate the original data objects', () => {
      const data = [{ v: 1 }, { v: 2 }];
      computeTrendLine(data, 'v', 'linear', 3);
      expect(data[0]).not.toHaveProperty('__trend__');
    });

    it('coerces string values to numbers', () => {
      const data = [{ v: '1' }, { v: '3' }, { v: '5' }] as Record<string, unknown>[];
      const result = computeTrendLine(data, 'v', 'linear', 3);
      expect(result[0].__trend__).toBeCloseTo(1, 4);
      expect(result[2].__trend__).toBeCloseTo(5, 4);
    });

    it('returns empty array for empty input', () => {
      expect(computeTrendLine([], 'v', 'linear', 3)).toEqual([]);
    });
  });

  describe('moving average', () => {
    it('computes trailing window averages correctly', () => {
      const data = [{ v: 1 }, { v: 2 }, { v: 3 }, { v: 4 }, { v: 5 }];
      const result = computeTrendLine(data, 'v', 'moving_avg', 3);
      expect(result[0].__trend__).toBeCloseTo(1);       // window=[1]
      expect(result[1].__trend__).toBeCloseTo(1.5);     // window=[1,2]
      expect(result[2].__trend__).toBeCloseTo(2);       // window=[1,2,3]
      expect(result[3].__trend__).toBeCloseTo(3);       // window=[2,3,4]
      expect(result[4].__trend__).toBeCloseTo(4);       // window=[3,4,5]
    });

    it('window=1 returns the values themselves', () => {
      const data = [{ v: 5 }, { v: 10 }, { v: 15 }];
      const result = computeTrendLine(data, 'v', 'moving_avg', 1);
      expect(result[0].__trend__).toBeCloseTo(5);
      expect(result[1].__trend__).toBeCloseTo(10);
      expect(result[2].__trend__).toBeCloseTo(15);
    });

    it('window larger than dataset uses all available points', () => {
      const data = [{ v: 2 }, { v: 4 }];
      const result = computeTrendLine(data, 'v', 'moving_avg', 10);
      expect(result[0].__trend__).toBeCloseTo(2);
      expect(result[1].__trend__).toBeCloseTo(3);
    });

    it('does not mutate the original data objects', () => {
      const data = [{ v: 1 }, { v: 2 }, { v: 3 }];
      computeTrendLine(data, 'v', 'moving_avg', 2);
      expect(data[0]).not.toHaveProperty('__trend__');
    });
  });
});
