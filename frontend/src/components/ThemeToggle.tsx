// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { Sun, Moon } from 'lucide-react';
import { useAppStore } from '../store/appStore';
import { cn } from '@/lib/utils';

export default function ThemeToggle() {
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);

  return (
    <button
      onClick={toggleTheme}
      aria-label={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
      className={cn(
        'flex h-9 w-9 items-center justify-center rounded-lg',
        'text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground',
      )}
    >
      {theme === 'light' ? (
        <Moon className="h-4 w-4" />
      ) : (
        <Sun className="h-4 w-4" />
      )}
    </button>
  );
}
