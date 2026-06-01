// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useEffect, useRef, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import logoImg from '@/assets/logo.png';
import {
  MessageSquare,
  Plug,
  Layers,
  Settings,
  Database,
  Sun,
  Moon,
  PanelLeftClose,
  PanelLeft,
  User,
  LogOut,
  FileText,
} from 'lucide-react';
import { useAppStore } from '../store/appStore';
import { useAuthStore } from '../store/authStore';
import { useConnections } from '../hooks/useConnections';
import { cn } from '@/lib/utils';

export default function AppSidebar() {
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  const activeConnectionId = useAppStore((s) => s.activeConnectionId);
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const navigate = useNavigate();

  const { data: connections } = useConnections();
  const activeConn = connections?.find((c) => c.id === activeConnectionId);

  const [expanded, setExpanded] = useState(false);
  const [width, setWidth] = useState(192);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  useEffect(() => {
    const onMouseMove = (e: globalThis.MouseEvent) => {
      if (!isResizing.current) return;
      const delta = e.clientX - startX.current;
      setWidth(Math.min(400, Math.max(150, startWidth.current + delta)));
    };
    const onMouseUp = () => {
      isResizing.current = false;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, []);

  const handleSignOut = () => {
    logout().then(() => navigate('/login', { replace: true }));
  };

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      'flex h-9 items-center gap-2 rounded-lg transition-colors',
      expanded ? 'px-2' : 'w-9 justify-center self-center',
      isActive
        ? 'bg-sidebar-accent text-sidebar-accent-foreground'
        : 'text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
    );

  return (
    <aside
      className={cn(
        'relative flex h-screen shrink-0 flex-col border-r border-border bg-sidebar py-4 transition-[width] duration-200',
        expanded ? 'items-stretch px-2' : 'w-14 items-center',
      )}
      style={expanded ? { width } : undefined}
    >
      {/* Logo */}
      <div className={cn('mb-6 flex items-center gap-2', expanded ? 'px-2' : 'justify-center')}>
        <img src={logoImg} alt="savvina ai" className="h-8 w-8 shrink-0 rounded-lg border border-border" />
        {expanded && (
          <span className="truncate font-display text-sm font-semibold text-foreground">
            savvina ai
          </span>
        )}
      </div>

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-1">
        {/* Chat */}
        <NavLink to="/chat" title="Chat" className={navLinkClass}>
          <MessageSquare className="h-4 w-4 shrink-0" />
          {expanded && <span className="truncate text-[13px]">Chat</span>}
        </NavLink>

        {/* Connections */}
        <NavLink to="/connect" title="Connections" className={navLinkClass}>
          <Plug className="h-4 w-4 shrink-0" />
          {expanded && <span className="truncate text-[13px]">Connections</span>}
        </NavLink>

        {/* Reports */}
        <NavLink to="/reports" title="Reports" className={navLinkClass}>
          <FileText className="h-4 w-4 shrink-0" />
          {expanded && <span className="truncate text-[13px]">Reports</span>}
        </NavLink>

        {/* Semantic Model — only shown when an active connection is selected */}
        {activeConn && (
          <NavLink
            to={`/semantic/${activeConnectionId}`}
            title="Semantic Model"
            className={navLinkClass}
          >
            <Layers className="h-4 w-4 shrink-0" />
            {expanded && <span className="truncate text-[13px]">Semantic Model</span>}
          </NavLink>
        )}

        {/* Settings */}
        <NavLink to="/settings" title="Settings" className={navLinkClass}>
          <Settings className="h-4 w-4 shrink-0" />
          {expanded && <span className="truncate text-[13px]">Settings</span>}
        </NavLink>
      </nav>

      {/* Bottom controls */}
      <div className="flex flex-col gap-2">
        {/* Collapse toggle */}
        <button
          onClick={() => setExpanded(!expanded)}
          title={expanded ? 'Collapse sidebar' : 'Expand sidebar'}
          className={cn(
            'flex h-9 items-center gap-2 rounded-lg text-sidebar-foreground transition-colors hover:bg-sidebar-accent',
            expanded ? 'px-2' : 'w-9 justify-center self-center',
          )}
        >
          {expanded ? (
            <PanelLeftClose className="h-4 w-4 shrink-0" />
          ) : (
            <PanelLeft className="h-4 w-4 shrink-0" />
          )}
          {expanded && <span className="truncate text-[13px]">Collapse</span>}
        </button>

        {/* Theme toggle */}
        <button
          onClick={toggleTheme}
          title="Toggle theme"
          className={cn(
            'flex h-9 items-center gap-2 rounded-lg text-sidebar-foreground transition-colors hover:bg-sidebar-accent',
            expanded ? 'px-2' : 'w-9 justify-center self-center',
          )}
        >
          {theme === 'light' ? (
            <Moon className="h-4 w-4 shrink-0" />
          ) : (
            <Sun className="h-4 w-4 shrink-0" />
          )}
          {expanded && (
            <span className="truncate text-[13px]">
              {theme === 'light' ? 'Dark mode' : 'Light mode'}
            </span>
          )}
        </button>

        {/* Active connection badge */}
        {activeConn && (
          <div
            className={cn(
              'flex items-center gap-1.5 rounded-md bg-sidebar-accent',
              expanded ? 'px-2 py-1.5' : 'h-8 w-8 justify-center self-center',
            )}
            title={`${activeConn.name} · ${activeConn.source_type}`}
          >
            <Database className="h-3.5 w-3.5 shrink-0 text-schema-icon" />
            {expanded && (
              <span className="truncate font-mono text-[10px] font-semibold uppercase tracking-wide text-badge-text">
                ● {activeConn.name} · {activeConn.source_type}
              </span>
            )}
          </div>
        )}

        {/* User profile link */}
        {user && (
          <NavLink to="/profile" title={user.email} className={navLinkClass}>
            <User className="h-4 w-4 shrink-0" />
            {expanded && (
              <span className="min-w-0 truncate text-[13px]">
                {user.display_name || user.email}
              </span>
            )}
          </NavLink>
        )}

        {/* Sign out */}
        {user && (
          <button
            onClick={handleSignOut}
            title="Sign Out"
            className={cn(
              'flex h-9 items-center gap-2 rounded-lg transition-colors',
              expanded ? 'px-2' : 'w-9 justify-center self-center',
              'text-destructive hover:bg-destructive/10',
            )}
          >
            <LogOut className="h-4 w-4 shrink-0" />
            {expanded && <span className="truncate text-[13px]">Sign Out</span>}
          </button>
        )}
      </div>
      {/* Resize handle — only visible when expanded */}
      {expanded && (
        <div
          role="separator"
          aria-orientation="vertical"
          tabIndex={0}
          onMouseDown={(e) => {
            isResizing.current = true;
            startX.current = e.clientX;
            startWidth.current = width;
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'col-resize';
            e.preventDefault();
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowRight') {
              setWidth((w) => Math.min(400, w + 10));
            } else if (e.key === 'ArrowLeft') {
              setWidth((w) => Math.max(150, w - 10));
            }
          }}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 focus:outline-none focus:bg-primary/40"
        />
      )}
    </aside>
  );
}
