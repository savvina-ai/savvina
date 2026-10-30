# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The query-rejection exceptions (a row-filter rejection, a strict sensitive-column
rejection, an invalid filter), in their own module so that ``row_filter``,
``row_filter_rewrite`` and ``sensitive_guard`` can all import them."""

from __future__ import annotations


class QueryRejectedError(ValueError):
    """The statement cannot be run under the connection's privacy settings, so it is never
    sent.

    ``str(exc)`` is shown to the user verbatim (``execution._user_facing_error`` skips the
    redaction for this class) and the failure is classified SYNTAX. The fixed parts of every
    message are plain sentences with no apostrophe or quote character; names taken from the
    query or from the settings are interpolated as written. ``llm_hint`` is set only when
    the model can act on the rejection; the correction prompt appends it.
    """

    def __init__(self, message: str, *, llm_hint: str | None = None) -> None:
        super().__init__(message)
        self.llm_hint = llm_hint


class RowFilterRejectedError(QueryRejectedError):
    """The statement cannot be run under the connection's row filters (an ambiguous or
    mis-cased table name, a parse failure, the migration state)."""


class SensitiveColumnRejectedError(QueryRejectedError):
    """Strict sensitive-column mode: the statement uses a sensitive column outside its
    SELECT list (``services/sensitive_guard``)."""


class RowFilterInvalidError(ValueError):
    """The text an admin saved as a row filter cannot be used as one.

    For the Phase 1 single condition the message completes the sentence "Row filter …";
    for the per-table map it is the whole 422 detail, starting with ``row_filters``.
    """
