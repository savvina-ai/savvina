// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest'

// ---------------------------------------------------------------------------
// All mocks must be created inside vi.hoisted so they are available before
// any module imports are evaluated.  BroadcastChannel is stubbed on globalThis
// here so that authStore's module-level `new BroadcastChannel(...)` call sees
// the mock instead of jsdom's undefined.
// ---------------------------------------------------------------------------
const { mockRefresh, mockLogin, mockGetMe, mockGetSetupStatus, bcPostMessage, getOnMessage } =
  vi.hoisted(() => {
    const mockRefresh = vi.fn()
    const mockLogin = vi.fn()
    const mockGetMe = vi.fn()
    const mockGetSetupStatus = vi.fn()
    const bcPostMessage = vi.fn()
    let _onMessage: ((ev: MessageEvent) => void) | null = null

    function MockBC() {
      return {
        postMessage: bcPostMessage,
        set onmessage(h: ((ev: MessageEvent) => void) | null) {
          _onMessage = h
        },
        get onmessage() {
          return _onMessage
        },
      }
    }

    globalThis.BroadcastChannel = MockBC as unknown as typeof BroadcastChannel

    return {
      mockRefresh,
      mockLogin,
      mockGetMe,
      mockGetSetupStatus,
      bcPostMessage,
      getOnMessage: () => _onMessage,
    }
  })

vi.mock('../api/client', () => ({ _registerAuthCallbacks: vi.fn() }))

vi.mock('../api/auth', () => ({
  authApi: {
    refresh: mockRefresh,
    login: mockLogin,
    logout: vi.fn(),
    getMe: mockGetMe,
    getSetupStatus: mockGetSetupStatus,
    register: vi.fn(),
  },
}))

import { useAuthStore } from './authStore'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const REFRESH_LOCK_KEY = 'savvina-refresh-lock'

const mockUser = {
  id: '1',
  email: 'a@b.com',
  display_name: 'Alice',
  is_super_admin: false,
}

function resetStore() {
  useAuthStore.setState({ accessToken: null, user: null, isLoading: false, needsSetup: null })
}

