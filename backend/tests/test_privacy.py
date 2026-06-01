# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for app/datasources/models.PrivacySettings."""

import pytest

from app.datasources.models import PrivacySettings


class TestIsColumnSensitive:
    def setup_method(self):
        self.p = PrivacySettings()

    # Should match — pattern is a substring of the column name
    @pytest.mark.parametrize(
        "col",
        [
            "email",
            "user_email",
            "customer_email_address",
            "password",
            "hashed_password",
            "passwd",
            "ssn",
            "social_security_number",
            "api_key",
            "secret_api_key",
            "credit_card",
            "card_number",
            "phone",
            "mobile_phone",
            "salary",
            "base_salary",
            "annual_wage",
            "bank_account",
            "bank_account_number",
            "dob",
            "date_of_birth",
            "tax_id",
            "federal_tax_id",
            "passport",
            "passport_number",
            "token",
            "access_token",
            "refresh_token",
        ],
    )
    def test_sensitive_patterns_matched(self, col):
        assert self.p.is_column_sensitive(col), f"Expected {col!r} to be sensitive"

    # Should NOT match — not in any sensitive pattern
    @pytest.mark.parametrize(
        "col",
        [
            "id",
            "name",
            "first_name",
            "last_name",
            "created_at",
            "updated_at",
            "status",
            "is_active",
            "order_count",
            "total_amount",
            "description",
            "notes",
            "country",
            "region",
        ],
    )
    def test_non_sensitive_columns_not_matched(self, col):
        assert not self.p.is_column_sensitive(col), f"Expected {col!r} not to be sensitive"

    def test_case_insensitive_match(self):
        assert self.p.is_column_sensitive("EMAIL")
        assert self.p.is_column_sensitive("Password")
        assert self.p.is_column_sensitive("USER_EMAIL")


class TestIsTableExcluded:
    def test_excluded_by_schema_name(self):
        p = PrivacySettings(excluded_schemas=["audit"])
        assert p.is_table_excluded("audit", "logs")
        assert p.is_table_excluded("audit", "events")

    def test_other_schema_not_excluded(self):
        p = PrivacySettings(excluded_schemas=["audit"])
        assert not p.is_table_excluded("public", "logs")

    def test_excluded_by_bare_table_name(self):
        p = PrivacySettings(excluded_tables=["secrets"])
        assert p.is_table_excluded("public", "secrets")
        assert p.is_table_excluded("other", "secrets")

    def test_excluded_by_full_schema_table_name(self):
        p = PrivacySettings(excluded_tables=["public.secrets"])
        assert p.is_table_excluded("public", "secrets")

    def test_full_name_only_excludes_that_schema(self):
        p = PrivacySettings(excluded_tables=["public.secrets"])
        assert not p.is_table_excluded("other", "secrets")

    def test_table_not_in_list_not_excluded(self):
        p = PrivacySettings(excluded_tables=["secrets"])
        assert not p.is_table_excluded("public", "users")

    def test_empty_exclusions_nothing_excluded(self):
        p = PrivacySettings()
        assert not p.is_table_excluded("public", "anything")


class TestIsColumnExcluded:
    def test_explicitly_excluded_column(self):
        p = PrivacySettings(excluded_columns=["public.users.internal_notes"])
        assert p.is_column_excluded("public", "users", "internal_notes")

    def test_explicit_exclusion_does_not_affect_other_columns(self):
        p = PrivacySettings(excluded_columns=["public.users.internal_notes"])
        assert not p.is_column_excluded("public", "users", "name")

    def test_explicit_exclusion_is_schema_table_column_specific(self):
        p = PrivacySettings(excluded_columns=["public.users.notes"])
        # Same column name in a different table is NOT excluded
        assert not p.is_column_excluded("public", "orders", "notes")

    def test_sensitive_column_is_also_excluded(self):
        """is_column_excluded bundles the sensitive check for sample/LLM-skip decisions."""
        p = PrivacySettings()
        assert p.is_column_excluded("public", "users", "email")
        assert p.is_column_excluded("public", "users", "password")

    def test_non_sensitive_non_explicit_not_excluded(self):
        p = PrivacySettings()
        assert not p.is_column_excluded("public", "users", "name")
        assert not p.is_column_excluded("public", "users", "status")

    def test_default_sensitive_patterns_are_populated(self):
        p = PrivacySettings()
        assert len(p.sensitive_column_patterns) > 0
        assert "email" in p.sensitive_column_patterns
        assert "password" in p.sensitive_column_patterns
        assert "ssn" in p.sensitive_column_patterns
