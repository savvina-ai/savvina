// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import GenerationBanner from '../GenerationBanner';

const base = {
  tablesDone: 3,
  tablesTotal: 10,
  canGenerate: true,
  disabledReason: null,
  hasTables: true,
  onResume: vi.fn(),
  onFinish: vi.fn(),
  onRestart: vi.fn(),
};

describe('GenerationBanner', () => {
  it('names the three actions and reports progress', () => {
    render(<GenerationBanner {...base} />);
    expect(screen.getByText(/interrupted at table 3 of 10/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: "Finish with what's generated" })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Restart from scratch' })).toBeEnabled();
  });

  it('routes each button to its own handler', async () => {
    const user = userEvent.setup();
    render(<GenerationBanner {...base} />);
    await user.click(screen.getByRole('button', { name: 'Resume' }));
    await user.click(screen.getByRole('button', { name: "Finish with what's generated" }));
    await user.click(screen.getByRole('button', { name: 'Restart from scratch' }));
    expect(base.onResume).toHaveBeenCalledTimes(1);
    expect(base.onFinish).toHaveBeenCalledTimes(1);
    expect(base.onRestart).toHaveBeenCalledTimes(1);
  });

  it('disables Finish when nothing was generated, but leaves Resume and Restart', () => {
    render(<GenerationBanner {...base} hasTables={false} tablesDone={0} />);
    expect(
      screen.getByText('No tables were generated — resume or restart instead'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: "Finish with what's generated" })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Restart from scratch' })).toBeEnabled();
  });

  it('disables everything with the given reason when the user cannot generate', () => {
    render(
      <GenerationBanner
        {...base}
        canGenerate={false}
        disabledReason="Configure an LLM provider in Settings first"
      />,
    );
    // Shown as text too: a disabled button's title is not reliably shown or announced.
    expect(screen.getByText('Configure an LLM provider in Settings first')).toBeInTheDocument();
    for (const name of ['Resume', "Finish with what's generated", 'Restart from scratch']) {
      const btn = screen.getByRole('button', { name });
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute('title', 'Configure an LLM provider in Settings first');
    }
  });
});
