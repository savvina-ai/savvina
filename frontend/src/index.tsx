// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import './index.css';
import { useAppStore } from './store/appStore';
import AppErrorBoundary from './components/AppErrorBoundary';

useAppStore.getState().initTheme();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      // 30 s default reduces refetch traffic for stable lists (connections, providers).
      // Individual hooks override where needed: schema=5 min, chat messages=Infinity.
      staleTime: 30_000,
    },
  },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </AppErrorBoundary>
  </React.StrictMode>,
);
