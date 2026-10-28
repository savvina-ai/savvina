# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""The two row-filter exceptions, in their own module so that ``row_filter`` (the facade)
and ``row_filter_rewrite`` (the sqlglot rewriter) can both import them."""

from __future__ import annotations


class RowFilterRejectedError(ValueError):
    """The statement cannot be run under the connection's row filters, so it is never sent.

    ``str(exc)`` is shown to the user verbatim (``execution._user_facing_error`` skips the
    redaction for this class). That is a guard in case the category ever changes: today
    rejections are classified ``ErrorCategory.SYNTAX``, which is not redacted. The fixed
    parts of every message are plain sentences with no apostrophe or quote character; names
    taken from the query or from the filter keys are interpolated as written. ``llm_hint``
    is set only when the model can act on the rejection (an ambiguous or mis-cased table
    name, a parse failure); the correction prompt appends it.
    """

    def __init__(self, message: str, *, llm_hint: str | None = None) -> None:
        super().__init__(message)
        self.llm_hint = llm_hint


class RowFilterInvalidError(ValueError):
    """The text an admin saved as a row filter cannot be used as one.

    For the Phase 1 single condition the message completes the sentence "Row filter …";
    for the per-table map it is the whole 422 detail, starting with ``row_filters``.
    """
