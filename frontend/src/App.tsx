// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import SetupPage from './pages/SetupPage';
import Layout from './components/Layout';
import ProtectedRoute from './components/ProtectedRoute';
import LoginPage from './pages/LoginPage';
import ChangePasswordPage from './pages/ChangePasswordPage';
import ForgotPasswordPage from './pages/ForgotPasswordPage';
import { useAppStore } from './store/appStore';
import { useAuthStore } from './store/authStore';

const SetupWizardPage = lazy(() => import('./pages/SetupWizardPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const ConnectionPage = lazy(() => import('./pages/ConnectionPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const HistoryPage = lazy(() => import('./pages/HistoryPage'));
const SemanticModelPage = lazy(() => import('./pages/SemanticModelPage'));
const ProfilePage = lazy(() => import('./pages/ProfilePage'));
const SharePage = lazy(() => import('./pages/SharePage'));
const SharedSessionPage = lazy(() => import('./pages/SharedSessionPage'));
const ReportBuilderPage = lazy(() => import('./pages/ReportBuilderPage'));

function RootRedirect() {
  const activeConnectionId = useAppStore((s) => s.activeConnectionId);
  return activeConnectionId ? (
    <Navigate to="/chat" replace />
  ) : (
    <Navigate to="/connect" replace />
  );
}

function App() {
  const initialize = useAuthStore((s) => s.initialize);

  useEffect(() => {
    initialize();
  }, [initialize]);

  return (
    <BrowserRouter>
      <Suspense fallback={null}>
        <Routes>
          {/* First-boot setup */}
          <Route path="/setup" element={<SetupPage />} />
          <Route
            path="/setup/wizard"
            element={
              <ProtectedRoute>
                <SetupWizardPage />
              </ProtectedRoute>
            }
          />

          {/* Public routes */}
          <Route path="/login" element={<LoginPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/change-password" element={<ProtectedRoute><ChangePasswordPage /></ProtectedRoute>} />
          <Route path="/share/:token" element={<SharePage />} />
          <Route path="/share/session/:token" element={<SharedSessionPage />} />

          {/* Protected routes */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<RootRedirect />} />
            <Route path="connect" element={<ConnectionPage />} />
            <Route path="chat" element={<ChatPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="reports" element={<ReportBuilderPage />} />
            <Route path="semantic/:connectionId" element={<SemanticModelPage />} />
            <Route path="profile" element={<ProfilePage />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}

export default App;
