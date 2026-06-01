// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// ── Auth types ────────────────────────────────────────────────────────────────

export interface CurrentUser {
  id: string
  email: string
  display_name: string | null
  is_super_admin?: boolean
}

export interface LoginResponse {
  access_token: string
  expires_in: number
  user: CurrentUser
}

export interface TokenPairResponse {
  access_token: string
  expires_in: number
}

export interface RegisterRequest {
  email: string
  password: string
  display_name?: string
}

export interface SessionInfo {
  id: string
  device_hint: string | null
  ip_address: string | null
  created_at: string
  expires_at: string
}

export interface SetupStatus {
  needs_setup: boolean
}
