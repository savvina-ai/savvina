// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { beforeAll, describe, it, expect, vi } from 'vitest';
import {
  MAX_X_AXIS_HEIGHT,
  computeXAxisLayout,
  estimateTextWidth,
  truncateToWidth,
} from '../chartAxisLayout';

const FONT = 11;

// jsdom has no canvas; force the deterministic character-count fallback and keep stderr quiet.
beforeAll(() => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

describe('truncateToWidth', () => {
  it('leaves a label that already fits untouched', () => {
    expect(truncateToWidth('Short', 500, FONT)).toBe('Short');
  });

  it('shortens an over-long label with an ellipsis that fits the width', () => {
    const label = 'Kids Educational Tablet With A Very Long Name';
    const out = truncateToWidth(label, 80, FONT);
    expect(out.endsWith('…')).toBe(true);
    expect(out.length).toBeLessThan(label.length);
    expect(estimateTextWidth(out, FONT)).toBeLessThanOrEqual(80);
  });
});

describe('computeXAxisLayout', () => {
  const labels = ['TechPro Laptop Pro 15', 'Home Desk Lamp LED', 'Kids Educational Tablet'];

  it('returns no overrides for horizontal labels', () => {
    expect(computeXAxisLayout(labels, 0, FONT)).toEqual({
      height: undefined,
      maxLabelWidth: Infinity,
      leftOverflow: 0,
    });
  });

  it('is tall enough for the longest rotated label at -45°', () => {
    const { height } = computeXAxisLayout(labels, -45, FONT);
    const longest = Math.max(...labels.map((l) => estimateTextWidth(l, FONT)));
    expect(height).toBeGreaterThanOrEqual(Math.ceil(longest * Math.SQRT1_2));
  });

  it('is tall enough for the longest rotated label at -90°', () => {
    const shortLabels = ['Home Desk Lamp', 'Kids Tablet'];
    const { height } = computeXAxisLayout(shortLabels, -90, FONT);
    const longest = Math.max(...shortLabels.map((l) => estimateTextWidth(l, FONT)));
    expect(height).toBeGreaterThanOrEqual(Math.ceil(longest));
  });

  it('grows with the label length', () => {
    const short = computeXAxisLayout(['abc'], -45, FONT).height!;
    const long = computeXAxisLayout(['abc'.repeat(8)], -45, FONT).height!;
    expect(long).toBeGreaterThan(short);
  });

  it('caps the height and reports the width labels must be truncated to', () => {
    const huge = ['x'.repeat(200)];
    const { height, maxLabelWidth } = computeXAxisLayout(huge, -90, FONT);
    expect(height).toBe(MAX_X_AXIS_HEIGHT);
    expect(Number.isFinite(maxLabelWidth)).toBe(true);
    expect(maxLabelWidth).toBeLessThan(MAX_X_AXIS_HEIGHT);
  });

  it('reports how far a -45° label spills left of its tick and none at -90°', () => {
    expect(computeXAxisLayout(labels, -45, FONT).leftOverflow).toBeGreaterThan(0);
    expect(computeXAxisLayout(labels, -90, FONT).leftOverflow).toBe(0);
  });

  it('handles an empty label list', () => {
    expect(computeXAxisLayout([], -45, FONT).height).toBeGreaterThan(0);
  });
});
