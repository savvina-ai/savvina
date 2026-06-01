# Testing

Savvina AI's backend has a comprehensive pytest test suite. This guide covers how to run tests, how the test infrastructure is set up, and how to write new tests.

---

## Running Tests

### Inside Docker (Recommended)

Matches the production environment exactly:

```bash
# Run all tests
docker compose run --rm backend pytest tests/ -v

# Run a specific test file
docker compose run --rm backend pytest tests/test_routers/test_chat.py -v

# Run a specific test class
docker compose run --rm backend pytest tests/test_routers/test_connections.py::TestCreateConnection -v

# Run with test output even on passing tests
docker compose run --rm backend pytest tests/ -v -s

# Run and stop on first failure
docker compose run --rm backend pytest tests/ -x
```

### Local Virtual Environment

If you have Python 3.12 and the backend dependencies installed locally:

```bash
cd backend
pytest tests/ -v
```

**Note:** The `ENCRYPTION_KEY` env var is set automatically by `tests/conftest.py` at collection time — you don't need to set it yourself.

---

## Test Structure

```
backend/tests/
├── conftest.py                    ← Global fixtures: ENCRYPTION_KEY, FakeRecord, MockConnection, MockPool
├── test_config.py                 ← Settings loading, env var aliases
├── test_encryption.py             ← Fernet encrypt/decrypt round-trips
├── test_models/
│   └── test_models_and_schemas.py ← ORM model construction, Pydantic schema validation
├── test_registry.py               ← register_datasource, register_provider decorators
├── test_datasources/
│   ├── test_postgresql.py         ← PostgreSQL adapter: connect, introspect, execute, validate
│   ├── test_mysql.py              ← MySQL adapter: connect, introspect, execute, validate
│   └── test_streaming.py          ← SSE streaming integration tests
├── test_validators/
│   └── test_sql_validator.py      ← BaseSQLValidator and PostgreSQLValidator rules
├── test_providers/
│   └── test_providers.py          ← Provider health checks, generate_response, parse_llm_response
├── test_semantic/
│   └── test_semantic_generator.py ← SemanticModelGenerator with mocked LLM
├── test_services/
│   ├── test_prompt_builder.py     ← PromptBuilder output structure
│   └── test_chat_service.py       ← Full pipeline: 7 scenarios end-to-end
├── test_cache/
│   └── test_query_cache.py        ← Cache lookup, hit counting, eviction
├── test_privacy.py                ← PrivacySettings: sensitive column detection, exclusions
└── test_routers/
    ├── conftest.py                ← MockResult, _mock_db(), _make_conn(), http_client fixture
    ├── test_datasources.py        ← GET /api/v1/datasources
    ├── test_connections.py        ← All /api/v1/connections/* endpoints
    ├── test_semantic.py           ← All /api/v1/connections/{id}/semantic/* endpoints
    ├── test_providers.py          ← All /api/providers/* endpoints
    ├── test_chat.py               ← All /api/v1/chat/* endpoints
    └── test_settings.py           ← GET/PUT /api/settings
```

---

## Configuration

`pytest.ini` (in `backend/`):

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

`asyncio_mode = auto` means all `async def test_*` functions are automatically run as async tests. **Do not add `@pytest.mark.asyncio`** — it's not needed and will cause a warning.

---

## Global Fixtures (`tests/conftest.py`)

### `ENCRYPTION_KEY`

The top-level `conftest.py` sets `os.environ["ENCRYPTION_KEY"]` before any app module is imported, then clears the `get_settings()` LRU cache to force a fresh `Settings()` build:

```python
TEST_ENCRYPTION_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
os.environ["ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
from app.config import get_settings
get_settings.cache_clear()
```

This ensures consistent behavior regardless of whether a real key is in the host environment.

### asyncpg Mocks

Three helpers are importable from the top-level conftest:

```python
from tests.conftest import FakeRecord, MockConnection, MockPool
```

| Class | Mimics | Key attributes |
|---|---|---|
| `FakeRecord` | `asyncpg.Record` | `dict` subclass with subscript access and `.keys()` |
| `MockConnection` | `asyncpg.Connection` | `fetchval`, `fetch`, `execute`, `close` as `AsyncMock`; `transaction()` context manager |
| `MockPool` | `asyncpg.Pool` | `acquire()` context manager returning `MockConnection`; `close` as `AsyncMock` |

