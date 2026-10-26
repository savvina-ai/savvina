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

  it('says a rate limit was hit and which, without guessing the wait', () => {
    // slowapi's handler answers {"error": "Rate limit exceeded: …"}; the auth routes'
    // limit is an operator setting and may be per hour.
    const err = {
      response: { status: 429, data: { error: 'Rate limit exceeded: 100 per 1 hour' } },
    };
    expect(apiErrorMessage(err, 'Unknown error')).toBe(
      'Too many requests (Rate limit exceeded: 100 per 1 hour) — try again later',
    );
    expect(apiErrorMessage({ response: { status: 429, data: {} } }, 'Unknown error')).toBe(
      'Too many requests — try again later',
    );
  });

  it("keeps a 429's own detail", () => {
    // E.g. an LLM provider throttle the backend maps to 429 with a reason.
    const err = { response: { status: 429, data: { detail: 'Provider rate limit: retry later' } } };
    expect(apiErrorMessage(err, 'Unknown error')).toBe('Provider rate limit: retry later');
  });

  it('reads a string `error` when there is no detail', () => {
    expect(
      apiErrorMessage({ response: { status: 503, data: { error: 'Service paused' } } }, 'x'),
    ).toBe('Service paused');
    expect(apiErrorMessage({ response: { status: 503, data: { error: '' } } }, 'x')).toBe('x');
    expect(apiErrorMessage({ response: { status: 503, data: { error: { a: 1 } } } }, 'x')).toBe(
      'x',
    );
  });

  it('falls back to String(err) when no fallback is given', () => {
    expect(apiErrorMessage(new Error('network down'))).toContain('network down');
  });
});
