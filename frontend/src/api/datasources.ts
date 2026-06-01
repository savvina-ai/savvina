// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client';
import type { DataSourceInfo } from '../types';

export const datasourcesApi = {
  getAvailable: async () => {
    const r = await apiClient.get<DataSourceInfo[]>('/api/v1/datasources');
    return r.data;
  },
};
