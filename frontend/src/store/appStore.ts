// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { ChatMessage, SchemaResponse } from '../types';
import { STORAGE_KEY_APP, STORAGE_KEY_THEME } from '../lib/storageKeys';

interface AppState {
  activeConnectionId: string | null;
  setActiveConnection: (id: string | null) => void;
  activeSessionId: string | null;
  setActiveSession: (id: string | null) => void;
  /** Set the session ID without clearing messages — used when the backend assigns a new session_id mid-stream. */
  promoteSession: (id: string) => void;
  selectedProvider: string;
  setSelectedProvider: (provider: string) => void;
  schema: SchemaResponse | null;
  setSchema: (schema: SchemaResponse | null) => void;
  messages: ChatMessage[];
  addMessage: (msg: ChatMessage) => void;
  updateMessage: (id: string, updates: Partial<ChatMessage>) => void;
  setMessages: (msgs: ChatMessage[]) => void;
  clearMessages: () => void;
  theme: 'light' | 'dark';
  toggleTheme: () => void;
  initTheme: () => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      activeConnectionId: null,
      setActiveConnection: (id) =>
        set({ activeConnectionId: id, activeSessionId: null, messages: [] }),

      activeSessionId: null,
      setActiveSession: (id) => set({ activeSessionId: id, messages: [] }),
      promoteSession: (id) => set({ activeSessionId: id }),

      selectedProvider: '',
      setSelectedProvider: (provider) => set({ selectedProvider: provider }),

      schema: null,
      setSchema: (schema) => set({ schema }),

      messages: [],
      addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
      updateMessage: (id, updates) =>
        set((s) => ({
          messages: s.messages.map((m) => (m.id === id ? { ...m, ...updates } : m)),
        })),
      setMessages: (msgs) => set({ messages: msgs }),
      clearMessages: () => set({ messages: [] }),

      theme: 'light',
      toggleTheme: () => {
        const next = get().theme === 'light' ? 'dark' : 'light'
        set({ theme: next })
        document.documentElement.classList.toggle('dark', next === 'dark')
        localStorage.setItem(STORAGE_KEY_THEME, next)
      },
      initTheme: () => {
        const raw = localStorage.getItem(STORAGE_KEY_THEME)
        const saved: 'light' | 'dark' | null = raw === 'light' || raw === 'dark' ? raw : null
        const preferred = saved || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
        set({ theme: preferred })
        document.documentElement.classList.toggle('dark', preferred === 'dark')
      },
    }),
    {
      name: STORAGE_KEY_APP,
      partialize: (state) => ({
        activeConnectionId: state.activeConnectionId,
        activeSessionId: state.activeSessionId,
        selectedProvider: state.selectedProvider,
      }),
    },
  ),
);
