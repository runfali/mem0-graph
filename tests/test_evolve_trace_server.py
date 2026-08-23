"""Tests for RECALL trace persistence (T6b): /search captures the funnel trace
into evolve_queries.trace while keeping the client response trace-free."""

import os
import uuid
import sys
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-trace-server")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base  # noqa: E402
from models import EvolveQuery, User  # noqa: E402

import server_state  # noqa: E402


class _FakeMemory:
    reranker = None

    def __init__(self, results=None, trace=None, error=None):
        self._trace = trace
        self._error = error
        self._results = {"results": results if results is not None else []}

    def search(self, query, filters, **params):
        if self._error:
            raise self._error
        if self._trace is not None:
            self._results["trace"] = self._trace
        return self._results


_TRACE = {
    "depth": "full",
    "stages": [
        {"stage": "candidates", "count": 42, "latency_ms": 12.3},
        {"stage": "threshold", "count": 8, "latency_ms": 1.2},
        {"stage": "decay", "count": 8, "latency_ms": 0.0},
        {"stage": "graph", "count": 0, "latency_ms": 0.0},
        {"stage": "rerank", "count": 5, "latency_ms": 3.4},
        {"stage": "final", "count": 5, "latency_ms": 0.5},
    ],
}


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
    server_main.SessionLocal = TestingSessionLocal
    server_main.get_memory_instance = lambda: _FakeMemory()

    return TestClient(app, raise_server_exceptions=False), TestingSessionLocal


def _set_memory(client, memory):
    server_main.get_memory_instance = lambda: memory


def _row_for(client, query, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client[1]() as db:
            row = db.scalar(select(EvolveQuery).where(EvolveQuery.query == query))
        if row is not None:
            return row
        time.sleep(0.05)
    raise AssertionError(f"no evolve_queries row for query {query!r}")


def test_search_persists_trace(client):
    _set_memory(client, _FakeMemory(results=[{"id": "a", "memory": "m1", "score": 0.9}], trace=_TRACE))

    resp = client[0].post("/search", json={"query": "trace me"})
    assert resp.status_code == 200

    row = _row_for(client, "trace me")
    assert row.trace is not None
    assert row.trace["depth"] == "full"
    assert {s["stage"] for s in row.trace["stages"]} == {
        "candidates",
        "threshold",
        "decay",
        "graph",
        "rerank",
        "final",
    }
    assert row.trace["stages"][0]["count"] == 42


def test_evolve_queries_depth_reflects_trace_depth(client):
    _set_memory(client, _FakeMemory(results=[{"id": "a", "memory": "m1", "score": 0.9}], trace=_TRACE))

    resp = client[0].post("/search", json={"query": "depth from trace", "depth": "minimal"})
    assert resp.status_code == 200

    row = _row_for(client, "depth from trace")
    assert row.depth == "full"


def test_evolve_queries_depth_keeps_param_without_trace(client):
    _set_memory(client, _FakeMemory(results=[{"id": "a", "memory": "m1", "score": 0.9}], trace=None))

    resp = client[0].post("/search", json={"query": "depth param only", "depth": "minimal"})
    assert resp.status_code == 200

    row = _row_for(client, "depth param only")
    assert row.depth == "minimal"


def test_trace_not_leaked_to_client(client):
    _set_memory(client, _FakeMemory(results=[{"id": "a", "memory": "m1", "score": 0.9}], trace=_TRACE))

    resp = client[0].post("/search", json={"query": "no leak"})
    assert resp.status_code == 200
    body = resp.json()
    assert "trace" not in body
    assert body == {"results": [{"id": "a", "memory": "m1", "score": 0.9}]}


def test_without_trace_backwards_compatible(client):
    _set_memory(client, _FakeMemory(results=[{"id": "b", "memory": "m2", "score": 0.8}], trace=None))

    resp = client[0].post("/search", json={"query": "plain search"})
    assert resp.status_code == 200
    assert resp.json() == {"results": [{"id": "b", "memory": "m2", "score": 0.8}]}

    row = _row_for(client, "plain search")
    assert row.trace is None


def test_zero_hit_records_empty_trace(client):
    _set_memory(client, _FakeMemory(results=[], trace=_TRACE))

    resp = client[0].post("/search", json={"query": "zero hits"})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}

    row = _row_for(client, "zero hits")
    assert row.trace is not None
    assert row.result_count == 0
