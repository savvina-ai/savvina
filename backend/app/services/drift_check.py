# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Schema drift comparison shared by Check Drift and the background drift monitor.

drift_report is what GET /connections/{id}/semantic/drift returns and what
services/drift_monitor stores; the baseline helpers (_effective_known_columns and the
builders behind it) are shared with the apply and the globals finalisation in
routers/semantic.py, which imports them from here. No HTTP in this module: the caller
supplies the connection lock, so a route can answer 409 and the monitor can log and skip.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ..datasources.models import DataSourceSchema, PrivacySettings, table_key
from ..schemas.semantic import DriftReport
from ..semantic import store as semantic_store
from ..semantic.drift import (
    compute_drift,
    policy_hides_column,
    strip_ident_quotes,
    table_renames,
)
from ..semantic.generator import SemanticModelGenerator
from ..semantic.models import GenerationStatus, SemanticModel, TableSemantic
from .schema_utils import _apply_privacy_to_schema

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..models.connection import Connection


class DriftCheckUnavailableError(Exception):
    """The drift check could not take the connection lock: the connection is gone
    (*gone*), or the lock wait timed out (``DB_LOCK_TIMEOUT_SECONDS``). Raised by the
    monitor's lock; a route passes its own lock and answers with HTTP instead."""

    def __init__(self, reason: str, *, gone: bool) -> None:
        super().__init__(reason)
        self.gone = gone


