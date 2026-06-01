// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import apiClient from './client'
import type {
  CurrentUser,
  LoginResponse,
  RegisterRequest,
  SessionInfo,
  SetupStatus,
  TokenPairResponse,
} from '../types/auth'
import type { PaginatedResponse } from '../types'

export const authApi = {
  async register(data: RegisterRequest): Promise<LoginResponse> {
    const res = await apiClient.post<LoginResponse>('/api/v1/auth/register', data)
    return res.data
  },

  async login(email: string, password: string): Promise<LoginResponse> {
    const res = await apiClient.post<LoginResponse>('/api/v1/auth/login', { email, password })
    return res.data
  },

  async refresh(): Promise<TokenPairResponse> {
    const res = await apiClient.post<TokenPairResponse>('/api/v1/auth/refresh', {})
    return res.data
  },

  async logout(): Promise<void> {
    await apiClient.post('/api/v1/auth/logout', {})
  },

  async logoutAll(): Promise<void> {
    await apiClient.post('/api/v1/auth/logout-all')
  },

  async getMe(): Promise<CurrentUser> {
    const res = await apiClient.get<CurrentUser>('/api/v1/auth/me')
    return res.data
  },

  async updateMe(data: {
    display_name?: string
    current_password?: string
    new_password?: string
  }): Promise<CurrentUser> {
    const res = await apiClient.put<CurrentUser>('/api/v1/auth/me', data)
    return res.data
  },

  async resetPassword(password: string): Promise<void> {
    await apiClient.post('/api/v1/auth/reset-password', { password })
  },

  async getSessions(): Promise<SessionInfo[]> {
    const res = await apiClient.get<PaginatedResponse<SessionInfo>>('/api/v1/auth/sessions')
    return res.data.items
  },

  async deleteSession(sessionId: string): Promise<void> {
    await apiClient.delete(`/api/v1/auth/sessions/${sessionId}`)
  },

  async getSetupStatus(): Promise<SetupStatus> {
    const res = await apiClient.get<SetupStatus>('/api/v1/auth/setup-status')
    return res.data
  },
}
