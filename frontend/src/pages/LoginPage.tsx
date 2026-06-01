// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState } from 'react'
import { useNavigate, Navigate, Link } from 'react-router-dom'
import { Sun, Moon } from 'lucide-react'
import { useAuthStore } from '../store/authStore'
import { useAppStore } from '../store/appStore'
import logoImg from '@/assets/logo-full.png'
import logoImgDark from '@/assets/logo-full-dark.png'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const login = useAuthStore((s) => s.login)
  const needsSetup = useAuthStore((s) => s.needsSetup)
  const isLoading = useAuthStore((s) => s.isLoading)
  const navigate = useNavigate()
  const theme = useAppStore((s) => s.theme)
  const toggleTheme = useAppStore((s) => s.toggleTheme)

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    )
  }

  if (needsSetup === true) {
    return <Navigate to="/setup" replace />
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
      navigate('/', { replace: true })
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Login failed. Please check your credentials.'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-sunken p-4">
      <Card className="w-full max-w-[380px] shadow-sm">
        <CardHeader className="pb-4 text-center">
          <div className="flex justify-end">
            <button
              onClick={toggleTheme}
              title="Toggle theme"
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              {theme === 'light' ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
            </button>
          </div>
          <div className="flex flex-col items-center gap-3">
            <img src={logoImg} alt="savvina ai" className="w-full max-w-[300px] h-auto dark:hidden" />
            <img src={logoImgDark} alt="savvina ai" className="hidden w-full max-w-[300px] h-auto dark:block" />
            <div>
              <CardTitle className="text-[22px] leading-tight">
                <span className="text-foreground">Welcome </span><span className="text-primary">back</span>
              </CardTitle>
              <CardDescription className="mt-1.5">
                Sign in to query your data in plain English.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
              />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="password">Password</Label>
                <Link
                  to="/forgot-password"
                  className="text-xs text-muted-foreground hover:text-primary hover:underline"
                >
                  Forgot password?
                </Link>
              </div>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
              />
            </div>

            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}

            <Button type="submit" variant="gradient" className="w-full" disabled={submitting}>
              {submitting ? 'Signing in…' : 'Sign in'}
            </Button>

          </form>
        </CardContent>
      </Card>
    </div>
  )
}
