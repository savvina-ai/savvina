// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import React from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

interface ConfirmDeleteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  title?: string;
  description?: React.ReactNode;
  isPending?: boolean;
}

/**
 * Reusable confirmation dialog for destructive delete actions.
 * Renders a modal with Cancel and Delete buttons.
 * The caller controls open/close state and the confirm handler.
 */
export function ConfirmDeleteDialog({
  open,
  onOpenChange,
  onConfirm,
  title = 'Delete?',
  description = 'This action cannot be undone.',
  isPending = false,
}: ConfirmDeleteDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border px-4 py-2 text-sm hover:bg-muted"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          >
            {isPending ? 'Deleting…' : 'Delete'}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