---

## Router Test Fixtures (`tests/test_routers/conftest.py`)

### `http_client`

```python
@pytest.fixture
async def http_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
```

This fixture:
- Creates an `httpx.AsyncClient` wired directly to the FastAPI ASGI app (no network required)
- Yields the client for use in tests

**Always use the `http_client` fixture for router tests.** Do not create your own `AsyncClient`.

### `MockResult`

A lightweight stand-in for a SQLAlchemy `CursorResult`:

```python
class MockResult:
    def __init__(self, single=None, rows=None, row=None): ...
    def scalar_one_or_none(self): return self._single
    def scalars(self): return self
    def all(self): return self._rows
    def first(self): return self._rows[0] if self._rows else self._single
    def one(self): return self._row
```

### `_mock_db(*results)`

Returns a `MagicMock` `AsyncSession` with `execute` set as an `AsyncMock` that returns each `MockResult` in order (using `side_effect`):

```python
def _mock_db(*results) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session
```

### `_make_conn(**kwargs)`

Returns a `MagicMock` shaped like a `Connection` ORM row with sensible defaults:

```python
conn = _make_conn(
    id="my-conn-id",
    source_type="postgresql",
    execution_mode="auto_execute",
)
```

---

## Writing Router Tests

Router tests wire a mock DB into FastAPI's dependency injection, then call the HTTP API. Here is the full pattern:

```python
from app.database import get_db
from app.main import app
from .conftest import MockResult, _make_conn, _mock_db


class TestGetConnection:
    async def test_returns_200_with_connection(self, http_client):
        conn = _make_conn(id="abc-123", name="My DB")
        db = _mock_db(MockResult(single=conn))  # first execute() call returns this
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.get("/api/v1/connections/abc-123")

        assert resp.status_code == 200
        assert resp.json()["id"] == "abc-123"

    async def test_returns_404_when_not_found(self, http_client):
        db = _mock_db(MockResult(single=None))  # DB returns no row
        app.dependency_overrides[get_db] = lambda: db

        resp = await http_client.get("/api/v1/connections/doesnt-exist")

        assert resp.status_code == 404
```

Key points:
- `app.dependency_overrides[get_db] = lambda: db` injects the mock session
- Overrides are cleared automatically between tests by the `clear_overrides` autouse fixture
- When a route calls `db.execute()` multiple times, pass multiple `MockResult` objects to `_mock_db()`

### Patching Services

For chat router tests, the service layer is separately mocked:

```python
from unittest.mock import MagicMock, patch

async def _stream_gen(*events):
    for e in events:
        yield e

async def test_chat_success(self, http_client):
    svc = MagicMock()
    svc.stream_message = MagicMock(return_value=_stream_gen())
    with patch("app.routers.chat._make_chat_service", return_value=svc):
        resp = await http_client.post("/api/v1/chat", json={
            "connection_id": "conn-1",
            "message": "How many orders?",
            "provider": "uuid-of-provider-config",
        })
    assert resp.status_code == 200
```

### Patching External Dependencies

Use `patch()` to replace adapter factories, encryption, and async drivers:

```python
# Patch the datasource factory (prevents real DB connections)
with patch("app.routers.connections.create_datasource", return_value=mock_adapter):
    ...

# Patch Fernet decryption in the connections router
with patch("app.routers.connections.decrypt_value", return_value='{"host":"localhost"}'):
    ...

# Patch asyncpg.create_pool in the PostgreSQL adapter
with patch("asyncpg.create_pool", return_value=mock_pool):
    ...
```

**Important patch targets** (confirmed by the existing test suite):

| What you're patching | Correct path |
|---|---|
| Fernet decrypt in connections router | `app.routers.connections.decrypt_value` |
| Adapter factory in connections router | `app.routers.connections.create_datasource` |
| `_schema_from_dict` in semantic router | `app.services.chat_service._schema_from_dict` |
| `asyncio.to_thread` | `asyncio.to_thread` (module-level, not router-scoped) |

