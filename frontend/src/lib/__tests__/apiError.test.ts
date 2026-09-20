// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { apiErrorMessage } from '../apiError';

describe('apiErrorMessage', () => {
  it('returns a string detail as-is', () => {
    expect(apiErrorMessage({ response: { data: { detail: 'Connection not found' } } })).toBe(
      'Connection not found',
    );
  });

  it('collapses a 422 validation array to one string instead of leaking it into JSX', () => {
    const err = {
      response: {
        data: {
          detail: [
            { loc: ['body', 'default_query_timeout'], msg: 'too big' },
            { loc: ['body', 'bcrypt_rounds'], msg: 'out of range' },
          ],
        },
      },
    };
    expect(apiErrorMessage(err)).toBe('default_query_timeout: too big; bcrypt_rounds: out of range');
  });

  it('serialises an object detail', () => {
    expect(apiErrorMessage({ response: { data: { detail: { msg: 'boom' } } } })).toBe(
      '{"msg":"boom"}',
    );
  });

  it('uses the fallback when there is no detail', () => {
    expect(apiErrorMessage({ response: { status: 500, data: {} } }, 'Failed to save')).toBe(
      'Failed to save',
    );
    expect(apiErrorMessage(new Error('network down'), 'Failed to save')).toBe('Failed to save');
  });

  it('uses the fallback when the detail is an empty string', () => {
    expect(apiErrorMessage({ response: { data: { detail: '' } } }, 'Failed to save')).toBe(
      'Failed to save',
    );
  });

  it('falls back to String(err) when no fallback is given', () => {
    expect(apiErrorMessage(new Error('network down'))).toContain('network down');
  });
});
