// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import type { PrivacySettings } from '../types';

/** The settings a new connection starts with and "Reset to defaults" restores. The one copy:
 *  PrivacySettings and ConnectionPage both import it. The pattern list mirrors the backend
 *  default (PrivacySettings.sensitive_column_patterns in backend/app/datasources/models.py). */
export const PRIVACY_DEFAULTS: PrivacySettings = {
  include_sample_values: false,
  include_column_comments: false,
  include_row_counts: false,
  sensitive_column_patterns: [
    'password', 'passwd', 'secret', 'token', 'api_key',
    'email', 'ssn', 'social_security', 'credit_card', 'card_number', 'cvv',
    'phone', 'mobile', 'address', 'salary', 'wage', 'income',
    'bank_account', 'routing_number', 'dob', 'date_of_birth',
    'national_id', 'passport', 'license_number', 'tax_id',
  ],
  sensitive_columns: [],
  strict_sensitive_columns: false,
  excluded_schemas: [],
  excluded_tables: [],
  excluded_columns: [],
  row_filters: {},
};