---

## Writing Service Tests

Service tests (`test_services/`) unit-test `ChatService` directly without HTTP. Mock the database session, cache, and provider:

```python
from app.services.chat_service import ChatService
from unittest.mock import AsyncMock, MagicMock, patch


async def test_cache_hit_skips_llm():
    cache = MagicMock()
    cache.lookup = MagicMock(return_value=CacheHit(
        query="SELECT COUNT(*) FROM orders",
        explanation="Counts all orders",
        cache_id="cache-1",
    ))
    db = _make_db(
        MockResult(single=mock_connection),   # load connection
        MockResult(single=None),              # load provider config
        # ...
    )
    service = ChatService(db=db, query_cache=cache)
    response = await service.process_message(
        connection_id="conn-1",
        message="how many orders?",
        provider="claude",
    )
    assert response["cache_hit"] is True
    # LLM was never called — no need to mock provider
```

---

## Writing Validator Tests

Validator tests are pure unit tests — no mocks needed:

```python
from app.datasources.validators.postgresql_validator import PostgreSQLValidator


class TestPostgreSQLValidator:
    def setup_method(self):
        self.v = PostgreSQLValidator()

    def test_valid_select(self):
        r = self.v.validate("SELECT id, name FROM customers")
        assert r.is_valid
        assert "LIMIT" in r.sanitized_query  # auto-appended

    def test_insert_is_blocked(self):
        r = self.v.validate("INSERT INTO customers VALUES (1)")
        assert not r.is_valid
        assert "INSERT" in r.error_message

    def test_pg_sleep_is_blocked(self):
        r = self.v.validate("SELECT pg_sleep(5)")
        assert not r.is_valid

    def test_cte_is_allowed(self):
        r = self.v.validate("WITH cte AS (SELECT 1) SELECT * FROM cte")
        assert r.is_valid

    def test_existing_limit_preserved(self):
        r = self.v.validate("SELECT * FROM orders LIMIT 10")
        assert r.is_valid
        assert r.sanitized_query.count("LIMIT") == 1
```

---

## Coverage Areas

The test suite covers:

| Area | Test file | Key scenarios |
|---|---|---|
| Config loading | `test_config.py` | Env var aliases, default values |
| Encryption | `test_encryption.py` | Encrypt/decrypt round-trip, wrong key |
| ORM models | `test_models/` | Model construction, Pydantic serialization |
| Adapter registry | `test_registry.py` | Unknown source type raises ValueError |
| PostgreSQL adapter | `test_datasources/test_postgresql.py` | Connect, introspect, execute, privacy filtering |
| MySQL adapter | `test_datasources/test_mysql.py` | Connect, introspect, execute, validate |
| SQL validator | `test_validators/` | 20+ cases for blocked keywords, CTEs, LIMIT |
| Providers | `test_providers/` | Health check, generate_response, response parsing |
| Prompt builder | `test_services/test_prompt_builder.py` | Section ordering, intent hints, entity context, conditional sections (semantic, few-shot, time expressions) |
| Chat service | `test_services/test_chat_service.py` | 7 end-to-end pipeline scenarios |
| Query cache | `test_cache/` | Exact match, semantic match, hit count |
| Privacy | `test_privacy.py` | Sensitive column detection, exclusion lists |
| All routers | `test_routers/` | Happy path + error cases for every endpoint |

---

## Common Pitfalls

**`asyncio_mode = auto` is set** — do not add `@pytest.mark.asyncio`. Adding it causes a warning about "unexpected asyncio mark".

**`get_settings()` is LRU-cached** — if you change env vars in a test, call `get_settings.cache_clear()` afterward, or the stale settings will persist for the remainder of the test session.

**`app.dependency_overrides` bleeds between tests** — always ensure the `clear_overrides` autouse fixture is in scope (it's in `test_routers/conftest.py`). For tests outside `test_routers/`, clean up overrides manually in a `finally` block or with a fixture.

**Mock the right target** — always patch at the import location, not the definition location. If `connections.py` does `from ..utils.encryption import decrypt_value`, patch `app.routers.connections.decrypt_value`, not `app.utils.encryption.decrypt_value`.
