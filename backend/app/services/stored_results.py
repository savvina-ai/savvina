# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Re-mask stored query results on every read.

A message's ``results_json`` is served long after it was stored: in the session history,
through a share link, in every export. Messages stored before results were masked on the
streaming path hold sensitive values in the clear, and the privacy settings may have
tightened since. So every read masks the stored rows again with the connection's
**current** settings, classified from the stored SQL exactly as a fresh execution is
(``result_masking._plan_result_mask``). Masking is idempotent: ``[REDACTED]`` stays
``[REDACTED]``. A result also carries the fingerprint of the row-filter map it was computed
under (``QueryResultsResponse.row_filter_fingerprint``); when it differs from the
connection's current map, is not a string, or the connection is in the row-filter migration
state, the result is served with no rows and ``stale_row_filter`` set, whatever the columns.

A connection with no stored settings, or one that no longer exists, masks with
``PrivacySettings()`` and an unknown dialect (double quotes read as identifiers, the
over-masking direction); classification that fails on SQL naming a sensitive column masks
every column (``MASK_ALL_COLUMNS``). A connection whose source type is no longer registered
masks every column: its dialect, and so how its SQL is lexed, is unknown.

Classifying a stored query is the costly part (a few milliseconds of SQL lexing), and a
request can serve hundreds of messages, some unauthenticated (``/public/share/session``).
``StoredResultMasks`` therefore builds one plan per (session, stored query) per request,
unwraps each stored query once for both the mask and the served SQL, and ``serve_all``
runs the batch in a worker thread so it does not block the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..config import get_settings
from ..datasources.models import PrivacySettings
from ..datasources.registry import get_datasource_class
from ..models.chat import ChatSession
from ..models.connection import Connection
from .execution import unwrap_row_filter
from .result_masking import (
    _REDACTED,
    MASK_ALL_COLUMNS,
    _double_quote_is_literal,
    _plan_result_mask,
    _ResultMaskPlan,
)
from .row_filter import row_filter_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Shown in place of a result computed under a different row-filter map than the connection
# has now (history notice, share and export 410, report section).
STALE_ROW_FILTER_TEXT = (
    "These results were computed under a different row filter. Re-run the question."
)


@dataclass(frozen=True)
class StoredResultPrivacy:
    """The privacy settings and dialect quoting a stored result is re-masked with.
    *mask_all*: the dialect is unknown, so every column is masked. *row_filter_fingerprint*:
    the fingerprint a stored result must carry to be served at all (``""`` when the
    connection has no row filters; ``None`` when no stored result can match, the row-filter
    migration state)."""

    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    double_quote_is_string_literal: bool = False
    mask_all: bool = False
    row_filter_fingerprint: str | None = ""

    def mask(self, results_json: dict | None, query: str | None) -> dict | None:
        """*results_json* with sensitive and excluded columns redacted; the same dict when
        nothing changes. *query* is the stored SQL (possibly row-filter wrapped). Every key
        other than ``rows`` is kept as stored. A result computed under another row-filter
        map comes back with no rows and ``stale_row_filter`` set."""
        return _remask(
            results_json,
            lambda: self.plan(query, served_query(query)),
            self.row_filter_fingerprint,
        )

    def plan(self, query: str | None, served: str | None) -> _ResultMaskPlan | None:
        """The mask plan for stored *query*, *served* being ``served_query(query)``: the
        clean SQL is classified, as for a fresh execution."""
        if self.mask_all:
            return _ResultMaskPlan(self.privacy, MASK_ALL_COLUMNS, set(), None)
        return _plan_result_mask(
            self.privacy,
            query,
            served,
            double_quote_is_string_literal=self.double_quote_is_string_literal,
        )


def _remask(
    results_json: dict | None,
    plan_for: Callable[[], _ResultMaskPlan | None],
    expected_fingerprint: str | None,
) -> dict | None:
    """``StoredResultPrivacy.mask`` with the plan supplied by *plan_for*, called only when
    the rows are readable enough to need one. First the row-filter check: a result whose
    stored fingerprint is not *expected_fingerprint* was computed under another map (or
    before fingerprints existed, while the connection now has filters) and is served with
    no rows; the rule is "same map or nothing", so loosening a filter blanks older results
    too. *expected_fingerprint* ``None`` (the migration state) makes every result stale. A
    stored fingerprint that is neither a string nor null is stale too and is served as
    ``None``."""
    if not results_json:
        return results_json
    stored = results_json.get("row_filter_fingerprint")
    readable = isinstance(stored, str | None)
    if expected_fingerprint is None or not readable or (stored or "") != expected_fingerprint:
        stale = {**results_json, "rows": [], "row_count": 0, "stale_row_filter": True}
        if not readable:
            # Not the string every writer stores: a tampered or corrupt row. Served stale,
            # with the field cleared so typed consumers (the single-share
            # ``QueryResultsResponse``, the frontend's ``string | null``) never see a
            # non-string.
            stale["row_filter_fingerprint"] = None
        return stale
    rows = results_json.get("rows")
    if not isinstance(rows, list):
        return {**results_json, "rows": []}
    columns = results_json.get("columns")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return {**results_json, "rows": [_redacted_row(row, 0) for row in rows]}
    if not all(isinstance(row, list) for row in rows):
        rows = [row if isinstance(row, list) else _redacted_row(row, len(columns)) for row in rows]
    plan = plan_for()
    masked = plan.redact(rows, columns) if plan is not None else rows
    if masked is results_json.get("rows"):
        return results_json
    return {**results_json, "rows": masked}


