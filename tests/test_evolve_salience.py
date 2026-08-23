"""Tests for memory salience (热度体系): access-count tracking in /search and
the opt-in salience rank boost in score_and_rank.

Endpoint tests reuse the test_evolve_queries.py pattern (sqlite temp DB +
FakeMemory). Scoring tests exercise score_and_rank directly: the default
MEM0_EVOLVE_RANK_WEIGHT=0 path must keep ordering byte-identical, and a
positive weight must lift high-frequency memories.
"""

import os
import uuid
import sys
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-salience")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# server/ modules use bare imports (from db import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base  # noqa: E402
from models import EvolveSalience, User  # noqa: E402

# Stub out initialize_state before importing main so collection succeeds
# without a live DB (same approach as test_evolve_queries.py).
import server_state  # noqa: E402


class _FakeMemory:
    reranker = None

    def __init__(self, results=None):
        self._results = {"results": results if results is not None else []}

    def search(self, query, filters, **params):
        return self._results


server_state.initialize_state = lambda config: None
server_state.get_memory_instance = lambda: _FakeMemory()

from fastapi.testclient import TestClient  # noqa: E402
import main as server_main  # noqa: E402


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False, "timeout": 10},
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


def _salience_row_for(client, mem_id, timeout=5.0):
    """Poll for the persisted salience row (writes are async)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client[1]() as db:
            row = db.get(EvolveSalience, mem_id)
        if row is not None:
            return row
        time.sleep(0.05)
    raise AssertionError(f"no evolve_salience row for {mem_id!r}")


def test_hit_memory_gets_access_count_and_last_access(client):
    _set_memory(client, _FakeMemory(results=[{"id": "m1", "memory": "like hiking", "score": 0.9}]))

    resp = client[0].post("/search", json={"query": "hiking", "filters": {"user_id": "u1"}})
    assert resp.status_code == 200

    row = _salience_row_for(client, "m1")
    assert row.access_count == 1
    assert row.last_access_at is not None


def test_repeated_hits_increment_access_count_and_refresh_last_access(client):
    _set_memory(client, _FakeMemory(results=[{"id": "m1", "memory": "like hiking", "score": 0.9}]))

    client[0].post("/search", json={"query": "hiking", "filters": {"user_id": "u1"}})
    first = _salience_row_for(client, "m1")

    time.sleep(0.05)
    client[0].post("/search", json={"query": "trails", "filters": {"user_id": "u1"}})
    second = _salience_row_for(client, "m1")

    assert second.access_count == 2
    assert second.last_access_at >= first.last_access_at


def test_graph_fragments_are_not_counted(client):
    _set_memory(
        client,
        _FakeMemory(
            results=[
                {"id": "m1", "memory": "like hiking", "score": 0.9},
                {"id": "7c99f0a0-0000-4000-8000-000000000000", "memory": "发哥 偏好 喝茶",
                 "score": 0.7, "source": "graph"},
            ]
        ),
    )

    client[0].post("/search", json={"query": "hiking", "filters": {"user_id": "u1"}})

    _salience_row_for(client, "m1")
    with client[1]() as db:
        assert db.get(EvolveSalience, "7c99f0a0-0000-4000-8000-000000000000") is None


def test_zero_hit_search_writes_no_salience(client):
    resp = client[0].post("/search", json={"query": "nothing matches"})
    assert resp.status_code == 200
    with client[1]() as db:
        assert db.query(EvolveSalience).count() == 0


class TestSalienceRankBoost:
    def test_default_weight_zero_keeps_ordering_identical(self):
        from mem0.utils.scoring import score_and_rank

        results = [
            {"id": "a", "score": 0.9, "payload": {"data": "mem a"}},
            {"id": "b", "score": 0.8, "payload": {"data": "mem b"}},
        ]
        base = score_and_rank(results, {}, {}, threshold=0.1, top_k=10)
        boosted = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10,
            salience_scores={
                "a": {"acc": 100, "sal": 1.0}, "b": {"acc": 50, "sal": 1.0},
            },
            salience_rank_weight=0.0,
        )
        assert [r["id"] for r in boosted] == [r["id"] for r in base]
        assert [r["score"] for r in boosted] == pytest.approx([r["score"] for r in base])

    def test_weight_zero_keeps_original_order_even_with_scores(self):
        from mem0.utils.scoring import score_and_rank

        results = [
            {"id": "cold", "score": 0.9, "payload": {"data": "mem a"}},
            {"id": "hot", "score": 0.8, "payload": {"data": "mem b"}},
        ]
        scored = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10,
            salience_scores={"hot": {"acc": 100, "sal": 1.0}},
            salience_rank_weight=0.0,
        )
        assert [r["id"] for r in scored] == ["cold", "hot"]

    def test_high_frequency_memory_ranks_higher_with_weight(self):
        from mem0.utils.scoring import score_and_rank

        results = [
            {"id": "cold", "score": 0.9, "payload": {"data": "mem a"}},
            {"id": "hot", "score": 0.8, "payload": {"data": "mem b"}},
        ]
        # heat_effective (min(acc/100, 1) + (sal - 1)) is computed in
        # score_and_rank; hot is max heat with default salience.
        scored = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10,
            salience_scores={
                "hot": {"acc": 100, "sal": 1.0},
                "cold": {"acc": 0, "sal": 1.0},
            },
            salience_rank_weight=0.5,
        )
        assert [r["id"] for r in scored] == ["hot", "cold"]
        assert scored[0]["score"] == pytest.approx(0.8 * (1 + 0.5 * 1.0))
        assert scored[1]["score"] == pytest.approx(0.9)

    def test_feedback_downweighted_memory_ranks_lower(self):
        from mem0.utils.scoring import score_and_rank

        results = [
            {"id": "neutral", "score": 0.8, "payload": {"data": "mem a"}},
            {"id": "demoted", "score": 0.8, "payload": {"data": "mem b"}},
        ]
        # useless feedback -> salience_score 0.85 gives a negative offset
        # (0.85 - 1.0), so the demoted memory must rank behind the neutral one.
        scored = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10,
            salience_scores={
                "neutral": {"acc": 0, "sal": 1.0},
                "demoted": {"acc": 0, "sal": 0.85},
            },
            salience_rank_weight=0.5,
        )
        assert [r["id"] for r in scored] == ["neutral", "demoted"]
        assert scored[1]["score"] == pytest.approx(0.8 * (1 + 0.5 * (0.85 - 1.0)))
        assert scored[0]["score"] == pytest.approx(0.8)

    def test_explain_reports_salience_factors_when_active(self):
        from mem0.utils.scoring import score_and_rank

        results = [{"id": "a", "score": 0.8, "payload": {"data": "mem a"}}]
        scored = score_and_rank(
            results, {}, {}, threshold=0.1, top_k=10, explain=True,
            salience_scores={"a": {"acc": 100, "sal": 1.0}},
            salience_rank_weight=0.5,
        )
        details = scored[0]["score_details"]
        assert details["salience_heat"] == 1.0
        assert details["salience_score"] == 1.0
        assert details["salience_rank_weight"] == 0.5
        assert details["salience_boost"] == pytest.approx(1.5)
        assert scored[0]["score"] == pytest.approx(0.8 * 1.5)
