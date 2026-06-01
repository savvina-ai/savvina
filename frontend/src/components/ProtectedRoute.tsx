// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { Navigate } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'

interface Props {
  children: React.ReactNode
}

export default function ProtectedRoute({ children }: Props) {
  const { accessToken, isLoading, needsSetup } = useAuthStore()

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    )
  }

  if (needsSetup === true) {
    return <Navigate to="/setup" replace />
  }

  if (!accessToken) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}