def _redacted_row(row: Any, width: int) -> list[str]:
    """A row the mask cannot read, every cell redacted."""
    return [_REDACTED] * (len(row) if isinstance(row, list) else width)


_DEFAULT = StoredResultPrivacy()


def stored_result_privacy(
    privacy_settings: dict | None, source_type: str | None
) -> StoredResultPrivacy:
    """The re-mask settings for a connection's stored ``privacy_settings`` and
    ``source_type``, read without connecting (the adapter class attribute). A source type
    that is no longer registered masks every column: the dialect is unknown. The expected
    row-filter fingerprint is computed from the stored map with the app's encryption key. In
    the migration state the expected fingerprint is ``None``: every stored result is stale
    until the admin migrates or removes the legacy filter."""
    privacy = PrivacySettings.from_dict(privacy_settings) if privacy_settings else PrivacySettings()
    fingerprint = (
        None
        if privacy.row_filter_needs_migration
        else row_filter_fingerprint(privacy.row_filters, get_settings().encryption_key)
    )
    dq = False
    if source_type:
        try:
            dq = _double_quote_is_literal(get_datasource_class(source_type))
        except ValueError:
            return StoredResultPrivacy(
                privacy, False, mask_all=True, row_filter_fingerprint=fingerprint
            )
    return StoredResultPrivacy(privacy, dq, row_filter_fingerprint=fingerprint)


@dataclass(frozen=True)
class StoredResultMasks:
    """Re-mask settings per chat session, loaded once per request.

    Also the request's memo: the served SQL per stored query and the mask plan per
    (session, stored query). One instance belongs to one request and is not shared between
    concurrent callers; ``serve_all`` uses it from a single worker thread.
    """

    by_session: dict[str, StoredResultPrivacy]
    _served: dict[str | None, str | None] = field(default_factory=dict, repr=False, compare=False)
    _plans: dict[tuple[str, str | None], _ResultMaskPlan | None] = field(
        default_factory=dict, repr=False, compare=False
    )

    def for_session(self, session_id: str) -> StoredResultPrivacy:
        return self.by_session.get(session_id, _DEFAULT)

    def mask(self, session_id: str, results_json: dict | None, query: str | None) -> dict | None:
        return self.serve(session_id, results_json, query)[0]

    def serve(
        self, session_id: str, results_json: dict | None, query: str | None
    ) -> tuple[dict | None, str | None]:
        """A stored message as it is served: its results re-masked and its SQL as
        ``served_query`` gives it, the stored query unwrapped once for both."""
        if query in self._served:
            served = self._served[query]
        else:
            served = self._served[query] = served_query(query)

        def plan_for() -> _ResultMaskPlan | None:
            key = (session_id, query)
            if key not in self._plans:
                self._plans[key] = self.for_session(session_id).plan(query, served)
            return self._plans[key]

        expected = self.for_session(session_id).row_filter_fingerprint
        return _remask(results_json, plan_for, expected), served

    async def serve_all(
        self, messages: Sequence[tuple[str, dict | None, str | None]]
    ) -> list[tuple[dict | None, str | None]]:
        """``serve`` for each ``(session_id, results_json, query)``, in order, in a worker
        thread: the classification is CPU-bound and a history page can hold hundreds of
        messages. The masking reads no shared mutable state; this object's memo is
        touched only by that thread while it runs."""
        if not messages:
            return []
        return await asyncio.to_thread(lambda: [self.serve(*message) for message in messages])


async def load_result_masks(db: AsyncSession, session_ids: Iterable[str]) -> StoredResultMasks:
    """One query: each session's connection privacy settings and source type. A session
    whose connection is gone (or that is not found) masks with the defaults."""
    ids = sorted(set(session_ids))
    if not ids:
        return StoredResultMasks({})
    result = await db.execute(
        select(ChatSession.id, Connection.privacy_settings, Connection.source_type)
        .outerjoin(Connection, Connection.id == ChatSession.connection_id)
        .where(ChatSession.id.in_(ids))
    )
    return StoredResultMasks(
        {
            session_id: stored_result_privacy(privacy_settings, source_type)
            for session_id, privacy_settings, source_type in result.all()
        }
    )


def served_query(query: str | None) -> str | None:
    """Stored SQL as it is served (to the user or the LLM): without the row-filter wrap
    the pre-Task-9 path stored, so the filter's columns and literals are not shown and a
    re-run is wrapped once. New rows already hold the clean SQL."""
    return unwrap_row_filter(query) if query else query
