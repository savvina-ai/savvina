// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import postgresIcon from '@/assets/postgres.svg';
import mysqlIcon from '@/assets/mysql.svg';

const ICONS: Record<string, string> = {
  postgresql: postgresIcon,
  mysql: mysqlIcon,
};

export function getDatasourceIcon(sourceType: string): string | null {
  return ICONS[sourceType] ?? null;
}
