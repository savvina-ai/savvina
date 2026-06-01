// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import ResultsTable from '../ResultsTable';
import { makeQueryResults } from '../../test/factories';

describe('ResultsTable', () => {
  describe('Column headers', () => {
    it('renders each column name as a th', () => {
      const results = makeQueryResults({ columns: ['user_id', 'email', 'age'] });
      render(<ResultsTable results={results} />);
      expect(screen.getByRole('columnheader', { name: /user_id/i })).toBeInTheDocument();
      expect(screen.getByRole('columnheader', { name: /email/i })).toBeInTheDocument();
      expect(screen.getByRole('columnheader', { name: /age/i })).toBeInTheDocument();
    });
  });

  describe('Row data', () => {
    it('renders correct number of rows', () => {
      const results = makeQueryResults({
        rows: [[1, 'a'], [2, 'b'], [3, 'c']],
        row_count: 3,
      });
      render(<ResultsTable results={results} />);
      const rows = screen.getAllByRole('row');
      // header row + 3 data rows
      expect(rows).toHaveLength(4);
    });

    it('renders cell values as strings', () => {
      const results = makeQueryResults({
        columns: ['id', 'name'],
        rows: [[42, 'Alice']],
        row_count: 1,
      });
      render(<ResultsTable results={results} />);
      expect(screen.getByText('42')).toBeInTheDocument();
      expect(screen.getByText('Alice')).toBeInTheDocument();
    });
  });

  describe('NULL handling', () => {
    it('null value renders italic NULL', () => {
      const results = makeQueryResults({
        columns: ['val'],
        rows: [[null]],
        row_count: 1,
      });
      const { container } = render(<ResultsTable results={results} />);
      const nullEl = container.querySelector('span.italic');
      expect(nullEl).toBeInTheDocument();
      expect(nullEl?.textContent).toBe('NULL');
    });

    it('undefined value renders italic NULL', () => {
      const results = makeQueryResults({
        columns: ['val'],
        rows: [[undefined]],
        row_count: 1,
      });
      const { container } = render(<ResultsTable results={results} />);
      const nullEl = container.querySelector('span.italic');
      expect(nullEl).toBeInTheDocument();
      expect(nullEl?.textContent).toBe('NULL');
    });
  });

  describe('Truncation', () => {
    it('value ≤80 chars is rendered in full', () => {
      const shortText = 'A'.repeat(80);
      const results = makeQueryResults({
        columns: ['val'],
        rows: [[shortText]],
        row_count: 1,
      });
      render(<ResultsTable results={results} />);
      expect(screen.getByText(shortText)).toBeInTheDocument();
    });

    it('value >80 chars is truncated to 80 chars + ellipsis with full value in title', () => {
      const longText = 'B'.repeat(100);
      const results = makeQueryResults({
        columns: ['val'],
        rows: [[longText]],
        row_count: 1,
      });
      const { container } = render(<ResultsTable results={results} />);
      const cell = container.querySelector('span[title]');
      expect(cell).toBeInTheDocument();
      expect(cell?.getAttribute('title')).toBe(longText);
      expect(cell?.textContent).toBe('B'.repeat(80) + '…');
    });
  });

  describe('Footer', () => {
    it('shows row count as "3 rows"', () => {
      render(<ResultsTable results={makeQueryResults({ row_count: 3 })} />);
      expect(screen.getByText(/3 rows/)).toBeInTheDocument();
    });

    it('shows singular "1 row" not "1 rows"', () => {
      render(<ResultsTable results={makeQueryResults({ rows: [[1, 'a']], row_count: 1 })} />);
      expect(screen.getByText(/1 row/)).toBeInTheDocument();
      expect(screen.queryByText(/1 rows/)).not.toBeInTheDocument();
    });

    it('shows "(truncated)" when results.truncated is true', () => {
      render(<ResultsTable results={makeQueryResults({ truncated: true })} />);
      expect(screen.getByText(/truncated/)).toBeInTheDocument();
    });

    it('shows formatted bytes when bytes_scanned is present', () => {
      render(
        <ResultsTable results={makeQueryResults({ bytes_scanned: 1024 * 1024 })} />,
      );
      expect(screen.getByText(/scanned/)).toBeInTheDocument();
    });

    it('hides bytes when bytes_scanned is null', () => {
      render(<ResultsTable results={makeQueryResults({ bytes_scanned: null })} />);
      expect(screen.queryByText(/scanned/)).not.toBeInTheDocument();
    });
  });

  describe('formatBytes helper (via rendered output)', () => {
    it('< 1024 bytes renders as "N B"', () => {
      render(<ResultsTable results={makeQueryResults({ bytes_scanned: 512 })} />);
      expect(screen.getByText('512 B scanned')).toBeInTheDocument();
    });

    it('KB range renders as "N.N KB"', () => {
      render(<ResultsTable results={makeQueryResults({ bytes_scanned: 2048 })} />);
      expect(screen.getByText('2.0 KB scanned')).toBeInTheDocument();
    });

    it('MB range renders as "N.N MB"', () => {
      render(
        <ResultsTable results={makeQueryResults({ bytes_scanned: 5 * 1024 * 1024 })} />,
      );
      expect(screen.getByText('5.0 MB scanned')).toBeInTheDocument();
    });

    it('GB range renders as "N.NN GB"', () => {
      render(
        <ResultsTable
          results={makeQueryResults({ bytes_scanned: 2 * 1024 * 1024 * 1024 })}
        />,
      );
      expect(screen.getByText('2.00 GB scanned')).toBeInTheDocument();
    });
  });
});
