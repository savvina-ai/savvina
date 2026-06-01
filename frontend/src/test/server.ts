// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { makeConnection, makeChatResponse } from './factories';

function paginated(items: unknown[] = []) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

export const handlers = [
  http.get('http://localhost:8000/api/v1/connections', () =>
    HttpResponse.json(paginated()),
  ),
  http.post('http://localhost:8000/api/v1/connections', () =>
    HttpResponse.json(makeConnection()),
  ),
  http.delete('http://localhost:8000/api/v1/connections/:id', () =>
    HttpResponse.json({}),
  ),
  http.post('http://localhost:8000/api/v1/connections/:id/test', () =>
    HttpResponse.json({ success: true, message: 'OK' }),
  ),
  http.get('http://localhost:8000/api/v1/connections/:id/schema', () =>
    HttpResponse.json({ source_type: 'postgresql', schemas: [], tables: [], relationships: [] }),
  ),
  http.get('http://localhost:8000/api/v1/providers', () =>
    HttpResponse.json(paginated()),
  ),
  http.post('http://localhost:8000/api/v1/chat', () =>
    HttpResponse.json(makeChatResponse()),
  ),
  http.post('http://localhost:8000/api/v1/chat/execute/:id', () =>
    HttpResponse.json(makeChatResponse()),
  ),
  http.get('http://localhost:8000/api/v1/chat/sessions', () =>
    HttpResponse.json(paginated()),
  ),
  http.delete('http://localhost:8000/api/v1/chat/sessions/:id', () =>
    HttpResponse.json({}),
  ),
  http.get('http://localhost:8000/api/v1/chat/sessions/:id/history', () =>
    HttpResponse.json(paginated()),
  ),
  http.post('http://localhost:8000/api/v1/chat/feedback/:id', () =>
    HttpResponse.json({}),
  ),
];

export const server = setupServer(...handlers);
