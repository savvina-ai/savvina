// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import axios from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'

const _base =
  import.meta.env.VITE_API_URL ||
  (typeof window !== 'undefined' ? window.location.origin : '')

const apiClient = axios.create({
  baseURL: _base,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

/**
 * Auth callbacks injected by authStore after it initializes,
 * breaking the circular dependency: client.ts ↔ authStore.ts → auth.ts → client.ts
 */
interface AuthCallbacks {
  getAccessToken: () => string | null
  onRefreshSuccess: (accessToken: string) => void
  onLogout: () => Promise<void>
}

let _auth: AuthCallbacks | null = null

export function _registerAuthCallbacks(callbacks: AuthCallbacks): void {
  _auth = callbacks
}

// ── Request interceptor — attach Bearer token ─────────────────────────────────
apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = _auth?.getAccessToken() ?? null
  if (token && config.headers) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  return config
})

// ── Response interceptor — auto-refresh on 401 ───────────────────────────────
let _refreshing: Promise<string> | null = null

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean }

    // original.url is the path segment as passed to apiClient (e.g. '/api/v1/auth/login'),
    // not the full URL, because baseURL is set on the instance. The endsWith checks are
    // robust to both path-only and full-URL forms.
    const isAuthEndpoint =
      original.url?.endsWith('/auth/login') ||
      original.url?.endsWith('/auth/refresh') ||
      original.url?.endsWith('/auth/register')

    if (
      error.response?.status === 401 &&
      !original._retry &&
      !isAuthEndpoint &&
      _auth
    ) {
      original._retry = true

      const { onRefreshSuccess, onLogout } = _auth

      try {
        if (!_refreshing) {
          _refreshing = (async () => {
            const resp = await axios.post<{ access_token: string }>(
              `${_base}/api/v1/auth/refresh`,
              {},
              { withCredentials: true },
            )
            onRefreshSuccess(resp.data.access_token)
            return resp.data.access_token
          })()
        }

        const newToken = await _refreshing
        _refreshing = null

        if (original.headers) {
          original.headers['Authorization'] = `Bearer ${newToken}`
        }
        return apiClient(original)
      } catch (refreshError: unknown) {
        _refreshing = null

        const isNetworkError =
          refreshError instanceof Error &&
          axios.isAxiosError(refreshError) &&
          (!refreshError.response || refreshError.code === 'ECONNABORTED')

        if (isNetworkError) {
          return Promise.reject(refreshError)
        }

        await onLogout()
        return Promise.reject(error)
      }
    }

    return Promise.reject(error)
  },
)

export default apiClient
