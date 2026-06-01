// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { create } from 'zustand'
import { _registerAuthCallbacks } from '../api/client'
import type { CurrentUser } from '../types/auth'
import {
  STORAGE_KEY_APP,
  STORAGE_KEY_WIZARD_DONE,
  STORAGE_KEY_REFRESH_LOCK,
} from '../lib/storageKeys'

const REFRESH_LOCK_TTL_MS = 5_000
// Refresh proactively at 85% of the token's lifetime so we don't wait for a 401.
const PROACTIVE_REFRESH_FRACTION = 0.85

// Cross-tab token broadcast — prevents concurrent refresh storms when multiple
// tabs hit the 85% proactive-refresh window simultaneously.
const _authChannel = typeof BroadcastChannel !== 'undefined'
  ? new BroadcastChannel('savvina-auth')
  : null

if (_authChannel) {
  _authChannel.onmessage = (ev: MessageEvent) => {
    if (ev.data?.type === 'TOKEN_REFRESHED') {
      useAuthStore.setState({ accessToken: ev.data.access_token })
      _scheduleProactiveRefresh(ev.data.expires_in)
    }
    // Another tab claimed the refresh — cancel our pending timer so we don't
    // race on the single-use refresh token. TOKEN_REFRESHED will re-arm it.
    if (ev.data?.type === 'REFRESH_STARTED') {
      _clearRefreshTimer()
    }
  }
}

let _refreshTimer: ReturnType<typeof setTimeout> | null = null

async function _tryRefresh(): Promise<void> {
  const lockAt = Number(localStorage.getItem(STORAGE_KEY_REFRESH_LOCK) ?? 0)
  if (Date.now() - lockAt < REFRESH_LOCK_TTL_MS) return
  // Claim leadership synchronously, then broadcast before the first await so
  // other tabs clear their timers before they could race on the single-use
  // token. A sub-millisecond race remains if two tabs fire in the same JS turn,
  // but the backend's reuse-detection handles that gracefully.
  localStorage.setItem(STORAGE_KEY_REFRESH_LOCK, String(Date.now()))
  _authChannel?.postMessage({ type: 'REFRESH_STARTED' })
  try {
    const { authApi } = await import('../api/auth')
    const data = await authApi.refresh()
    useAuthStore.setState({ accessToken: data.access_token })
    _scheduleProactiveRefresh(data.expires_in)
    _authChannel?.postMessage({
      type: 'TOKEN_REFRESHED',
      access_token: data.access_token,
      expires_in: data.expires_in,
    })
  } catch {
    // Proactive refresh failed — the 401 interceptor will handle it reactively
  } finally {
    localStorage.removeItem(STORAGE_KEY_REFRESH_LOCK)
  }
}

function _scheduleProactiveRefresh(expiresIn: number): void {
  if (_refreshTimer) clearTimeout(_refreshTimer)
  const refreshAtMs = expiresIn * 1000 * PROACTIVE_REFRESH_FRACTION
  _refreshTimer = setTimeout(_tryRefresh, refreshAtMs)
}

function _clearRefreshTimer(): void {
  if (_refreshTimer) {
    clearTimeout(_refreshTimer)
    _refreshTimer = null
  }
}

interface RegisterParams {
  email: string
  password: string
  displayName?: string
}

interface AuthState {
  accessToken: string | null
  user: CurrentUser | null
  isLoading: boolean
  needsSetup: boolean | null

  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  initialize: () => Promise<void>
  register: (params: RegisterParams) => Promise<void>

  _setAccessToken: (token: string) => void
}

export const useAuthStore = create<AuthState>((set) => {
  _registerAuthCallbacks({
    getAccessToken: () => useAuthStore.getState().accessToken,
    onRefreshSuccess: (accessToken) => {
      set({ accessToken })
    },
    onLogout: async () => {
      _clearRefreshTimer()
      set({ accessToken: null, user: null })
      const { useAppStore } = await import('./appStore')
      useAppStore.getState().setActiveConnection(null)
    },
  })

  return {
    accessToken: null,
    user: null,
    isLoading: true,
    needsSetup: null,

    _setAccessToken: (token) => set({ accessToken: token }),

    login: async (email, password) => {
      set({ isLoading: true })
      try {
        const { authApi } = await import('../api/auth')
        const data = await authApi.login(email, password)
        set({
          accessToken: data.access_token,
          user: data.user,
          isLoading: false,
        })
        _scheduleProactiveRefresh(data.expires_in)
      } catch (err) {
        set({ isLoading: false })
        throw err
      }
    },

    logout: async () => {
      _clearRefreshTimer()
      set({ accessToken: null, user: null })

      const { useAppStore } = await import('./appStore')
      useAppStore.getState().setActiveConnection(null)

      try {
        const { authApi } = await import('../api/auth')
        await authApi.logout()
      } catch {
        // Ignore — local state is already cleared
      }
    },

    initialize: async () => {
      set({ isLoading: true })
      // Phase 1: setup status — failure here doesn't kill session restore
      try {
        const { authApi } = await import('../api/auth')
        const setupData = await authApi.getSetupStatus()
        set({ needsSetup: setupData.needs_setup })
      } catch {
        set({ needsSetup: false })
      }
      // Phase 2: token restore — rely on HttpOnly cookie, no localStorage
      try {
        const { authApi } = await import('../api/auth')
        const data = await authApi.refresh()
        set({
          accessToken: data.access_token,
          isLoading: false,
        })
        _scheduleProactiveRefresh(data.expires_in)
        const user = await authApi.getMe()
        set({ user })
      } catch {
        set({ accessToken: null, user: null, isLoading: false })
      }
    },

    register: async ({ email, password, displayName }) => {
      set({ isLoading: true })
      try {
        const { authApi } = await import('../api/auth')
        const data = await authApi.register({
          email,
          password,
          display_name: displayName,
        })
        // First-boot registration always starts the setup wizard from scratch.
        localStorage.removeItem(STORAGE_KEY_WIZARD_DONE)
        localStorage.removeItem(STORAGE_KEY_APP)
        const { useAppStore } = await import('./appStore')
        useAppStore.setState({
          activeConnectionId: null,
          activeSessionId: null,
          selectedProvider: '',
          messages: [],
        })
        set({
          accessToken: data.access_token,
          user: data.user,
          needsSetup: false,
          isLoading: false,
        })
        _scheduleProactiveRefresh(data.expires_in)
      } catch (err) {
        set({ isLoading: false })
        throw err
      }
    },
  }
})
