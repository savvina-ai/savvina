// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Export API — authenticated XLSX/CSV blob downloads and report generation. */

import apiClient from './client';
import { downloadBlob } from '../lib/exportUtils';

export const exportApi = {
  /** Download query results as XLSX (triggers browser download). */
  downloadXlsx: async (messageId: string) => {
    const res = await apiClient.get<Blob>(
      `/api/v1/export/messages/${messageId}/xlsx`,
      { responseType: 'blob' },
    );
    downloadBlob(res.data, `query-${messageId.slice(0, 8)}.xlsx`);
  },

  /** Download query results as CSV via backend (for contexts without local data). */
  downloadCsvBackend: async (messageId: string) => {
    const res = await apiClient.get<Blob>(
      `/api/v1/export/messages/${messageId}/csv`,
      { responseType: 'blob' },
    );
    downloadBlob(res.data, `query-${messageId.slice(0, 8)}.csv`);
  },

  /** Generate and download a PDF report combining multiple message results. */
  downloadReport: async (
    title: string,
    sections: { message_id: string; heading?: string; chart_image?: string; chart_title?: string }[],
  ) => {
    const res = await apiClient.post<Blob>(
      '/api/v1/export/report',
      { title, sections },
      { responseType: 'blob' },
    );
    downloadBlob(res.data, `${title.replace(/[^a-zA-Z0-9-_ ]/g, '').slice(0, 60)}.pdf`);
  },
};
