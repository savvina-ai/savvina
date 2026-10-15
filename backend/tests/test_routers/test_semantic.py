# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Tests for /api/v1/connections/{id}/semantic endpoints."""

from __future__ import annotations

from contextlib import ExitStack
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.database import get_db
from app.datasources.models import ColumnInfo, DataSourceSchema, SchemaInfo, TableInfo
from app.main import app
from app.semantic import store as semantic_store
from app.semantic.models import (
    GENERATION_BATCH_SIZE,
    AggregationType,
    CommonJoin,
    DerivedColumn,
    GenerationProgress,
    GenerationStatus,
    RelationshipEdge,
    Segment,
    SemanticModel,
    SimpleMetric,
    TableSemantic,
)

from .conftest import MockResult, _make_conn, _mock_db

# Minimal dict that validates as SemanticModelResponse
_EMPTY_MODEL = {
    "tables": {},
    "business_metrics": [],
    "common_joins": [],
    "is_user_reviewed": False,
}

_STORE = "app.routers.semantic.semantic_store"


def _populated() -> SemanticModel:
    return SemanticModel.model_validate(
        {
            **_EMPTY_MODEL,
            "tables": {
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {"status": {"display_name": "Status"}},
                }
            },
        }
    )


# ── GET /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestGetSemanticModel:
    async def test_returns_200_when_model_exists(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]

    async def test_returns_404_when_no_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 404

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.get("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── PUT /api/v1/connections/{id}/semantic ───────────────────────────────────────


class TestUpdateSemanticModel:
    async def test_patch_goes_through_the_store_and_returns_its_model(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        apply = AsyncMock(return_value=_populated())
        with patch(f"{_STORE}.apply_user_patch", new=apply):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic",
                json={"tables": {"public.orders": {"description": "hand written"}}},
            )
        assert resp.status_code == 200
        assert "public.orders" in resp.json()["tables"]
        patch_arg = apply.await_args.args[2]
        assert patch_arg.tables["public.orders"].description == "hand written"
        assert apply.await_args.kwargs["updated_by"]
        db.commit.assert_awaited_once()

    async def test_invalidates_caches(self, http_client):
        """PUT semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # delete QueryCacheEntry
            MockResult(),  # delete UserSchemaCache
            MockResult(),  # delete TableEmbeddingCache
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.apply_user_patch", new=AsyncMock(return_value=_populated())):
            await http_client.put("/api/v1/connections/conn-123/semantic", json={})
        # 1 select (lock_and_reread) + 3 deletes; the store write is patched out.
        assert db.execute.await_count == 4

    async def test_store_conflict_is_409(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch",
            new=AsyncMock(side_effect=semantic_store.SemanticStoreConflict("moved on")),
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 409
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_validation_error_is_400(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(
            f"{_STORE}.apply_user_patch", new=AsyncMock(side_effect=ValueError("bad shape"))
        ):
            resp = await http_client.put(
                "/api/v1/connections/conn-123/semantic", json={"notes": ["x"]}
            )
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.put("/api/v1/connections/missing/semantic", json={})
        assert resp.status_code == 404


# ── DELETE /api/v1/connections/{id}/semantic ────────────────────────────────────


class TestDeleteSemanticModel:
    async def test_deletes_through_the_store(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # clear the legacy blob
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        delete = AsyncMock()
        with patch(f"{_STORE}.delete_model", new=delete):
            resp = await http_client.delete("/api/v1/connections/conn-123/semantic")
        assert resp.status_code == 204
        delete.assert_awaited_once()
        assert delete.await_args.args[1] == "conn-123"
        assert delete.await_args.kwargs["changed_by"]
        db.commit.assert_awaited_once()

    async def test_clears_the_legacy_blob_with_sql_null(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # clear the legacy blob
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.delete_model", new=AsyncMock()):
            await http_client.delete("/api/v1/connections/conn-123/semantic")
        sql = [str(c.args[0].compile()) for c in db.execute.await_args_list]
        # SQL NULL, not a bound None (which a JSON column stores as the text 'null').
        assert any(q.startswith("UPDATE connections SET semantic_model=NULL") for q in sql), sql

    async def test_invalidates_caches(self, http_client):
        """DELETE semantic must invalidate all three cache tables before commit."""
        db = _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # clear the legacy blob
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.delete_model", new=AsyncMock()):
            await http_client.delete("/api/v1/connections/conn-123/semantic")
        # 1 select (lock_and_reread) + 1 blob update + 3 deletes; the store delete is patched out.
        assert db.execute.await_count == 5

    async def test_returns_404_when_connection_missing(self, http_client):
        db = _mock_db(MockResult(single=None))
        app.dependency_overrides[get_db] = lambda: db
        resp = await http_client.delete("/api/v1/connections/missing/semantic")
        assert resp.status_code == 404


# ── GET /api/v1/connections/{id}/semantic/drift ─────────────────────────────────


class TestCheckDrift:
    async def test_returns_200_no_warnings(self, http_client):
        usc = MagicMock()
        usc.schema_cache = {"tables": {}}
        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=usc),  # user schema cache lookup
        )
        app.dependency_overrides[get_db] = lambda: db
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())),
            patch("app.routers.semantic._schema_from_dict", return_value=MagicMock()),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
        ):
            mock_gen.return_value.detect_drift = MagicMock(return_value=[])
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        assert resp.json()["warnings"] == []

    async def test_what_the_policy_hides_is_not_reported_as_removed(self, http_client):
        """The store keeps hidden annotations; drift must read the policy, not the absence."""
        usc = MagicMock()
        usc.schema_cache = {"tables": {}}
        conn = _make_conn(
            privacy_settings={
                "excluded_tables": ["public.payroll"],
                "excluded_columns": ["public.orders.ssn"],
            }
        )
        db = _mock_db(MockResult(single=conn), MockResult(single=usc))
        app.dependency_overrides[get_db] = lambda: db
        stored = SemanticModel(
            tables={
                "public.orders": TableSemantic(
                    display_name="Orders",
                    columns={"id": {"display_name": "Id"}, "ssn": {"display_name": "SSN"}},
                ),
                "public.payroll": TableSemantic(display_name="Payroll"),
                "public.ghosts": TableSemantic(display_name="Ghosts"),
            }
        )
        # The cache as introspected: orders.ssn excluded at introspection, payroll present.
        with (
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=stored)),
            patch(
                "app.routers.semantic._schema_from_dict",
                return_value=_schema(["orders", "payroll"]),
            ),
        ):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 200
        warnings = resp.json()["warnings"]
        assert not any("payroll" in w or "ssn" in w for w in warnings)
        # A table that is genuinely gone is still reported.
        assert any('"public.ghosts"' in w for w in warnings)

    async def test_returns_404_no_semantic_model(self, http_client):
        db = _mock_db(MockResult(single=_make_conn()))
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404

    async def test_returns_404_no_schema_cache(self, http_client):
        db = _mock_db(
            MockResult(single=_make_conn()),  # get_connection_or_404
            MockResult(single=None),  # no user schema cache for this user
        )
        app.dependency_overrides[get_db] = lambda: db
        with patch(f"{_STORE}.load_model", new=AsyncMock(return_value=_populated())):
            resp = await http_client.get("/api/v1/connections/conn-123/semantic/drift")
        assert resp.status_code == 404


# ── Phased generation ───────────────────────────────────────────────────────────


def _schema(names: list[str], columns: dict[str, list[str]] | None = None) -> DataSourceSchema:
    """A ``public`` schema; each table has an ``id`` column unless ``columns`` names its own."""
    cols = columns or {}
    return DataSourceSchema(
        source_type="postgresql",
        schemas=[SchemaInfo(name="public")],
        tables=[
            TableInfo(
                catalog=None,
                schema_name="public",
                name=n,
                table_type="table",
                columns=[
                    ColumnInfo(name=c, data_type="integer", native_type="int4")
                    for c in cols.get(n, ["id"])
                ],
            )
            for n in names
        ],
    )


def _fk_schema() -> DataSourceSchema:
    """``orders.ssn_ref`` is excluded, so the adapter never introspected it."""
    return _schema(
        ["orders", "customers"],
        columns={"orders": ["id", "customer_id"], "customers": ["id"]},
    )


def _fk_edges() -> tuple[RelationshipEdge, RelationshipEdge]:
    """A live FK edge and one on the excluded ``orders.ssn_ref`` (adapter FKs keep it)."""

    def edge(column: str) -> RelationshipEdge:
        return RelationshipEdge(
            from_table="public.orders",
            from_column=column,
            to_table="public.customers",
            to_column="id",
            join_sql=f"public.orders.{column} = public.customers.id",
        )

    return edge("customer_id"), edge("ssn_ref")


def _header(
    status: GenerationStatus,
    schema_hash: str,
    done: int = 0,
    total: int = 2,
    run_id: str | None = "run-1",
):
    return semantic_store.SemanticHeader(
        generation_status=str(status),
        schema_hash=schema_hash,
        generation_progress=GenerationProgress(tables_done=done, tables_total=total, batch_size=4),
        generation_run_id=run_id,
    )


def _no_provenance() -> dict:
    return {
        "model": {},
        "table": {},
        "column": {},
        "metric": {},
        "global_item": {},
        "relationship": {},
    }


def _reset_limiter() -> None:
    from app.auth.limiter import limiter

    limiter._storage.reset()


class TestGenerateInit:
    def setup_method(self) -> None:
        _reset_limiter()

    async def test_fresh_run_plans_every_key_and_blanks_nothing(self, http_client) -> None:
        conn = _make_conn()
        begin = AsyncMock()
        # One result only, for the row lock: any direct row write would fail the request.
        db = _mock_db(MockResult(single=_make_conn()))
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders", "customers"]), conn)
            gen = mock_gen.return_value
            gen.prepare_generation.return_value = {
                "tables_total": 2,
                "batch_count": 1,
                "batch_size": 4,
            }
            gen.compute_schema_hash.return_value = "hash-abc"
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")

        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_table_keys"] == ["public.orders", "public.customers"]
        assert body["resumed"] is False
        assert body["batch_size"] == GENERATION_BATCH_SIZE
        assert body["batch_count"] == 1
        kwargs = begin.await_args.kwargs
        assert kwargs["schema_hash"] == "hash-abc"
        assert kwargs["tables_total"] == 2
        assert kwargs["tables_done"] == 0
        assert kwargs["batch_size"] == GENERATION_BATCH_SIZE
        assert kwargs["source_dialect"] == "postgresql"
        assert kwargs["resume"] is False
        assert db.execute.await_count == 1
        assert db.execute.await_args.args[0]._for_update_arg is not None
        db.commit.assert_awaited_once()

    async def test_resume_returns_only_pending_keys(self, http_client) -> None:
        conn = _make_conn()
        begin = AsyncMock()
        pending = AsyncMock(return_value=["public.customers"])
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "h")),
            ),
            patch(f"{_STORE}.pending_table_keys", new=pending),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders", "customers"]), conn)
            gen = mock_gen.return_value
            gen.prepare_generation.return_value = {
                "tables_total": 2,
                "batch_count": 1,
                "batch_size": 4,
            }
            gen.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["resumed"] is True
        assert body["pending_table_keys"] == ["public.customers"]
        assert pending.await_args.args[2] == ["public.orders", "public.customers"]
        assert begin.await_args.kwargs["tables_done"] == 1
        assert begin.await_args.kwargs["resume"] is True

    async def test_resume_without_an_interrupted_run_is_400(self, http_client) -> None:
        begin = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=None)),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 400
        db.rollback.assert_awaited()  # the row lock is released
        db.commit.assert_not_awaited()
        begin.assert_not_awaited()

    async def test_resume_of_a_finished_run_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.COMPLETE, "h")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 400

    async def test_resume_onto_a_changed_schema_is_409(self, http_client) -> None:
        begin = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "old")),
            ),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            mock_gen.return_value.compute_schema_hash.return_value = "new"
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 409
        db.rollback.assert_awaited()  # the row lock is released
        db.commit.assert_not_awaited()
        detail = resp.json()["detail"]
        assert "schema changed" in detail.lower()
        # Points at the non-destructive fix first, and says what Restart costs.
        assert "regenerate (keeps your edits)" in detail
        assert "Restart from scratch discards them" in detail
        begin.assert_not_awaited()

    async def test_takes_the_row_lock_before_reading_run_state(self, http_client) -> None:
        calls: list[str] = []

        async def lock(*_a, **_k):
            calls.append("lock")
            return _make_conn()

        async def header(*_a, **_k):
            calls.append("load_header")
            return _header(GenerationStatus.TABLES_PARTIAL, "h")

        async def pending(*_a, **_k):
            calls.append("pending_table_keys")
            return []

        async def begin(*_a, **_k):
            calls.append("begin_generation")

        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch("app.routers.semantic.lock_and_reread_connection", new=lock),
            patch(f"{_STORE}.load_header", new=header),
            patch(f"{_STORE}.pending_table_keys", new=pending),
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(
                "/api/v1/connections/conn-123/semantic/generate/init?resume=true"
            )
        assert resp.status_code == 200
        assert calls == ["lock", "load_header", "pending_table_keys", "begin_generation"]

    async def test_connection_deleted_before_the_lock_is_409(self, http_client) -> None:
        begin = AsyncMock()
        db = _mock_db(MockResult(single=None))  # lock_and_reread finds no row
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(f"{_STORE}.begin_generation", new=begin),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.prepare_generation.return_value = {
                "tables_total": 1,
                "batch_count": 1,
                "batch_size": 4,
            }
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post("/api/v1/connections/conn-123/semantic/generate/init")
        assert resp.status_code == 409
        begin.assert_not_awaited()
        db.rollback.assert_awaited()


_BATCH_URL = "/api/v1/connections/conn-123/semantic/generate/batch"


class TestGenerateBatch:
    def setup_method(self) -> None:
        _reset_limiter()

    def _provider(self):
        return (MagicMock(), "model-x", "claude")

    def _patches(
        self,
        mock_gen_target: str = "app.routers.semantic.SemanticModelGenerator",
        headers: list | None = None,
        reconcile: AsyncMock | None = None,
        advance: AsyncMock | None = None,
    ):
        """The patches a batch that reaches the LLM needs; returns them for ``with``."""
        hdrs = iter(headers or [_header(GenerationStatus.TABLES_PARTIAL, "h")] * 2)
        return (
            patch("app.routers.semantic._load_schema_for_connection"),
            patch(
                "app.routers.semantic._resolve_provider",
                new=AsyncMock(return_value=self._provider()),
            ),
            patch(mock_gen_target),
            patch(f"{_STORE}.load_header", new=AsyncMock(side_effect=lambda *_: next(hdrs))),
            patch(f"{_STORE}.reconcile_tables", new=reconcile or AsyncMock()),
            patch(
                f"{_STORE}.advance_progress",
                new=advance
                or AsyncMock(
                    return_value=GenerationProgress(tables_done=1, tables_total=2, batch_size=4)
                ),
            ),
        )

    @staticmethod
    def _wire(mock_load, mock_gen, generated, schema=None) -> MagicMock:
        mock_load.return_value = (schema or _schema(["orders", "customers"]), _make_conn())
        gen = mock_gen.return_value
        gen.compute_schema_hash.return_value = "h"
        gen._build_relationship_graph.return_value = []
        if isinstance(generated, BaseException):
            gen.generate_tables = AsyncMock(side_effect=generated)
        else:
            gen.generate_tables = AsyncMock(return_value=generated)
        return gen

    async def test_passes_table_keys_to_the_generator_and_reconciles(self, http_client) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            gen = self._wire(mock_load, mock_gen, generated)
            db = _mock_db(MockResult(single=_make_conn()))  # lock_and_reread_connection
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert resp.json() == {
            "connection_id": "conn-123",
            "table_keys": ["public.orders"],
            "tables_done": 1,
            "tables_total": 2,
            "batch_size": 4,
        }
        assert gen.generate_tables.await_args.kwargs["table_keys"] == ["public.orders"]
        assert reconcile.await_args.args[2] == generated
        assert reconcile.await_args.kwargs["updated_by"] == "admin-1"
        assert reconcile.await_args.kwargs["expected_run_id"] == "run-1"
        # The connection row lock is taken before the reconcile.
        stmt = db.execute.await_args_list[0].args[0]
        assert stmt._for_update_arg is not None
        db.commit.assert_awaited_once()

    async def test_tables_the_batch_did_not_name_are_not_reconciled(self, http_client) -> None:
        generated = {
            "public.orders": TableSemantic(display_name="Orders"),
            "public.customers": TableSemantic(display_name="Not asked for"),
        }
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert set(reconcile.await_args.args[2]) == {"public.orders"}

    async def test_an_fk_on_an_excluded_column_does_not_reach_the_generator(
        self, http_client
    ) -> None:
        """Adapter FKs are filtered by table only; the adapter never introspects an
        excluded column, so an FK edge on one must not name it in the prompt."""
        live, hidden = _fk_edges()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            gen = self._wire(mock_load, mock_gen, {}, schema=_fk_schema())
            gen._build_relationship_graph.return_value = [live, hidden]
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})

        assert resp.status_code == 200
        assert gen.generate_tables.await_args.kwargs["relationship_edges"] == [live]

    async def test_omitted_tables_are_logged_and_left_pending(self, http_client, caplog) -> None:
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with (
            caplog.at_level(logging.WARNING, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(mock_load, mock_gen, generated)
            app.dependency_overrides[get_db] = lambda: _mock_db(MockResult(single=_make_conn()))
            resp = await http_client.post(
                _BATCH_URL, json={"table_keys": ["public.orders", "public.customers"]}
            )

        assert resp.status_code == 200
        assert resp.json()["table_keys"] == ["public.orders", "public.customers"]
        assert set(reconcile.await_args.args[2]) == {"public.orders"}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("public.customers" in r.getMessage() for r in warnings)
        assert not any("public.orders'" in r.getMessage() for r in warnings)

    async def test_without_init_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(f"{_STORE}.load_header", new=AsyncMock(return_value=None)),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400

    async def test_a_partial_header_without_a_run_id_is_400(self, http_client) -> None:
        """Migration 0003 backfills an interrupted blob-era run as tables_partial with no
        run id. Stamping tables with None would leave progress at 0 and make a later
        resume regenerate everything, so the batch sends the client to /generate/init."""
        reconcile = AsyncMock()
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(
                    return_value=_header(GenerationStatus.TABLES_PARTIAL, "h", run_id=None)
                ),
            ),
            patch(f"{_STORE}.reconcile_tables", new=reconcile),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        assert "/generate/init" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        mock_gen.return_value.generate_tables.assert_not_called()

    async def test_changed_schema_is_409(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "stale")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "fresh"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "regenerate (keeps your edits)" in detail
        assert "Restart from scratch discards them" in detail

    async def test_unknown_key_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            patch(
                f"{_STORE}.load_header",
                new=AsyncMock(return_value=_header(GenerationStatus.TABLES_PARTIAL, "h")),
            ),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            mock_gen.return_value.compute_schema_hash.return_value = "h"
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.ghost"]})
        assert resp.status_code == 400
        assert "public.ghost" in resp.json()["detail"]

    async def test_more_than_batch_size_keys_is_422(self, http_client) -> None:
        app.dependency_overrides[get_db] = lambda: _mock_db()
        resp = await http_client.post(
            _BATCH_URL,
            json={"table_keys": [f"public.t{i}" for i in range(GENERATION_BATCH_SIZE + 1)]},
        )
        assert resp.status_code == 422

    async def test_status_moved_on_during_the_llm_call_is_409(self, http_client) -> None:
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(
            headers=[
                _header(GenerationStatus.TABLES_PARTIAL, "h"),  # pre-LLM guard
                _header(GenerationStatus.COMPLETE, "h"),  # post-lock re-read
            ],
            reconcile=reconcile,
        )
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        reconcile.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_a_batch_from_a_superseded_run_is_409(self, http_client) -> None:
        """A fresh /generate/init during the LLM call keeps the status tables_partial
        but mints a new run id; reconciling would stamp these tables with it."""
        reconcile = AsyncMock()
        advance = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(
            headers=[
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-1"),
                _header(GenerationStatus.TABLES_PARTIAL, "h", run_id="run-2"),
            ],
            reconcile=reconcile,
            advance=advance,
        )
        generated = {"public.orders": TableSemantic(display_name="Orders")}
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, generated)
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        advance.assert_not_awaited()
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_connection_deleted_during_the_llm_call_is_409(self, http_client) -> None:
        reconcile = AsyncMock()
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=None))  # lock_and_reread finds no row
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"]
        reconcile.assert_not_awaited()
        db.rollback.assert_awaited()

    async def test_store_value_error_is_400_not_500(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=ValueError("table_key 'x...' is 600 characters"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        assert "600 characters" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_run_superseded_at_the_write_is_409(self, http_client) -> None:
        reconcile = AsyncMock(side_effect=semantic_store.SemanticStoreConflict("superseded"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(reconcile=reconcile)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 409
        assert "restarted" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_progress_value_error_is_400_not_500(self, http_client) -> None:
        advance = AsyncMock(side_effect=ValueError("malformed progress"))
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches(advance=advance)
        with p_load as mock_load, p_prov, p_gen as mock_gen, p_hdr, p_rec, p_adv:
            self._wire(mock_load, mock_gen, {"public.orders": TableSemantic(display_name="O")})
            db = _mock_db(MockResult(single=_make_conn()))
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 400
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_generator_value_error_is_422_and_logs_the_traceback(
        self, http_client, caplog
    ) -> None:
        p_load, p_prov, p_gen, p_hdr, p_rec, p_adv = self._patches()
        with (
            caplog.at_level(logging.ERROR, logger="app.routers.semantic"),
            p_load as mock_load,
            p_prov,
            p_gen as mock_gen,
            p_hdr,
            p_rec,
            p_adv,
        ):
            self._wire(mock_load, mock_gen, ValueError("LLM call failed in generate_tables: boom"))
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_BATCH_URL, json={"table_keys": ["public.orders"]})
        assert resp.status_code == 422
        assert "boom" in resp.json()["detail"]
        errors = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert errors and errors[-1].exc_info is not None


_GLOBALS_URL = "/api/v1/connections/conn-123/semantic/generate/globals"


class TestGenerateGlobals:
    def setup_method(self) -> None:
        _reset_limiter()

    @staticmethod
    def _partial(**fields) -> SemanticModel:
        return SemanticModel.model_validate(
            {
                **_EMPTY_MODEL,
                "generation_status": "tables_partial",
                "tables": {"public.orders": {"display_name": "Orders", "columns": {}}},
                **fields,
            }
        )

    async def _run(
        self,
        http_client,
        *,
        model: SemanticModel,
        schema: DataSourceSchema,
        llm=([], [], [], [], []),
        provenance: dict | None = None,
        save: AsyncMock | None = None,
        db: MagicMock | None = None,
        extra_patches: tuple = (),
        fresh_edges: list[RelationshipEdge] | None = None,
        conn: MagicMock | None = None,
    ):
        """POST /generate/globals with the store and LLM mocked; returns (resp, save, gen)."""
        save = save or AsyncMock()
        db = db or _mock_db(
            MockResult(single=_make_conn()),  # lock_and_reread_connection
            MockResult(),  # clear the legacy blob
            MockResult(),
            MockResult(),
            MockResult(),  # cache deletes
        )
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(
                "app.routers.semantic._resolve_provider",
                new=AsyncMock(return_value=(MagicMock(), "model-x", "claude")),
            ),
            patch("app.routers.semantic.SemanticModelGenerator") as mock_gen,
            # After the save, load_model returns what was saved (the route reloads).
            patch(
                f"{_STORE}.load_model",
                new=AsyncMock(
                    side_effect=lambda *_: save.await_args.args[2] if save.await_args else model
                ),
            ),
            patch(
                f"{_STORE}.provenance_map",
                new=AsyncMock(return_value=provenance or _no_provenance()),
            ),
            patch(f"{_STORE}.save_model", new=save),
            ExitStack() as stack,
        ):
            # Entered last, so an extra patch overrides a default one above.
            for extra in extra_patches:
                stack.enter_context(extra)
            mock_load.return_value = (schema, conn or _make_conn())
            gen = mock_gen.return_value
            gen._build_relationship_graph.return_value = fresh_edges or []
            if isinstance(llm, BaseException):
                gen.generate_globals = AsyncMock(side_effect=llm)
            else:
                gen.generate_globals = AsyncMock(return_value=llm)
            gen.build_time_expressions.return_value = {}
            gen.compute_schema_hash.return_value = "h"
            gen._auto_generate_notes.return_value = []
            app.dependency_overrides[get_db] = lambda: db
            resp = await http_client.post(_GLOBALS_URL)
        return resp, save, gen

    async def test_finalises_over_the_stored_tables_and_keeps_edited_metrics(
        self, http_client
    ) -> None:
        partial = self._partial(
            business_metrics=[
                {
                    "name": "Hand",
                    "metric_type": "simple",
                    "definition": "1",
                    "description": "kept",
                    "aggregation": "sum",
                }
            ]
        )
        llm_metrics = [
            SimpleMetric(
                name="Hand", definition="2", description="llm", aggregation=AggregationType.SUM
            ),
            SimpleMetric(
                name="Fresh", definition="3", description="llm", aggregation=AggregationType.SUM
            ),
        ]
        provenance = {**_no_provenance(), "metric": {"Hand": ["payload"]}}
        db = _mock_db(MockResult(single=_make_conn()), MockResult(), MockResult(), MockResult())
        resp, save, _ = await self._run(
            http_client,
            model=partial,
            schema=_schema(["orders"]),
            llm=(llm_metrics, [], [], [], []),
            provenance=provenance,
            db=db,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["generation_status"] == "complete"
        assert body["generation_progress"] is None
        names = {m["name"]: m["description"] for m in body["business_metrics"]}
        assert names == {"Hand": "kept", "Fresh": "llm"}
        saved = save.await_args.args[2]
        assert saved.generation_status == GenerationStatus.COMPLETE
        assert save.await_args.kwargs["source"] == "generation"
        assert save.await_args.kwargs["provenance"] == provenance
        db.commit.assert_awaited_once()

    async def test_without_a_partial_model_is_400(self, http_client) -> None:
        with (
            patch("app.routers.semantic._load_schema_for_connection") as mock_load,
            patch(f"{_STORE}.load_model", new=AsyncMock(return_value=None)),
        ):
            mock_load.return_value = (_schema(["orders"]), _make_conn())
            app.dependency_overrides[get_db] = lambda: _mock_db()
            resp = await http_client.post(_GLOBALS_URL)
        assert resp.status_code == 400

    async def test_tables_and_columns_that_left_the_schema_are_pruned(self, http_client) -> None:
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "primary_timestamp_column": "dropped_col",
                    "columns": {
                        "id": {"display_name": "ID"},
                        "dropped_col": {"display_name": "Gone"},
                    },
                },
                "public.stale": {"display_name": "Stale", "columns": {}},
            }
        )
        resp, save, gen = await self._run(
            http_client, model=partial, schema=_schema(["orders", "unannotated"])
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders"}
        assert set(saved.tables["public.orders"].columns) == {"id"}
        assert saved.tables["public.orders"].primary_timestamp_column is None
        assert set(resp.json()["tables"]) == {"public.orders"}
        # The LLM is not shown the stale table either.
        assert set(gen.generate_globals.await_args.kwargs["all_tables"]) == {"public.orders"}

    async def test_marked_edges_that_left_the_schema_are_dropped(self, http_client) -> None:
        to_unannotated = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.customer_id = public.customers.id",
        )
        to_gone_table = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.ghosts",
            to_column="id",
            join_sql="public.orders.customer_id = public.ghosts.id",
        )
        from_gone_column = RelationshipEdge(
            from_table="public.orders",
            from_column="region_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.region_id = public.customers.id",
        )
        to_gone_column = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="legacy_id",
            join_sql="public.orders.customer_id = public.customers.legacy_id",
        )
        edges = [to_unannotated, to_gone_table, from_gone_column, to_gone_column]
        partial = self._partial(relationships=[e.model_dump(mode="json") for e in edges])
        provenance = {
            **_no_provenance(),
            "relationship": {semantic_store.relationship_key(e): ["description"] for e in edges},
        }
        # Only public.orders is annotated; public.customers is real but unannotated.
        schema = _schema(
            ["orders", "customers"],
            columns={"orders": ["id", "customer_id"], "customers": ["id"]},
        )
        resp, save, _ = await self._run(
            http_client, model=partial, schema=schema, provenance=provenance
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert saved.relationships == [to_unannotated]

    async def test_fresh_edges_are_bounded_by_the_pruned_tables(self, http_client) -> None:
        """``render_tables`` is the pruned set: a fresh FK edge to a table that is still
        stored but left the schema must not be rebuilt into the saved model."""
        live_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="public.customers",
            to_column="id",
            join_sql="public.orders.customer_id = public.customers.id",
        )
        stale_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="legacy_id",
            to_table="public.legacy",
            to_column="id",
            join_sql="public.orders.legacy_id = public.legacy.id",
        )
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.customers": {"display_name": "Customers", "columns": {}},
                # Stored, but no longer in the live schema: prune_to_schema removes it.
                "public.legacy": {"display_name": "Legacy", "columns": {}},
            }
        )
        schema = _schema(
            ["orders", "customers"],
            columns={"orders": ["id", "customer_id", "legacy_id"], "customers": ["id"]},
        )
        resp, save, _ = await self._run(
            http_client, model=partial, schema=schema, fresh_edges=[live_edge, stale_edge]
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders", "public.customers"}
        assert live_edge in saved.relationships
        assert stale_edge not in saved.relationships

    async def test_an_fk_on_an_excluded_column_is_neither_prompted_nor_saved(
        self, http_client
    ) -> None:
        live, hidden = _fk_edges()
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.customers": {"display_name": "Customers", "columns": {}},
            }
        )
        resp, save, gen = await self._run(
            http_client,
            model=partial,
            schema=_fk_schema(),
            fresh_edges=[live, hidden],
            conn=_make_conn(privacy_settings={"excluded_columns": ["public.orders.ssn_ref"]}),
        )

        assert resp.status_code == 200
        assert gen.generate_globals.await_args.kwargs["relationship_edges"] == [live]
        assert save.await_args.args[2].relationships == [live]

    async def test_what_the_privacy_policy_hides_survives_globals(self, http_client) -> None:
        """The schema the route sees has excluded tables removed and excluded columns
        never introspected; that absence is the policy, not drift."""
        hidden_edge = RelationshipEdge(
            from_table="public.orders",
            from_column="customer_id",
            to_table="hr.people",
            to_column="id",
            join_sql="public.orders.customer_id = hr.people.id",
        )
        partial = self._partial(
            tables={
                "public.orders": {
                    "display_name": "Orders",
                    "columns": {
                        "id": {"display_name": "ID"},
                        "customer_id": {"display_name": "Customer"},
                        "ssn": {"display_name": "SSN", "description": "hand written"},
                    },
                },
                "hr.people": {"display_name": "People", "description": "hand written"},
            },
            relationships=[hidden_edge.model_dump(mode="json")],
        )
        provenance = {
            **_no_provenance(),
            "table": {"hr.people": ["description"]},
            "column": {"public.orders.ssn": ["description"]},
            "relationship": {semantic_store.relationship_key(hidden_edge): ["join_sql"]},
        }
        conn = _make_conn(
            privacy_settings={
                "excluded_schemas": ["hr"],
                "excluded_columns": ["public.orders.ssn"],
            }
        )
        schema = _schema(["orders"], columns={"orders": ["id", "customer_id"]})
        resp, save, gen = await self._run(
            http_client, model=partial, schema=schema, provenance=provenance, conn=conn
        )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert set(saved.tables) == {"public.orders", "hr.people"}
        assert saved.tables["hr.people"].description == "hand written"
        assert saved.tables["public.orders"].columns["ssn"].description == "hand written"
        assert saved.relationships == [hidden_edge]
        # Kept in the store, but never shown to the LLM.
        llm_tables = gen.generate_globals.await_args.kwargs["all_tables"]
        assert set(llm_tables) == {"public.orders"}
        assert set(llm_tables["public.orders"].columns) == {"id", "customer_id"}

    async def test_a_near_miss_drop_is_warned_once_per_call(self, http_client, caplog) -> None:
        """prune_to_schema runs twice (LLM input, then the save); only one warns."""
        partial = self._partial(
            tables={
                "public.orders": {"display_name": "Orders", "columns": {}},
                "public.Orders": {"display_name": "Legacy spelling", "columns": {}},
            }
        )
        with caplog.at_level(logging.WARNING, logger="app.semantic.generator"):
            resp, save, _ = await self._run(http_client, model=partial, schema=_schema(["orders"]))
        assert resp.status_code == 200
        assert set(save.await_args.args[2].tables) == {"public.orders"}
        near_miss = [r for r in caplog.records if "near-miss" in r.getMessage()]
        assert len(near_miss) == 1

    async def test_unstorable_generated_names_are_dropped_not_fatal(
        self, http_client, caplog
    ) -> None:
        long_name = "x" * 513
        fits_once_nuls_go = "a" * 512 + "\x00"
        # Segments, derived columns and joins share a Text ``name`` column that only
        # the unique index's byte limit bounds; a metric name is VARCHAR(512).
        too_long_for_index = "x" * (semantic_store._MAX_INDEXED_TEXT_BYTES + 1)
        metrics = [
            SimpleMetric(name=long_name, definition="1", aggregation=AggregationType.SUM),
            SimpleMetric(name="ok", definition="1", aggregation=AggregationType.SUM),
            SimpleMetric(name=fits_once_nuls_go, definition="1", aggregation=AggregationType.SUM),
        ]
        segments = [
            Segment(name=too_long_for_index, sql_expression="1=1"),
            Segment(name="seg", sql_expression="1=1"),
        ]
        derived = [
            DerivedColumn(name=too_long_for_index, sql_expression="a/b"),
            DerivedColumn(name="ratio", sql_expression="a/b"),
        ]
        # A join is keyed by its pattern, which is Text bounded by the unique index.
        joins = [
            CommonJoin(
                description="too long for the index",
                tables=["public.orders"],
                join_pattern="j" * (semantic_store._MAX_INDEXED_TEXT_BYTES + 1),
            ),
            CommonJoin(description="fine", tables=["public.orders"], join_pattern="o.id = c.id"),
        ]
        with caplog.at_level(logging.WARNING, logger="app.routers.semantic"):
            resp, save, _ = await self._run(
                http_client,
                model=self._partial(),
                schema=_schema(["orders"]),
                llm=(metrics, joins, derived, [], segments),
            )

        assert resp.status_code == 200
        saved = save.await_args.args[2]
        assert [m.name for m in saved.business_metrics] == ["ok", fits_once_nuls_go]
        assert [s.name for s in saved.segments] == ["seg"]
        assert [d.name for d in saved.derived_columns] == ["ratio"]
        assert [j.join_pattern for j in saved.common_joins] == ["o.id = c.id"]
        dropped = [r for r in caplog.records if "dropped generated" in r.getMessage()]
        assert len(dropped) == 4

    async def test_store_value_error_is_400_not_500(self, http_client) -> None:
        db = _mock_db(MockResult(single=_make_conn()))
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=AsyncMock(side_effect=ValueError("from_table 'x...' is 600 characters")),
            db=db,
        )
        assert resp.status_code == 400
        assert "600 characters" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    async def test_generator_value_error_is_422_and_logs_the_traceback(
        self, http_client, caplog
    ) -> None:
        save = AsyncMock()
        with caplog.at_level(logging.ERROR, logger="app.routers.semantic"):
            resp, _, _ = await self._run(
                http_client,
                model=self._partial(),
                schema=_schema(["orders"]),
                llm=ValueError("LLM call failed: boom"),
                save=save,
            )
        assert resp.status_code == 422
        errors = [r for r in caplog.records if r.name == "app.routers.semantic"]
        assert errors and errors[-1].exc_info is not None
        save.assert_not_awaited()

    async def test_provenance_is_read_after_the_row_lock(self, http_client) -> None:
        calls: list[str] = []

        async def lock(*_a, **_k):
            calls.append("lock")
            return _make_conn()

        async def provenance(*_a, **_k):
            calls.append("provenance")
            return _no_provenance()

        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            db=_mock_db(MockResult(), MockResult(), MockResult()),  # cache deletes only
            extra_patches=(
                patch("app.routers.semantic.lock_and_reread_connection", new=lock),
                patch(f"{_STORE}.provenance_map", new=provenance),
            ),
        )
        assert resp.status_code == 200
        assert calls == ["lock", "provenance"]

    async def test_connection_deleted_during_the_llm_call_is_409(self, http_client) -> None:
        """A vanished connection under the lock is 409 (state moved on), nothing saved."""
        save = AsyncMock()
        db = _mock_db()
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            db=db,
            extra_patches=(
                patch(
                    "app.routers.semantic.lock_and_reread_connection",
                    new=AsyncMock(side_effect=HTTPException(status_code=404, detail="gone")),
                ),
            ),
        )
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"]
        save.assert_not_awaited()
        db.rollback.assert_awaited()

    async def test_returns_the_model_as_stored(self, http_client) -> None:
        stored = self._partial(notes=["as stored"])
        save = AsyncMock()
        loads = iter([self._partial(), self._partial(), stored])
        resp, _, _ = await self._run(
            http_client,
            model=self._partial(),
            schema=_schema(["orders"]),
            save=save,
            extra_patches=(
                patch(f"{_STORE}.load_model", new=AsyncMock(side_effect=lambda *_: next(loads))),
            ),
        )
        assert resp.status_code == 200
        assert resp.json()["notes"] == ["as stored"]
        save.assert_awaited_once()