def _effective_known_columns(
    known: Mapping[str, Collection[str]],
    stored_tables: Mapping[str, TableSemantic],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> dict[str, frozenset[str]]:
    """The baseline ``compute_drift`` reads: *known*, with the entry of each stored table
    the live schema holds completed to what a baseline write records for it
    (``_baseline_entries``): a table with no entry gets its live columns, and every entry
    gains the real names of the table's stored keys the policy hides and its stored live
    columns.

    A table with no entry (a model from before the baseline existed, or a table a user
    added) is baselined at first sight: whatever it holds now is "seen", so no live column
    is reported new on that account, and a column the model holds only under a near-miss
    key is renamed to its live name (``compute_drift``'s ``renamed_columns``), now or,
    for a hidden one, once the policy shows it. A column added before that first sight is
    not reported either; a Regenerate annotates it. A table whose entry predates the
    recording of hidden names gains them the same way, so un-hiding such a column renames
    its near-miss key too; ``check_drift`` persists what this completed, so the name is
    on record by the time the policy stops hiding it.
    """
    stored_columns = {key: table.columns for key, table in stored_tables.items()}
    return _completed_known_columns(known, stored_columns, schema, privacy)


def _completed_known_columns(
    known: Mapping[str, Collection[str]],
    stored_columns: Mapping[str, Collection[str]],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> dict[str, frozenset[str]]:
    """``_effective_known_columns`` from the stored column keys of each table
    (*stored_columns*) rather than a model's tables: what ``drift_report`` writes under
    the lock, from the keys ``store.load_column_names`` reads there.

    A table stored under a near-miss key that ``compute_drift`` renames
    (``table_renames``) is read under its live key, as the diff reads it, so its entry
    is that key's (completed at first sight like any other) and its near-miss column
    keys can be renamed too."""
    renames = table_renames(stored_columns, schema, privacy, known)
    stored_columns = {renames.get(key, key): names for key, names in stored_columns.items()}
    effective = {key: frozenset(names) for key, names in known.items()}
    for key, names in _baseline_entries(known, stored_columns, schema, privacy).items():
        effective[key] = frozenset(names)
    return effective


def _live_columns(
    schema: DataSourceSchema, keys: Collection[str] | None = None
) -> dict[str, list[str]]:
    """Table key → the live column names of *schema* (of the tables in *keys* when given).
    The shape ``store.update_known_columns`` takes. The routes' schema is policy-filtered,
    so a hidden column is not here; ``_baseline_entries`` adds the hidden ones the model
    stores (``_held_column_names``)."""
    return {
        table_key(table): [c.name for c in table.columns]
        for table in schema.tables
        if keys is None or table_key(table) in keys
    }


def _held_column_names(
    key: str, live: Collection[str], stored: Collection[str], privacy: PrivacySettings | None
) -> set[str]:
    """The column names table *key*'s baseline must hold because the model stores them:
    each of its *stored* column keys that is a *live* column, and the real name
    (``strip_ident_quotes``) of each stored key the policy hides.

    ``_baseline_entries`` adds these to every entry it builds, for every reader and
    writer of the baseline, so an entry is a superset of the stored live columns and of
    the real names of the stored hidden keys. The schema the routes see is
    policy-filtered, so without the second part a hidden column would leave the entry.
    That is right for one the model never annotated: once un-hidden it is reported new.
    One it stores stays seen, so once un-hidden a near-miss key of it (a blob-era
    ``'"ssn"'``) is renamed to ``ssn`` rather than ``ssn`` being reported new.

    Only a key the policy itself keeps is covered: ``policy_hides_column`` matches the
    key or its quote-stripped form exactly, so a case-only ``SSN`` of a hidden ``ssn`` is
    not hidden, the prune drops it like any gone key, and it is not recorded here.
    """
    live_names = set(live)
    return {name for name in stored if name in live_names} | {
        strip_ident_quotes(name) for name in stored if policy_hides_column(key, name, privacy)
    }


def _baseline_entries(
    seen: Mapping[str, Collection[str]],
    stored_columns: Mapping[str, Collection[str]],
    schema: DataSourceSchema,
    privacy: PrivacySettings | None,
) -> dict[str, set[str]]:
    """The baseline entry of each table of *stored_columns* that the live *schema* holds:
    the names *seen* gives it (its live columns when *seen* has no entry for it) plus the
    names the model stores (``_held_column_names``: its stored live columns, and the real
    names of its stored keys the policy hides).

    The one builder behind every reader and writer of the baseline, so an entry is always
    a superset of what the table stores and no writer can leave the hidden names out:
    ``_effective_known_columns`` (the drift check, the apply and the finalisation read
    through it) passes the stored baseline as *seen*; a generation batch passes nothing
    (its tables have seen their live columns, so the stored live columns add nothing and
    only the hidden names count); the apply and the finalisation pass what each table had
    seen that is still live. A table absent from the live schema (hidden, unreadable or
    gone) gets no entry here: the caller keeps or prunes the one it has. Nothing is
    dropped: a name in *seen* that left the database stays, so a stored key the baseline
    knew is still a drop to ``compute_drift``; the writers intersect with the live
    columns themselves, before passing *seen*.
    """
    entries: dict[str, set[str]] = {}
    for key, live in _live_columns(schema, set(stored_columns)).items():
        base = seen.get(key)
        entries[key] = set(live if base is None else base) | _held_column_names(
            key, live, stored_columns[key], privacy
        )
    return entries


def _baseline_changes(
    known: Mapping[str, Collection[str]], wanted: Mapping[str, Collection[str]]
) -> dict[str, set[str]]:
    """The entries of *wanted* that differ from the baseline *known*, or that it lacks.

    ``update_known_columns`` reads and writes only the tables it is given (plus, with
    ``prune``, one delete of the rows of tables the model no longer holds), so the
    finalisation and the apply pass it just these: the cost of keeping the baseline
    follows what changed, not the size of the schema."""
    return {
        key: set(names)
        for key, names in wanted.items()
        if key not in known or set(known[key]) != set(names)
    }


async def drift_report(
    db: AsyncSession,
    conn: Connection,
    stored_model: SemanticModel,
    schema: DataSourceSchema,
    *,
    schema_source: Literal["live", "cache"],
    schema_cached_at: str | None,
    refresh_error: str | None,
    lock: Callable[[], Awaitable[object]],
    source: Literal["manual", "monitor"],
    model_version: int | None,
) -> DriftReport:
    """Compare *stored_model* with *schema* (as introspected, or the cached copy when the
    source was unreachable: *schema_source*), store the result as the connection's last
    report, and return it. The one comparison behind GET .../semantic/drift and the
    background monitor (services/drift_monitor).

    The connection's privacy settings are applied to *schema* first. On a live schema,
    outside a run, the baseline entry of each table the model holds and the live schema
    shows is completed (_effective_known_columns) and written under the connection
    row lock, which *lock* takes: the route passes its HTTP lock (409 when the connection
    is gone), the monitor one that raises DriftCheckUnavailableError. The refreshed schema
    cache is committed before the lock is taken (see the comment below).

    The report is then stored as the connection's latest (``record_drift_report``) with
    *source* (Check Drift or the monitor) and *model_version*, the header version read
    before *stored_model* was loaded, so ``GET .../drift/last`` can tell a report about
    the current model from one a later write superseded. It is written in a transaction
    of its own, after the cache commit: the cache transaction holds the schema-cache row
    and, when the schema moved, the cache deletes, and every locked writer takes the
    header first and those rows second, so one transaction holding both in the other
    order could deadlock with a save.
    """
    connection_id = conn.id
    privacy = (
        PrivacySettings.from_dict(conn.privacy_settings)
        if conn.privacy_settings
        else PrivacySettings()
    )
    schema = _apply_privacy_to_schema(schema, privacy)

    known = await semantic_store.load_known_columns(db, connection_id)
    effective = _effective_known_columns(known, stored_model.tables, schema, privacy)
    if (
        _baseline_changes(known, effective)
        and schema_source == "live"
        and stored_model.generation_status != GenerationStatus.TABLES_PARTIAL
    ):
        # Persist what the read completed: the first-sight entry of a table with none, and
        # the names an entry from before they were recorded lacks (the real name of a
        # stored key the policy hides, a column annotated by hand), so the next check can
        # tell a new column from one the model already had, and an un-hidden column's
        # near-miss key is renamed rather than the column reported new. Bookkeeping under
        # the row lock: no version bump, no history. Not from a cached schema (it is not
        # what the model has seen), and not under a run in progress (its batches write
        # the baseline themselves).
        # The refreshed schema cache is committed first, in its own transaction, as
        # ``apply_drift`` does: the refresh always writes the user's schema-cache row and,
        # when the schema changed, deletes the query and embedding cache rows. Every writer
        # that takes this lock and then purges caches (``_invalidate_connection_caches``: a
        # save, a suggestion apply, a model delete, a generation batch, a finalisation, an
        # apply) deletes those same rows, so holding them while waiting for the lock can
        # deadlock. Safe: the session keeps its loaded objects on commit.
        await db.commit()
        await lock()
        # ``stored_model`` was loaded before the introspection, so a Regenerate another tab
        # started meanwhile is only seen by re-reading the header under the lock; its
        # batches write the baseline themselves, so the write is skipped. The baseline is
        # re-read too: a batch or an apply may have moved it since the read above, and a
        # row is replaced whole, so what is written must complete the entry as the lock
        # sees it. So are the stored column keys of the tables the read held: a PUT may
        # have deleted a hidden key meanwhile, whose real name must then not be recorded
        # as seen (and a table deleted meanwhile is not baselined). The report below is
        # still the pre-lock model's.
        header = await semantic_store.load_header(db, connection_id)
        if header is not None and header.generation_status != GenerationStatus.TABLES_PARTIAL:
            locked = await semantic_store.load_known_columns(db, connection_id)
            held = await semantic_store.load_column_names(
                db, connection_id, set(stored_model.tables)
            )
            changes = _baseline_changes(
                locked, _completed_known_columns(locked, held, schema, privacy)
            )
            if changes:
                await semantic_store.update_known_columns(db, connection_id, changes)
        # Commit now, with or without a write: releases the connection lock instead of
        # holding it through the diff and the response.
        await db.commit()
    # The store keeps what the policy hides; that absence from ``schema`` is not drift.
    # One diff for the warnings and the flag, the same diff apply-drift acts on.
    drift = compute_drift(schema, stored_model, privacy, effective)
    warnings = SemanticModelGenerator().detect_drift(schema, stored_model, privacy, drift=drift)
    checked_at = datetime.now(UTC)
    report = DriftReport(
        connection_id=connection_id,
        warnings=warnings,
        warning_count=len(warnings),
        checked_at=checked_at.isoformat(),
        schema_source=schema_source,
        schema_cached_at=schema_cached_at,
        refresh_error=refresh_error,
        has_actionable_changes=not drift.is_empty,
    )
    # Persist the refreshed cache (a no-op on the fallback path, and after the baseline
    # commit above).
    await db.commit()
    # Then the report, alone (see the docstring): bookkeeping on the header, no version
    # bump. A model deleted meanwhile matches no row and stores nothing.
    await semantic_store.record_drift_report(
        db,
        connection_id,
        {**report.model_dump(mode="json"), "source": source, "model_version": model_version},
        checked_at,
    )
    await db.commit()
    return report
