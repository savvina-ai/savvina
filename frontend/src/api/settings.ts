// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type { AppSettings } from '../types';

export const settingsApi = {
  get: () =>
    apiClient.get<AppSettings>('/api/v1/settings').then((r) => r.data),

  update: (payload: Partial<AppSettings>) =>
    apiClient.put<AppSettings>('/api/v1/settings', payload).then((r) => r.data),
};
