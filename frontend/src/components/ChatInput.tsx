// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { memo, useRef, type KeyboardEvent } from 'react';
import { Square } from 'lucide-react';

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: (message: string) => void;
  onStop?: () => void;
  disabled?: boolean;
  placeholder?: string;
}

function ChatInput({ value, onChange, onSend, onStop, disabled = false, placeholder }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    onChange('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  return (
    <div className="border-t border-border bg-background p-4">
      <div className="flex items-center gap-2 rounded-xl border border-border bg-surface-sunken px-3 py-2 shadow-sm transition-colors focus-within:border-primary focus-within:ring-1 focus-within:ring-primary/20">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder={placeholder ?? 'Ask a question about your data…'}
          disabled={disabled}
          rows={1}
          className="max-h-48 flex-1 resize-none overflow-y-auto bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        />
        {onStop && disabled ? (
          <button
            onClick={onStop}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-destructive text-destructive-foreground transition-opacity hover:opacity-90"
            title="Stop"
          >
            <Square className="h-3 w-3 fill-current" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={disabled || !value.trim()}
            className="flex h-7 shrink-0 items-center gap-1.5 rounded-lg bg-brand-gradient px-2 font-mono text-[11px] font-semibold uppercase tracking-wide text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            ▶ Send
            <kbd className="rounded border border-white/25 bg-white/20 px-1 py-px font-mono text-[9px] leading-none">
              ⌘↵
            </kbd>
          </button>
        )}
      </div>
    </div>
  );
}

export default memo(ChatInput);
