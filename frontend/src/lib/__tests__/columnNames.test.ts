// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { uniqueColumnNames } from '../columnNames';

describe('uniqueColumnNames', () => {
  it('keeps distinct names as they are', () => {
    expect(uniqueColumnNames(['id', 'name'])).toEqual(['id', 'name']);
  });
  it('suffixes the second and later occurrences', () => {
    expect(uniqueColumnNames(['count', 'count', 'count'])).toEqual(['count', 'count_2', 'count_3']);
  });
  it('skips a suffix that is itself a column name', () => {
    expect(uniqueColumnNames(['count', 'count_2', 'count'])).toEqual(['count', 'count_2', 'count_3']);
    expect(uniqueColumnNames(['count', 'count', 'count_2'])).toEqual(['count', 'count_3', 'count_2']);
  });
  it('honours suffixed names anywhere in the input (the approved example)', () => {
    expect(uniqueColumnNames(['count', 'count', 'count_2', 'count'])).toEqual([
      'count', 'count_3', 'count_2', 'count_4',
    ]);
  });
  it('is case-sensitive, as the engines are', () => {
    expect(uniqueColumnNames(['Count', 'count'])).toEqual(['Count', 'count']);
  });
  it('returns an empty array for an empty input', () => {
    expect(uniqueColumnNames([])).toEqual([]);
  });
});
