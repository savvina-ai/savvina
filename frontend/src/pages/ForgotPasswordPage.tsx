// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { Link } from 'react-router-dom'
import logoImg from '@/assets/logo-full.png'
import logoImgDark from '@/assets/logo-full-dark.png'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'

export default function ForgotPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-sunken p-4">
      <Card className="w-full max-w-[380px] shadow-sm">
        <CardHeader className="pb-4 text-center">
          <div className="flex flex-col items-center gap-3">
            <img src={logoImg} alt="savvina ai" className="w-full max-w-[300px] h-auto dark:hidden" />
            <img src={logoImgDark} alt="savvina ai" className="hidden w-full max-w-[300px] h-auto dark:block" />
            <div>
              <CardTitle className="text-[22px] leading-tight">
                <span className="text-foreground">Forgot your </span>
                <span className="text-primary">password?</span>
              </CardTitle>
              <CardDescription className="mt-1.5">
                Password resets are managed by your administrator.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground text-center">
            Please contact your administrator to have your password reset.
          </p>
          <p className="text-center text-sm text-muted-foreground">
            <Link to="/login" className="text-primary hover:underline">
              Back to sign in
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
