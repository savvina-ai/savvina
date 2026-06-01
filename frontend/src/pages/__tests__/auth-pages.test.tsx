// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// ── Asset stubs ──────────────────────────────────────────────────────────────

vi.mock('@/assets/logo-full.png', () => ({ default: 'logo.png' }))
vi.mock('@/assets/logo-full-dark.png', () => ({ default: 'logo-dark.png' }))

// ── Store mocks ──────────────────────────────────────────────────────────────

vi.mock('../../store/authStore', () => ({
  useAuthStore: vi.fn(),
}))

vi.mock('../../store/appStore', () => ({
  useAppStore: vi.fn(() => ({ theme: 'light', toggleTheme: vi.fn() })),
}))

import { useAuthStore } from '../../store/authStore'
const mockUseAuthStore = vi.mocked(useAuthStore)

import LoginPage from '../LoginPage'
import ForgotPasswordPage from '../ForgotPasswordPage'
import ChangePasswordPage from '../ChangePasswordPage'

// ── LoginPage ────────────────────────────────────────────────────────────────

describe('LoginPage', () => {
  beforeEach(() => {
    mockUseAuthStore.mockImplementation((selector: any) =>
      selector({ login: vi.fn(), needsSetup: false, isLoading: false, accessToken: 'tok' })
    )
  })

  it('renders "Forgot password?" link pointing to /forgot-password', () => {
    render(
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    )
    const link = screen.getByRole('link', { name: /forgot password/i })
    expect(link).toBeInTheDocument()
    expect(link).toHaveAttribute('href', '/forgot-password')
  })
})

// ── ForgotPasswordPage (new public page) ─────────────────────────────────────

describe('ForgotPasswordPage (public)', () => {
  it('renders without requiring authentication', () => {
    render(
      <MemoryRouter>
        <ForgotPasswordPage />
      </MemoryRouter>
    )
    expect(screen.getByText(/forgot your/i)).toBeInTheDocument()
    expect(screen.getByText(/contact your administrator/i)).toBeInTheDocument()
  })

  it('has a "Back to sign in" link pointing to /login', () => {
    render(
      <MemoryRouter>
        <ForgotPasswordPage />
      </MemoryRouter>
    )
    const link = screen.getByRole('link', { name: /back to sign in/i })
    expect(link).toHaveAttribute('href', '/login')
  })
})

// ── ChangePasswordPage ───────────────────────────────────────────────────────

describe('ChangePasswordPage', () => {
  it('redirects to /login when unauthenticated', () => {
    mockUseAuthStore.mockImplementation((selector: any) =>
      selector({ accessToken: null })
    )

    render(
      <MemoryRouter initialEntries={['/change-password']}>
        <Routes>
          <Route path="/change-password" element={<ChangePasswordPage />} />
          <Route path="/login" element={<div data-testid="login-page" />} />
        </Routes>
      </MemoryRouter>
    )
    expect(screen.getByTestId('login-page')).toBeInTheDocument()
  })

  it('renders the password form when authenticated', () => {
    mockUseAuthStore.mockImplementation((selector: any) =>
      selector({ accessToken: 'tok-123' })
    )

    render(
      <MemoryRouter initialEntries={['/change-password']}>
        <Routes>
          <Route path="/change-password" element={<ChangePasswordPage />} />
        </Routes>
      </MemoryRouter>
    )
    expect(screen.getByLabelText(/new password/i)).toBeInTheDocument()
  })
})
