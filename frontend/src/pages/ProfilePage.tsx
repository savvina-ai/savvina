// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  User,
  Mail,
  KeyRound,
  Monitor,
  Trash2,
  Check,
  AlertCircle,
  Loader2,
} from 'lucide-react'

import { useAuthStore } from '@/store/authStore'
import { authApi } from '@/api/auth'
import { cn } from '@/lib/utils'
import type { CurrentUser, SessionInfo } from '@/types/auth'

/* ── helpers ─────────────────────────────────────────────────────────────── */

function SectionCard({ title, icon: Icon, children }: {
  title: string
  icon: typeof User
  children: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3">
        <Icon className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  )
}

function StatusBanner({ type, message }: { type: 'success' | 'error'; message: string }) {
  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-md px-3 py-2 text-sm',
        type === 'success'
          ? 'bg-primary/10 text-primary'
          : 'bg-destructive/10 text-destructive',
      )}
    >
      {type === 'success' ? (
        <Check className="h-4 w-4 shrink-0" />
      ) : (
        <AlertCircle className="h-4 w-4 shrink-0" />
      )}
      {message}
    </div>
  )
}

/* ── page ────────────────────────────────────────────────────────────────── */

export default function ProfilePage() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // ── Profile info state ──
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [profileStatus, setProfileStatus] = useState<{ type: 'success' | 'error'; msg: string } | null>(null)

  useEffect(() => {
    setDisplayName(user?.display_name ?? '')
  }, [user?.display_name])

  const profileMutation = useMutation({
    mutationFn: (data: { display_name: string }) => authApi.updateMe(data),
    onSuccess: (updated: CurrentUser) => {
      useAuthStore.setState({ user: updated })
      setProfileStatus({ type: 'success', msg: 'Display name updated' })
    },
    onError: () => {
      setProfileStatus({ type: 'error', msg: 'Failed to update display name' })
    },
  })

  // ── Password change state ──
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordStatus, setPasswordStatus] = useState<{ type: 'success' | 'error'; msg: string } | null>(null)

  const passwordMutation = useMutation({
    mutationFn: (data: { current_password: string; new_password: string }) =>
      authApi.updateMe(data),
    onSuccess: () => {
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setPasswordStatus({ type: 'success', msg: 'Password changed successfully' })
    },
    onError: (err: unknown) => {
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response: { data: { detail: string } } }).response?.data?.detail
          : null
      setPasswordStatus({ type: 'error', msg: detail || 'Failed to change password' })
    },
  })

  const handlePasswordChange = () => {
    setPasswordStatus(null)
    if (newPassword !== confirmPassword) {
      setPasswordStatus({ type: 'error', msg: 'New passwords do not match' })
      return
    }
    if (newPassword.length < 12) {
      setPasswordStatus({ type: 'error', msg: 'Password must be at least 12 characters' })
      return
    }
    passwordMutation.mutate({ current_password: currentPassword, new_password: newPassword })
  }

  // ── Sessions ──
  const { data: sessions, isPending: sessionsLoading } = useQuery<SessionInfo[]>({
    queryKey: ['auth-sessions'],
    queryFn: () => authApi.getSessions(),
  })

  const [sessionActionError, setSessionActionError] = useState<string | null>(null)

  const revokeSession = useMutation({
    mutationFn: (id: string) => authApi.deleteSession(id),
    onSuccess: () => {
      setSessionActionError(null)
      queryClient.invalidateQueries({ queryKey: ['auth-sessions'] })
    },
    onError: () => setSessionActionError('Failed to revoke session'),
  })

  const logoutAll = useMutation({
    mutationFn: () => authApi.logoutAll(),
    onSuccess: () => {
      logout().then(() => navigate('/login', { replace: true }))
    },
    onError: () => setSessionActionError('Failed to sign out all devices'),
  })


  if (!user) return null

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-6 py-8">
        {/* Header */}
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10">
            <User className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-foreground">Profile</h1>
            <p className="text-sm text-muted-foreground">{user.email}</p>
          </div>
        </div>

        <div className="flex flex-col gap-6">
          {/* ── Profile information ───────────────────────────────────── */}
          <SectionCard title="Profile Information" icon={User}>
            {profileStatus && (
              <div className="mb-4">
                <StatusBanner type={profileStatus.type} message={profileStatus.msg} />
              </div>
            )}

            <div className="flex flex-col gap-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Email
                </label>
                <div className="flex items-center gap-2 rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
                  <Mail className="h-3.5 w-3.5" />
                  {user.email}
                </div>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Display Name
                </label>
                <input
                  type="text"
                  value={displayName}
                  onChange={(e) => {
                    setDisplayName(e.target.value)
                    setProfileStatus(null)
                  }}
                  placeholder="Enter your display name"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div className="flex justify-end">
                <button
                  onClick={() => {
                    setProfileStatus(null)
                    profileMutation.mutate({ display_name: displayName })
                  }}
                  disabled={profileMutation.isPending || displayName === (user.display_name ?? '') || displayName.trim() === ''}
                  className={cn(
                    'rounded-md px-4 py-2 text-sm font-medium transition-colors',
                    'bg-brand-gradient text-white shadow-gradient-btn hover:opacity-90',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                  )}
                >
                  {profileMutation.isPending ? 'Saving...' : 'Save'}
                </button>
              </div>
            </div>
          </SectionCard>

          {/* ── Change password ───────────────────────────────────────── */}
          <SectionCard title="Change Password" icon={KeyRound}>
            {passwordStatus && (
              <div className="mb-4">
                <StatusBanner type={passwordStatus.type} message={passwordStatus.msg} />
              </div>
            )}

            <div className="flex flex-col gap-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Current Password
                </label>
                <input
                  type="password"
                  value={currentPassword}
                  onChange={(e) => {
                    setCurrentPassword(e.target.value)
                    setPasswordStatus(null)
                  }}
                  placeholder="Enter current password"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  New Password
                </label>
                <input
                  type="password"
                  value={newPassword}
                  onChange={(e) => {
                    setNewPassword(e.target.value)
                    setPasswordStatus(null)
                  }}
                  placeholder="At least 12 characters"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
                {newPassword.length > 0 && (
                  <ul className="mt-1.5 space-y-0.5 text-xs">
                    {[
                      { label: 'At least 12 characters', met: newPassword.length >= 12 },
                      { label: 'One uppercase letter', met: /[A-Z]/.test(newPassword) },
                      { label: 'One digit', met: /\d/.test(newPassword) },
                      { label: 'One special character', met: /[^A-Za-z0-9]/.test(newPassword) },
                    ].map(({ label, met }) => (
                      <li key={label} className={met ? 'text-green-600 dark:text-green-400' : 'text-muted-foreground'}>
                        {met ? '✓' : '·'} {label}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-muted-foreground">
                  Confirm New Password
                </label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => {
                    setConfirmPassword(e.target.value)
                    setPasswordStatus(null)
                  }}
                  placeholder="Re-enter new password"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div className="flex justify-end">
                <button
                  onClick={handlePasswordChange}
                  disabled={
                    passwordMutation.isPending ||
                    !currentPassword ||
                    !newPassword ||
                    !confirmPassword
                  }
                  className={cn(
                    'rounded-md px-4 py-2 text-sm font-medium transition-colors',
                    'bg-brand-gradient text-white shadow-gradient-btn hover:opacity-90',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                  )}
                >
                  {passwordMutation.isPending ? 'Changing...' : 'Change Password'}
                </button>
              </div>
            </div>
          </SectionCard>

          {/* ── Active sessions ───────────────────────────────────────── */}
          <SectionCard title="Active Sessions" icon={Monitor}>
            {sessionActionError && (
              <div className="mb-4">
                <StatusBanner type="error" message={sessionActionError} />
              </div>
            )}
            {sessionsLoading ? (
              <div className="flex items-center justify-center py-6">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : sessions && sessions.length > 0 ? (
              <div className="flex flex-col gap-2">
                {sessions.map((s) => (
                  <div
                    key={s.id}
                    className="flex items-center justify-between rounded-md border border-border px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-foreground">
                        {s.device_hint || 'Unknown device'}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {s.ip_address || 'Unknown IP'}
                        {' · '}
                        Created {new Date(s.created_at).toLocaleDateString()}
                      </p>
                    </div>
                    <button
                      onClick={() => revokeSession.mutate(s.id)}
                      disabled={revokeSession.isPending}
                      title="Revoke session"
                      className="ml-2 rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}

                <button
                  onClick={() => logoutAll.mutate()}
                  disabled={logoutAll.isPending}
                  className="mt-2 self-end text-xs font-medium text-destructive hover:underline"
                >
                  {logoutAll.isPending ? 'Signing out...' : 'Sign out of all sessions'}
                </button>
              </div>
            ) : (
              <p className="py-4 text-center text-sm text-muted-foreground">
                No active sessions found
              </p>
            )}
          </SectionCard>

        </div>
      </div>
    </div>
  )
}
