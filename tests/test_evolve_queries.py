"""Tests for search-quality observability (evolve_queries instrumentation in /search)."""

import os
import uuid
import sys
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-queries")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# server/ modules use bare imports (from db import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base  # noqa: E402
from models import EvolveQuery, User  # noqa: E402

# main.py builds a real Memory (pgvector → postgres) at import time via
# initialize_state; stub it out BEFORE importing main so collection succeeds
# without a live DB. The search route calls get_memory_instance() directly
# (not via Depends), so per-test control monkeypatches server_main directly.
import server_state  # noqa: E402


class _FakeMemory:
    reranker = None

    def __init__(self, results=None, error=None):
        # mirrors production Memory.search() shape: {"results": [...]}
        self._results = {"results": results if results is not None else []}
        self._error = error

    def search(self, query, filters, **params):
        if self._error:
            raise self._error
        return self._results


server_state.initialize_state = lambda config: None
server_state.get_memory_instance = lambda: _FakeMemory()

from fastapi.testclient import TestClient  # noqa: E402
import main as server_main  # noqa: E402


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    app = server_main.app
    def _fake_admin():
        return User(
            id=uuid.UUID(int=1),
            name="test-admin",
            email="admin@test.local",
            password_hash="",
            role="admin",
        )

    app.dependency_overrides[require_auth] = _fake_admin
    # _persist_evolve_query writes via main's module-level SessionLocal
    # (postgres in prod); point it at the test DB so rows land in sqlite.
    server_main.SessionLocal = TestingSessionLocal
    # Reset the direct-call global so tests that don't _set_memory get a clean fake.
    server_main.get_memory_instance = lambda: _FakeMemory()

    return TestClient(app, raise_server_exceptions=False), TestingSessionLocal


def _set_memory(client, memory):
    server_main.get_memory_instance = lambda: memory


def _row_for(client, query, timeout=5.0):
    """Poll for the persisted row for a specific query (writes are async)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client[1]() as db:
            row = db.scalar(select(EvolveQuery).where(EvolveQuery.query == query))
        if row is not None:
            return row
        time.sleep(0.05)
    raise AssertionError(f"no evolve_queries row for query {query!r}")


def test_search_success_records_query(client):
    _set_memory(
        client,
        _FakeMemory(
            results=[
                {"id": "a", "memory": "m1", "score": 0.9},
                {"id": "b", "memory": "m2", "score": 0.5},
                {"id": "c", "memory": "m3", "score": None},
            ]
        ),
    )

    resp = client[0].post("/search", json={"query": "favorite food", "filters": {"user_id": "u1"}})
    assert resp.status_code == 200

    row = _row_for(client, "favorite food")
    assert row.query == "favorite food"
    assert row.user_id == "u1"
    assert row.result_count == 3
    assert row.is_zero_hit is False
    # avg over non-None scores only
    assert row.avg_score == 0.7
    assert row.latency_ms >= 0
    assert row.rerank is False


def test_zero_hit_marks_is_zero_hit(client):
    resp = client[0].post("/search", json={"query": "nothing matches"})
    assert resp.status_code == 200

    row = _row_for(client, "nothing matches")
    assert row.result_count == 0
    assert row.is_zero_hit is True
    assert row.avg_score is None


def test_params_captured(client):
    _set_memory(client, _FakeMemory(results=[{"id": "a", "memory": "m1", "score": 0.8}]))

    client[0].post(
        "/search",
        json={
            "query": "param check",
            "filters": {"user_id": "u1", "agent_id": "a1", "run_id": "r1"},
            "top_k": 5,
            "depth": "full",
            "rerank": True,
        },
    )

    row = _row_for(client, "param check")
    assert row.user_id == "u1"
    assert row.agent_id == "a1"
    assert row.run_id == "r1"
    assert row.top_k == 5
    assert row.depth == "full"
    assert row.rerank is True


def test_error_still_records_zero_hit_row(client):
    _set_memory(client, _FakeMemory(error=ValueError("bad filter")))

    resp = client[0].post("/search", json={"query": "boom"})
    assert resp.status_code == 400

    row = _row_for(client, "boom")
    assert row.query == "boom"
    assert row.result_count == 0
    assert row.is_zero_hit is True
    assert row.latency_ms >= 0


def test_instrumentation_never_breaks_success_response(client):
    resp = client[0].post("/search", json={"query": "ok"})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}