// ---------------------------------------------------------------------------
// Refresh lock mutex behaviour
// ---------------------------------------------------------------------------
describe('authStore — refresh lock mutex', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    localStorage.clear()
    mockRefresh.mockReset()
    mockLogin.mockReset()
    resetStore()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('backs off when another tab has set the lock within the TTL window', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    await useAuthStore.getState().login('a@b.com', 'pw')

    // Advance to just before the proactive refresh fires (85 % of 10 s = 8 500 ms)
    await vi.advanceTimersByTimeAsync(8_490)

    // Another tab claims the lock right before our timer fires
    localStorage.setItem(REFRESH_LOCK_KEY, String(Date.now()))

    await vi.advanceTimersByTimeAsync(20) // timer fires at 8 500 ms
    expect(mockRefresh).not.toHaveBeenCalled()
  })

  it('writes REFRESH_LOCK_KEY before calling the refresh endpoint', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    mockRefresh.mockResolvedValueOnce({ access_token: 'tok2', expires_in: 10 })

    await useAuthStore.getState().login('a@b.com', 'pw')

    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')
    await vi.advanceTimersByTimeAsync(8_500)

    expect(setItemSpy.mock.calls.some(([k]) => k === REFRESH_LOCK_KEY)).toBe(true)
  })

  it('removes REFRESH_LOCK_KEY after a successful refresh', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    mockRefresh.mockResolvedValueOnce({ access_token: 'tok2', expires_in: 10 })

    await useAuthStore.getState().login('a@b.com', 'pw')
    await vi.advanceTimersByTimeAsync(8_500)

    expect(localStorage.getItem(REFRESH_LOCK_KEY)).toBeNull()
  })

  it('removes REFRESH_LOCK_KEY even when the refresh call throws', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    mockRefresh.mockRejectedValueOnce(new Error('network'))

    await useAuthStore.getState().login('a@b.com', 'pw')
    await vi.advanceTimersByTimeAsync(8_500)

    expect(localStorage.getItem(REFRESH_LOCK_KEY)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// BroadcastChannel cross-tab coordination
// ---------------------------------------------------------------------------
describe('authStore — BroadcastChannel coordination', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    localStorage.clear()
    mockRefresh.mockReset()
    mockLogin.mockReset()
    bcPostMessage.mockReset()
    resetStore()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('broadcasts REFRESH_STARTED immediately after claiming the lock', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    mockRefresh.mockResolvedValueOnce({ access_token: 'tok2', expires_in: 10 })

    await useAuthStore.getState().login('a@b.com', 'pw')
    await vi.advanceTimersByTimeAsync(8_500)

    expect(bcPostMessage).toHaveBeenCalledWith({ type: 'REFRESH_STARTED' })
  })

  it('broadcasts TOKEN_REFRESHED with the new token on a successful refresh', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })
    mockRefresh.mockResolvedValueOnce({ access_token: 'refreshed-tok', expires_in: 3600 })

    await useAuthStore.getState().login('a@b.com', 'pw')
    await vi.advanceTimersByTimeAsync(8_500)

    expect(bcPostMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'TOKEN_REFRESHED', access_token: 'refreshed-tok' }),
    )
  })

  it('TOKEN_REFRESHED message from another tab updates the store accessToken', () => {
    const handler = getOnMessage()
    expect(handler).not.toBeNull()

    handler!(
      new MessageEvent('message', {
        data: { type: 'TOKEN_REFRESHED', access_token: 'cross-tab-tok', expires_in: 3600 },
      }),
    )

    expect(useAuthStore.getState().accessToken).toBe('cross-tab-tok')
  })

  it('REFRESH_STARTED message from another tab cancels the pending proactive refresh', async () => {
    mockLogin.mockResolvedValueOnce({ access_token: 'tok', expires_in: 10, user: mockUser })

    await useAuthStore.getState().login('a@b.com', 'pw')

    // Simulate another tab announcing that it has taken the refresh lead
    const handler = getOnMessage()!
    handler(new MessageEvent('message', { data: { type: 'REFRESH_STARTED' } }))

    await vi.advanceTimersByTimeAsync(8_500)

    // Our timer was cancelled — no refresh should have been issued
    expect(mockRefresh).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Session restore on page load — gated on the session-hint cookie
// ---------------------------------------------------------------------------
const SESSION_HINT = 'savvina_session'

function setHint() {
  document.cookie = `${SESSION_HINT}=1; path=/`
}

function clearHint() {
  document.cookie = `${SESSION_HINT}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
}

describe('authStore — initialize() and the session-hint cookie', () => {
  beforeEach(() => {
    localStorage.clear()
    clearHint()
    mockRefresh.mockReset()
    mockGetMe.mockReset()
    mockGetSetupStatus.mockReset()
    mockGetSetupStatus.mockResolvedValue({ needs_setup: false })
    resetStore()
  })

  afterEach(() => {
    clearHint()
    useAuthStore.setState({ accessToken: null, user: null })
  })

  it('does not call /auth/refresh for a visitor with no hint cookie', async () => {
    await useAuthStore.getState().initialize()

    expect(mockRefresh).not.toHaveBeenCalled()
    expect(mockGetMe).not.toHaveBeenCalled()
    const state = useAuthStore.getState()
    expect(state.accessToken).toBeNull()
    expect(state.user).toBeNull()
    expect(state.isLoading).toBe(false)
  })

  it('still reads the setup status for a visitor with no hint cookie', async () => {
    mockGetSetupStatus.mockResolvedValueOnce({ needs_setup: true })

    await useAuthStore.getState().initialize()

    expect(mockGetSetupStatus).toHaveBeenCalledTimes(1)
    expect(useAuthStore.getState().needsSetup).toBe(true)
  })

  it('restores the session through /auth/refresh when the hint cookie is present', async () => {
    vi.useFakeTimers()
    try {
      setHint()
      mockRefresh.mockResolvedValueOnce({ access_token: 'restored', expires_in: 900 })
      mockGetMe.mockResolvedValueOnce(mockUser)

      await useAuthStore.getState().initialize()

      expect(mockRefresh).toHaveBeenCalledTimes(1)
      const state = useAuthStore.getState()
      expect(state.accessToken).toBe('restored')
      expect(state.user).toEqual(mockUser)
      expect(state.isLoading).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('ends up signed out when the hint cookie is stale and /auth/refresh is refused', async () => {
    setHint()
    mockRefresh.mockRejectedValueOnce(new Error('401'))

    await useAuthStore.getState().initialize()

    const state = useAuthStore.getState()
    expect(mockRefresh).toHaveBeenCalledTimes(1)
    expect(state.accessToken).toBeNull()
    expect(state.user).toBeNull()
    expect(state.isLoading).toBe(false)
  })

  it('does not read a cookie whose name only ends with the hint name', async () => {
    document.cookie = `x${SESSION_HINT}=1; path=/`
    try {
      await useAuthStore.getState().initialize()
      expect(mockRefresh).not.toHaveBeenCalled()
    } finally {
      document.cookie = `x${SESSION_HINT}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`
    }
  })
})
